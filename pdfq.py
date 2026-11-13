# -*- coding: utf-8 -*-
"""pdfq —— PDF 批量翻译 · 命令行

为什么改成命令行
----------------
旧版是 Tk 界面 + 每篇一个子进程：子进程每篇都要重新 import babeldoc、
重新加载 ONNX 版面模型，而「并发锁定」之类的补丁只挂在父进程上，进了
子进程就全部失效。界面本身也不产出任何价值——真正要做的只有一件事：
把一堆 PDF 按顺序翻完。

去掉界面之后，一次调用可以直接跑在同一个进程里：模型只加载一次，之后每篇
开箱即跑。代码量从「界面 2600 行 + 驱动 620 行」压到这一个文件。

没有跨次运行的历史队列：命令行给什么就翻什么，翻完就结束。中断了就把
文件再拖/再传一次，不会留下一个越攒越多的待办列表。

用法
----
    pdfq a.pdf b.pdf 论文目录/     # 直接翻这些文件
    pdfq config                    # 看/改引擎与并发
    pdfq logs                      # 打开日志文件夹
    pdfq clean [--force]           # 清理临时文件

选项
----
    --qps N          每秒请求数（默认取 config.env，否则 10）
    --no-adapt       关闭自适应限流（固定用 --qps）
    --no-color       关掉颜色
    --report N       进度刷新间隔秒（默认 0.5）

为什么这样设置并发
------------------
PDFMathTranslate-next 官方 FAQ 对两个旋钮的说法：
    --qps              每秒发几个请求，设成服务商允许的上限
    --pool-max-workers 在飞请求数，经验值 qps × 10（单请求 5–10 秒，
                       线程池太小会让限流阀门空转）
旧配置 qps 8 / pool 8 就是典型空转：只有 8 个请求在飞，每篇 1184 段
硬生生跑成 26 分钟。

免费代理的服务端 /config 公布 qps=10 / pool=100，但实测下来 429 依然
大量出现（旧日志 1198 条），说明真正的瓶颈在代理背后的免费模型额度。
所以这里加了一个 AIMD 控制器：429 一多就降速，稳住了再慢慢加回来。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
import re
import shutil
import sys
import threading
import time
import traceback
from pathlib import Path

# ---------------------------------------------------------------- 基本路径

HERE = Path(__file__).resolve().parent
VENV_PY = HERE / "venv" / "Scripts" / "python.exe"
CONFIG_FILE = HERE / "config.env"
STATE_DIR = Path(os.environ.get("LOCALAPPDATA") or str(Path.home())) / "PDFTranslateQueue"
LOG_DIR = STATE_DIR / "logs"
OUTPUT_SUFFIX = "_中文.pdf"

# 随包附带的模型/字体，是 babeldoc 缓存的离线副本。babeldoc 把缓存路径写死在
# ~/.cache/babeldoc，改不了，所以只能在首次运行时从这里播种过去。
SEED_DIR = Path(__file__).resolve().parent / "models"
SEED_PARTS = ("models", "fonts", "cmap", "tiktoken")

PER_FILE_TIMEOUT = 90 * 60     # 单篇硬上限，防死锁
MAX_ATTEMPTS = 3               # 单篇最多试几次
RETRY_WAIT = 20                # 第 n 次重试前等 n × 这个秒数

# 官方经验值：在飞请求数 ≈ qps × 10
POOL_FACTOR = 10
POOL_CAP = 400

# 能跳的 CPU 阶段都跳掉。这几项都不影响译文质量，只影响中间产物是否
# 漂亮：clean 是压缩输出、formula-offset 是公式微调、scanned-detection
# 是判断是不是扫描件（论文 PDF 都有文字层）。
BASE_ARGS = [
    "--no-dual",                              # 不要双语版，省一半排版
    "--watermark-output-mode", "no_watermark",
    # 术语抽取 = 拿全文再跑一遍 LLM。旧日志实测占 4 分 12 秒 / 32 分钟。
    "--no-auto-extract-glossary",
    "--skip-clean",
    "--skip-formula-offset-calculation",
    "--skip-scanned-detection",
]

# ---------------------------------------------------------------- 终端输出

COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

_ANSI = {
    "dim": "\x1b[2m", "bold": "\x1b[1m", "reset": "\x1b[0m",
    "red": "\x1b[31m", "green": "\x1b[32m", "yellow": "\x1b[33m",
    "blue": "\x1b[34m", "cyan": "\x1b[36m", "grey": "\x1b[90m",
}


def c(text, tone=None, bold=False):
    """上色。非 TTY（重定向到文件）时原样返回。"""
    if not COLOR:
        return text
    prefix = ""
    if tone:
        prefix += _ANSI.get(tone, "")
    if bold:
        prefix += _ANSI["bold"]
    return prefix + text + _ANSI["reset"] if prefix else text


def term_width(default=100):
    try:
        return max(60, min(160, shutil.get_terminal_size((default, 24)).columns))
    except Exception:
        return default


def fmt_dur(seconds):
    seconds = int(round(max(0, seconds)))
    if seconds < 60:
        return "%d 秒" % seconds
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return "%d 分 %02d 秒" % (minutes, sec)
    hours, minutes = divmod(minutes, 60)
    return "%d 小时 %02d 分" % (hours, minutes)


def bar(pct, width):
    pct = 0.0 if pct is None else max(0.0, min(100.0, pct))
    filled = int(round(width * pct / 100.0))
    return "█" * filled + "░" * (width - filled)


def short_name(name, limit):
    if len(name) <= limit:
        return name
    return name[: max(4, limit - 1)] + "…"


# ---------------------------------------------------------------- 引擎配置

ENGINE_FLAGS = {
    "siliconflow": {
        "flag": "--siliconflow", "key": "--siliconflow-api-key",
        "model": "--siliconflow-model", "base_url": "--siliconflow-base-url",
    },
    "deepseek": {
        "flag": "--deepseek", "key": "--deepseek-api-key",
        "model": "--deepseek-model", "base_url": None,
    },
    "openaicompatible": {
        "flag": "--openaicompatible", "key": "--openai-compatible-api-key",
        "model": "--openai-compatible-model", "base_url": "--openai-compatible-base-url",
    },
    # 本地模型走 Ollama（能用 GPU，但吞吐受显卡限制，详见使用说明）
    # 不需要 API Key；host 就是 Ollama 服务地址，默认本机 11434。
    "ollama": {
        "flag": "--ollama", "key": None,
        "model": "--ollama-model", "base_url": "--ollama-host",
    },
}

ENGINE_ALIASES = {
    "": "siliconflowfree", "free": "siliconflowfree",
    "siliconflowfree": "siliconflowfree", "silicon-flow": "siliconflow",
    "openai-compatible": "openaicompatible", "openai_compatible": "openaicompatible",
    "compatible": "openaicompatible", "ds": "deepseek",
    "local": "ollama", "gpu": "ollama", "本地": "ollama",
}

DEFAULT_QPS = 10

# 有些引擎有合理默认值，用户不配也能直接跑。
# Ollama 的并发必须压低：本地只有一张卡，10 路请求会直接把显存撑爆。
ENGINE_DEFAULTS = {
    "ollama": {"model": "qwen2.5:7b", "base_url": "http://localhost:11434", "qps": 4},
}


def read_config():
    cfg = {}
    try:
        text = CONFIG_FILE.read_text(encoding="utf-8-sig")
    except OSError:
        return cfg
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        cfg[key.strip().upper()] = value
    return cfg


def write_config(cfg):
    lines = [
        "# PDF 翻译引擎配置（pdfq config 也可改）",
        "# MODEL_SOURCE: public（默认，公共免费 API）| pinned（在 Magpie 里选定）| magpie（跟随 Magpie 当前模型）",
        "# 免费代理稳定但慢；换成自己的付费 key，qps 能开到 32–64。",
        "ENGINE=%s" % (cfg.get("ENGINE") or "siliconflowfree"),
        "MODEL_SOURCE=%s" % (cfg.get("MODEL_SOURCE") or "public"),
    ]
    for key in ("API_KEY", "MODEL", "BASE_URL", "QPS"):
        if cfg.get(key):
            lines.append("%s=%s" % (key, cfg[key]))
    CONFIG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def resolve_engine(cli_qps=None):
    """返回 (引擎名, 引擎参数列表, 是否配了 key, qps)。

    MODEL_SOURCE=public（默认）时不管 config.env 里写的 ENGINE，一律走公共
    免费节点；pinned / magpie 才用 Magpie 反代。手改配置后两者矛盾，以
    来源为准。
    """
    cfg = read_config()
    source = "magpie"
    try:
        import model_source
        source = model_source.source_of(cfg)
        cfg = model_source.apply_source(cfg, source)
    except Exception as exc:
        logging.getLogger("pdfq").warning("解析模型来源失败，按配置的引擎跑：%s", exc)

    raw = (cfg.get("ENGINE") or "").strip().lower()
    engine = ENGINE_ALIASES.get(raw, raw)
    try:
        qps = int(cfg.get("QPS") or 0)
    except ValueError:
        qps = 0
    if cli_qps:
        qps = cli_qps
    defaults = ENGINE_DEFAULTS.get(engine, {})
    qps = qps or defaults.get("qps") or DEFAULT_QPS

    if engine not in ENGINE_FLAGS:
        return "siliconflowfree", [], False, qps

    spec = ENGINE_FLAGS[engine]
    args = [spec["flag"]]
    api_key = cfg.get("API_KEY") or ""
    model = cfg.get("MODEL") or defaults.get("model", "")
    if engine == "openaicompatible":
        # 两个走 Magpie 的来源都现读一次模型名：pinned 读 Magpie 写进来的
        # 那个，magpie 跟随 Magpie 当前给 agent 用的那个。
        try:
            import model_source
            model = model_source.resolve_model(cfg)[1] or model
        except Exception as exc:
            logging.getLogger("pdfq").warning("解析模型来源失败，用固定模型：%s", exc)
    base_url = cfg.get("BASE_URL") or defaults.get("base_url", "")
    if api_key:
        args += [spec["key"], api_key]
    if model and spec["model"]:
        args += [spec["model"], model]
    if base_url and spec["base_url"]:
        args += [spec["base_url"], base_url]
    return engine, args, bool(api_key), qps


# ---------------------------------------------------------------- 运行状态
# 只存在于本次进程内；不落盘，所以没有「历史队列」。

PENDING, RUNNING, DONE, FAILED = "pending", "running", "done", "failed"


def target_of(pdf: Path) -> Path:
    stem = pdf.stem
    if stem.endswith(OUTPUT_SUFFIX[: -len(".pdf")]):
        stem = stem[: -len(OUTPUT_SUFFIX[: -len(".pdf")])]
    return pdf.with_name(stem + OUTPUT_SUFFIX)


def unique_target(pdf: Path) -> Path:
    """给这一轮的产物挑一个不覆盖已有译本的名字。

    用户在译本上加过批注（批注存在阅读器自己的库里，按页码定位），覆盖同名
    文件会让高亮和手写全部错位。所以重翻一律另存：``_中文.pdf`` 已存在时用
    ``_中文_v2.pdf``，再存在就 ``_v3``……旧文件一个字节都不动。
    """
    target = target_of(pdf)
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for i in range(2, 1000):
        alt = target.with_name("%s_v%d%s" % (stem, i, suffix))
        if not alt.exists():
            return alt
    return target.with_name("%s_v%d%s" % (stem, int(time.time()), suffix))


def collect_inputs(paths):
    """展开目录，只收 PDF；保持命令行给出的顺序。

    要排掉 babeldoc 自己的中间产物：``foo.zh.mono.pdf``、``foo.zh.dual.pdf``、
    ``foo.no_watermark.zh.mono.pdf`` 的扩展名同样是 .pdf，扫目录时会被当成
    源文件再翻一遍——旧版界面就吃过这个亏，源目录里留了一堆中间稿。
    """
    found, seen = [], set()
    junk_markers = (".zh.mono", ".zh.dual", ".zh.glossary", ".no_watermark",
                    ".decompressed")

    def push(p):
        p = p.resolve()
        key = str(p).lower()
        if not p.is_file() or p.suffix.lower() != ".pdf":
            return
        stem = p.stem.lower()
        if key in seen:
            return
        if stem.endswith(OUTPUT_SUFFIX[: -len(".pdf")]) or any(m in stem for m in junk_markers):
            return  # 译本或中间稿，别再翻一遍
        seen.add(key)
        found.append(p)

    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            for item in sorted(p.rglob("*.pdf")):
                push(item)
        else:
            push(p)
    return found


# ---------------------------------------------------------------- 自适应限流

class Throttle:
    """AIMD 限流：429 多就降，稳住了再慢慢加回来。

    为什么需要它：服务端 /config 说 qps=10，但代理背后的免费模型额度远没有
    这么高。旧日志里 1198 条 RateLimitError 说明大部分请求在撞墙——而
    tenacity 的退避是 min 4 秒、指数涨到 120 秒、每篇最多重试 100 次，于是
    大量时间花在睡觉而不是翻译。

    降低在飞请求数反而能提高吞吐：少发一点、全都成功，比狂发一半被拒快。
    """

    def __init__(self, start, lowest=1, adapt=True):
        self.ceiling = max(1, start)
        # 保守起步：服务端公布的 qps 是"上限"不是"承诺"，免费代理背后还有一层
        # 共享额度。从上限起步意味着第一批请求必然撞墙，而撞墙的代价极大——
        # tenacity 的退避起步 4 秒、最高 120 秒，一个请求重试几次就是一分多钟。
        # 从 3 起步、稳住了再往上加，远比"先冲再退"划算。
        self.current = min(3, self.ceiling)
        self.lowest = max(1, min(lowest, self.ceiling))
        self.adapt = adapt
        self.ok = 0
        self.hit = 0
        self._lock = threading.Lock()
        self._limiters = []
        self._last_judge = time.monotonic()
        self._good_streak = 0
        self.changed_at = 0.0
        self.log = None

    def attach(self, limiter):
        """记下当前翻译器用的漏桶，改了 qps 才能立刻生效。

        注意时序：``get_translator`` 在构造翻译器时就会拿服务端公布的 qps
        （10）去 ``set_max_qps``，把我们在配置里写的值顶掉。所以这一步必须
        发生在 config 构造**之后**，并且要立刻把限流器按保守值重新压一遍。
        """
        if limiter is None:
            return
        with self._lock:
            if not any(l is limiter for l in self._limiters):
                self._limiters.append(limiter)
                self._limiters = self._limiters[-4:]   # 只留最近几个，别越攒越多
        self._apply(self.current)
        if self.log:
            self.log.info("限流器接管：服务端建议 %s，实际按 %s 起（自适应）",
                          self.ceiling, self.current)

    def note(self, is_429):
        with self._lock:
            if is_429:
                self.hit += 1
            else:
                self.ok += 1

    def _apply(self, value):
        for limiter in list(self._limiters):
            try:
                limiter.set_max_qps(int(value))
            except Exception:
                pass

    def review(self):
        """每 3 秒看一次战况，决定要不要调速。返回 (旧值, 新值) 或 None。"""
        if not self.adapt:
            return None
        now = time.monotonic()
        with self._lock:
            if now - self._last_judge < 3.0:
                return None
            ok, hit = self.ok, self.hit
            self.ok = self.hit = 0
            self._last_judge = now
        total = ok + hit
        if total < 8:
            return None
        ratio = hit / total
        old = self.current
        if ratio > 0.25 and self.current > self.lowest:
            # 撞墙超过四分之一：乘性降速
            self.current = max(self.lowest, int(self.current * 0.7) or self.lowest)
            self._good_streak = 0
        elif ratio < 0.03 and self.current < self.ceiling:
            # 连续两轮几乎没撞墙：加性提速
            self._good_streak += 1
            if self._good_streak >= 2:
                self.current = min(self.ceiling, self.current + 1)
                self._good_streak = 0
        else:
            self._good_streak = 0
        if self.current != old:
            self._apply(self.current)
            self.changed_at = time.time()
            return old, self.current
        return None


THROTTLE = None


# ---------------------------------------------------------------- 引擎补丁


def patch_engine(settings, qps, log):
    """把并发钉死在 babeldoc 配置上，并砍掉无谓的阶段。

    三件事：

    1. **主进程内翻译**
       pdf2zh_next 默认把翻译丢进 multiprocessing 子进程（Windows 上是
       spawn）：子进程要重新 import babeldoc、重新加载 ONNX 版面模型，而且
       父进程挂的补丁在子进程里全部失效。``settings.basic.debug = True``
       会让上游走同进程分支（high_level.py:629），代价是它顺手把
       ``config.debug`` 也设成 True、于是往临时目录写一堆 json，所以构造完
       配置后立刻压回 False。

    2. **并发锁定**
       ``translator/utils.py:get_translator`` 拿到翻译器后会无条件用服务端
       推荐值覆盖 ``settings.translation.qps`` 与 ``pool_max_workers``。
       在配置层再钉一次，并把握手到的漏桶交给 Throttle，自适应调速才能生效。

    3. **模型单例**
       ``DocLayoutModel.load_available()`` 每次都 ``load_onnx()``，没有任何
       缓存。每篇都要重新吃一遍模型加载；这里换成进程内单例。

    术语抽取也在这一层关掉：它是拿全文再跑一遍 LLM，旧日志实测 4 分 12 秒
    占整篇 13%，而用户要的只是译文。
    """
    import babeldoc.docvision.doclayout as doclayout
    import pdf2zh_next.high_level as hl

    pool = min(POOL_CAP, max(qps, qps * POOL_FACTOR))
    term_pool = max(1, min(qps, pool))

    # --- 模型单例：跨文件复用同一个 ONNX session
    if not getattr(doclayout.DocLayoutModel.load_available, "_pdfq_cached", False):
        original_load = doclayout.DocLayoutModel.load_available
        holder = {}

        def cached_load():
            if "model" not in holder:
                tick = time.time()
                holder["model"] = original_load()
                log.info("版面模型首次加载（%.1f 秒），之后复用", time.time() - tick)
            return holder["model"]

        cached_load._pdfq_cached = True
        doclayout.DocLayoutModel.load_available = staticmethod(cached_load)

    # --- 接口探测只做一次
    # get_fast_service() 会对两个代理各发 3 次 /check 测延迟，每篇都来一遍。
    # 实测这一项单独就能吃掉 10–20 秒，八篇就是两分多钟白等。
    try:
        from pdf2zh_next.translator.translator_impl.siliconflowfree import (
            SiliconFlowFreeTranslator,
        )

        if not getattr(SiliconFlowFreeTranslator.get_fast_service, "_pdfq_once", False):
            original_fast = SiliconFlowFreeTranslator.get_fast_service
            picked = {}

            def once(self):
                if "url" in picked:
                    self.url = picked["url"]
                    return self.url
                result = original_fast(self)
                picked["url"] = self.url
                log.info("接口探测完成，选用 %s（之后复用）", self.url)
                return result

            once._pdfq_once = True
            SiliconFlowFreeTranslator.get_fast_service = once
    except Exception as exc:
        log.warning("跳过接口探测缓存：%s", exc)

    # --- 配置包装
    if getattr(hl.create_babeldoc_config, "_pdfq_patched", False):
        log.info("并发锁定 qps=%s / 线程池=%s", qps, pool)
        return pool

    original_create = hl.create_babeldoc_config

    def patched_create(s, file):
        config = original_create(s, file)
        for name, value in (
            ("debug", False),                     # 别写 json 调试文件
            ("auto_extract_glossary", False),     # 术语抽取 = 全文再来一遍
            ("save_auto_extracted_glossary", False),
            ("qps", qps),
            ("pool_max_workers", pool),
            ("term_pool_max_workers", term_pool),
            ("use_rich_pbar", False),             # 别让它往终端画进度条
        ):
            try:
                setattr(config, name, value)
            except Exception:
                pass
        # 把漏桶交给 Throttle，后面才能动态调速
        try:
            if THROTTLE is not None:
                THROTTLE.attach(getattr(getattr(config, "translator", None),
                                        "rate_limiter", None))
        except Exception:
            pass
        return config

    patched_create._pdfq_patched = True
    hl.create_babeldoc_config = patched_create
    log.info("并发锁定 qps=%s / 线程池=%s / 术语池=%s", qps, pool, term_pool)
    return pool


def seed_assets(log):
    """把随包附带的模型与字体播种到 babeldoc 的缓存目录。

    为什么必须自己播种：babeldoc 的缓存路径在 ``babeldoc/const.py`` 里写死成
    ``Path.home() / ".cache" / "babeldoc"``，没有任何环境变量能改。所以"便携"
    只能反过来做——首次运行时把 ``models/`` 里的内容复制过去，之后就不用再
    联网下载那 700MB。少了这一步，``models/`` 就是 413MB 的死数据：拷到新机器
    上它不会被读，程序照样去网上重新下一遍。

    逐个文件补，缺什么拷什么，已存在的一律不覆盖。这里原本是按目录判断
    （``CACHE_FOLDER / name`` 存在就整块跳过），结果是只要 ``models/`` 目录
    在，里面少一个 ``ch_PP-OCRv4_det_infer.onnx`` 也永远不会补——babeldoc 每
    次启动都去 modelscope 拉这个文件，那边早就 404 了，于是每次翻译前先糊一屏
    红色 traceback、再白等十几秒。按文件粒度补齐才能自愈。
    """
    try:
        from babeldoc.const import CACHE_FOLDER
    except Exception as exc:
        log.warning("读不到 babeldoc 缓存目录，跳播种：%s", exc)
        return

    if not SEED_DIR.is_dir():
        return

    tick = time.time()
    copied = 0
    for name in SEED_PARTS:
        source = SEED_DIR / name
        if not source.exists():
            continue
        dest_root = CACHE_FOLDER / name
        try:
            if source.is_file():
                if not dest_root.exists():
                    dest_root.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, dest_root)
                    copied += 1
                continue
            for root, _dirs, files in os.walk(source):
                dest_dir = dest_root / Path(root).relative_to(source)
                for filename in files:
                    dest = dest_dir / filename
                    if dest.exists():
                        continue
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(Path(root) / filename, dest)
                    copied += 1
        except OSError as exc:
            log.warning("播种 %s 失败（可以联网时会重新下载）：%s", name, exc)
    if copied:
        log.info("已从 models/ 播种 %d 个文件到缓存（%.1f 秒）",
                 copied, time.time() - tick)


def install_http_retry(attempts=4, base=0.35, cap=4.0):
    """把 429 / 5xx 的退避从 tenacity 手里抢回来。

    这一步是整篇提速的关键。siliconflowfree 自带的策略是
    ``stop_after_attempt(100)`` + ``wait_exponential(min=4, max=120)``：
    撞一次墙睡 4 秒，多撞几次就睡到 120 秒。旧日志里 1198 条重试、32 分钟
    里有二十多分钟就是这么睡掉的——真正的翻译时间反而很少。

    这里在 HTTP 层做短退避（0.35 → 4 秒封顶，共 4 次），瞬时抖动当场救回
    来；真撞死了才把 429 原样交还给上层，让原来的逻辑继续处理。注意必须
    保留 429 的响应对象，不能吞掉——上层要靠状态码判断是限流还是成功。

    顺带把每一次响应喂给 Throttle，自适应调速才有数据可依。
    """
    try:
        import httpx
    except Exception:
        return
    original = httpx.Client.post
    if getattr(original, "_pdfq_retry", False):
        return

    def post(self, url, *args, **kwargs):
        if "chatproxy" not in str(url):
            return original(self, url, *args, **kwargs)
        delay = base
        response = None
        for attempt in range(attempts):
            response = original(self, url, *args, **kwargs)
            code = response.status_code
            if code != 429 and not (500 <= code < 600):
                if THROTTLE is not None:
                    THROTTLE.note(False)
                return response
            if THROTTLE is not None:
                THROTTLE.note(True)
            if attempt == attempts - 1:
                return response          # 交还给上层，它会自己记日志/重试
            time.sleep(delay + random.uniform(0, delay * 0.3))
            delay = min(delay * 2, cap)
        return response

    post._pdfq_retry = True
    httpx.Client.post = post


def force_no_watermark(settings, log):
    """无论参数怎么传，译本都不带那行 funstory.ai 的尾巴。"""
    try:
        settings.pdf.watermark_output_mode = "no_watermark"
        return
    except Exception:
        pass
    try:
        object.__setattr__(settings.pdf, "watermark_output_mode", "no_watermark")
    except Exception as exc:
        log.warning("无法强制关闭水印：%s", exc)


# ---------------------------------------------------------------- 产物收尾

_ARTIFACT_TAILS = (
    ".zh.mono.pdf", ".zh.dual.pdf", ".zh.glossary.csv", ".zh.log", ".decompressed.pdf",
)


def cleanup_siblings(pdf: Path, keep: Path):
    """删掉 babeldoc 留下的中间稿，只留发布出去的那一份。

    不清的话 PDF 阅读器会把 ``foo.zh.mono.pdf`` 也列出来，用户迟早会点错。
    """
    removed = 0
    base = pdf.stem
    try:
        entries = list(pdf.parent.iterdir())
    except OSError:
        return 0
    for item in entries:
        if not item.is_file() or item == keep:
            continue
        name = item.name
        if not name.startswith(base + "."):
            continue
        tail = name[len(base):].lower()
        if not (tail.endswith(_ARTIFACT_TAILS) or ".no_watermark." in tail):
            continue
        try:
            item.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def scrub_metadata(pdf: Path, translated: Path, log, title: str = ""):
    """抹掉译本属性里的 BabelDOC/AI 标记。

    babeldoc 会把 producer 改成 ``BabelDOCv..._Translation_generated_by_AI,
    please_carefully_discern``。它看不见，但露在「文件属性」里，而且 babeldoc
    下次读到这个字段会拒绝再翻译一次。
    """
    try:
        import fitz
    except Exception:
        return
    try:
        with fitz.open(str(pdf)) as src:
            source_meta = src.metadata or {}
    except Exception:
        source_meta = {}
    try:
        with fitz.open(str(translated)) as doc:
            meta = dict(doc.metadata or {})
            marks = ("BabelDOC", "Translation_generated_by_AI")
            producer = meta.get("producer") or ""
            creator = meta.get("creator") or ""
            meta["title"] = title or target_of(pdf).stem
            if any(m in producer for m in marks):
                meta["producer"] = (source_meta.get("producer") or "").strip()
            if any(m in creator for m in marks):
                meta["creator"] = (source_meta.get("creator") or "").strip()
            doc.set_metadata(meta)
            doc.saveIncr()
    except Exception as exc:
        log.warning("清除译本属性失败（不影响阅读）：%s", exc)


def subset_fonts(mono: Path, staging: Path, log) -> bool:
    """把整包内嵌的中文字体压成子集，体积通常掉到 5% 左右。

    为什么必须自己补这一刀：BASE_ARGS 里的 ``--skip-clean`` 会让 babeldoc
    跳过它自己的 ``subset_fonts``（pdf_creater.py: 只有 not skip_clean 才
    调用），于是排版时用的整份 TTF 原封不动进了 PDF——LXGW WenKai 24MB、
    思源宋体 Regular/Bold 各 14MB，一份 600KB 的论文译文能涨到 32MB，
    其中 95% 以上是没人读的字形。

    子集化发生在排版完成之后，只删用不到的字形、重排字形编号并同步改正文
    引用，不动文字内容和坐标，所以渲染逐像素一致。唯一副作用是：以后用
    PDF 编辑器往这份文件里**新增**文字时，缺字可能显示为方框（纯阅读、
    搜索、复制、打印、批注都不受影响）。

    失败时降级为原样输出：体积偏大总比发布不出来好。
    """
    import fitz
    try:
        with fitz.open(str(mono)) as doc:
            doc.subset_fonts(fallback=False)
            doc.save(
                str(staging),
                garbage=3,       # 清掉被替换掉的原始字体对象
                deflate=True,
                deflate_fonts=True,
                clean=True,
            )
    except Exception as exc:
        log.warning("字体子集化失败，按原样输出（体积会偏大）：%s", exc)
        return False

    try:
        before = mono.stat().st_size
        after = staging.stat().st_size
        if before > 0 and after < before:
            log.info(
                "字体子集化：%.1f MB → %.1f MB", before / 1048576, after / 1048576,
            )
    except OSError:
        pass
    return True


def publish(pdf: Path, mono: Path, log) -> Path:
    """校验、改名、发布到源文件旁边。"""
    import fitz
    with fitz.open(str(mono)) as doc:
        if doc.page_count < 1 or doc.is_encrypted:
            raise RuntimeError("译本 PDF 无法正常读取")

    target = unique_target(pdf)
    staging = target.with_name(".pdfq-" + target.name + ".tmp")
    try:
        if not subset_fonts(mono, staging, log):
            shutil.copyfile(mono, staging)
        scrub_metadata(pdf, staging, log, title=target.stem)
        os.replace(str(staging), str(target))   # 原子替换
    finally:
        try:
            staging.unlink()
        except OSError:
            pass
    log.info("已保存：%s", target)
    try:
        removed = cleanup_siblings(pdf, keep=target)
        if removed:
            log.info("清理中间文件 %d 个", removed)
    except Exception as exc:
        log.warning("清理中间文件失败：%s", exc)
    return target


# ---------------------------------------------------------------- 进度显示

STAGE_ZH = (
    ("DetectScannedFile", "检测扫描件"),
    ("Parse PDF", "解析 PDF"),
    ("Automatic Term Extraction", "提取术语"),
    ("Parse Page Layout", "版面分析"),
    ("Parse Paragraphs", "识别段落"),
    ("Parse Formulas", "解析公式"),
    ("Remove Char Descent", "字形微调"),
    ("Parse Table", "识别表格"),
    ("Translate Paragraphs", "翻译段落"),
    ("Add Fonts", "嵌入字体"),
    ("Typesetting", "重新排版"),
    ("Generate drawing", "生成指令"),
    ("Subset font", "字体子集"),
    ("Save PDF", "保存输出"),
    ("Add Debug", "调试信息"),
)


def stage_zh(name):
    if not name:
        return ""
    for prefix, zh in STAGE_ZH:
        if name.startswith(prefix):
            return zh
    return name


class Progress:
    """单行原地刷新的进度条。

    不用全屏 TUI：那些要么吞掉终端历史，要么在各种 Windows 终端里错位。
    这里只在最后一行用 \\r 覆盖，完成的文件各占一行——终端历史本身就是日志。
    """

    def __init__(self, index, total, name, is_tty):
        self.index = index
        self.total = total
        self.name = name
        self.is_tty = is_tty
        self.started = time.time()
        self.last_draw = 0.0
        self.last_pct = -1
        self.stage = ""
        self.width = term_width()
        self._drawn = False

    def update(self, pct, stage="", cur=None, tot=None, force=False):
        now = time.time()
        if not force and now - self.last_draw < 0.35:
            return
        self.last_draw = now
        zh = stage_zh(stage) or self.stage or "准备中"
        self.stage = zh

        elapsed = now - self.started
        eta = ""
        if pct and pct > 3 and elapsed > 5:
            eta = "  ETA " + fmt_dur(elapsed * (100.0 - pct) / pct)

        detail = ""
        if cur is not None and tot:
            detail = "  %s/%s" % (cur, tot)

        if not self.is_tty:
            # 重定向到文件时别刷屏，每 10% 打一行
            bucket = int((pct or 0) // 10)
            if bucket != self.last_pct:
                self.last_pct = bucket
                print("  [%d/%d] %s%%  %s%s" % (self.index, self.total,
                                                 int(pct or 0), zh, detail), flush=True)
            return

        label = short_name(self.name, max(16, self.width - 52))
        barw = max(10, min(28, self.width - 46 - len(label)))
        line = "  %s %s %3d%% %s %s%s%s" % (
            c("[%d/%d]" % (self.index, self.total), "grey"),
            bar(pct, barw), int(pct or 0),
            c(label, "bold"), zh, detail, eta,
        )
        sys.stdout.write("\r" + line[: self.width - 1].ljust(self.width - 1))
        sys.stdout.flush()
        self._drawn = True

    def clear(self):
        if self.is_tty and self._drawn:
            sys.stdout.write("\r" + " " * (self.width - 1) + "\r")
            sys.stdout.flush()
            self._drawn = False


# ---------------------------------------------------------------- 翻译一篇


class JobResult:
    def __init__(self, state, seconds=0.0, error=""):
        self.state = state
        self.seconds = seconds
        self.error = error


def purge_working(pdf, log):
    """删掉这篇在缓存里留下的工作副本。

    babeldoc 会给每篇在 ``<cache>/working/<去掉扩展名的文件名>/`` 下放一份完整
    的源 PDF 拷贝（十几 MB 的论文就是十几 MB）再加一个进度 json，翻完之后没有任何
    地方再读它。不清就会一直涨：翻一百篇能白占一两个 G。
    ``working`` 目录在工具目录之外（~/.cache/babeldoc，本机是 D:\\Tools\\babeldoc-cache），
    所以文件管理器里根本看不见，属于典型的"悄悄长大"。

    只在产物已经落地之后调用——中途中断的那篇要留着进度，下次好接着跑。
    """
    try:
        from babeldoc.const import CACHE_FOLDER
    except Exception:
        return
    folder = Path(CACHE_FOLDER) / "working" / pdf.stem
    if not folder.is_dir():
        return
    try:
        shutil.rmtree(folder)
        log.info("已清理工作副本 working/%s", folder.name)
    except OSError as exc:
        log.warning("清理工作副本失败（不影响结果）：%s", exc)


def translate_one(pdf, settings, engine_args, log, progress):
    """跑完一篇，返回 (产出路径, 耗时)。异常直接抛给上层。"""
    import pdf2zh_next.high_level as hl

    started = time.time()
    mono = None
    last_stage = None

    async def consume():
        nonlocal mono, last_stage
        async for event in hl.do_translate_async_stream(settings, pdf):
            kind = event.get("type")
            if kind == "finish":
                result = event.get("translate_result")
                if result is not None:
                    mono = (getattr(result, "mono_pdf_path", None)
                            or getattr(result, "no_watermark_mono_pdf_path", None))
                continue
            if kind == "error":
                log.error("引擎报错：%s", event.get("error"))
                continue
            if kind not in ("progress_start", "progress_update", "progress_end"):
                continue
            stage = event.get("stage") or ""
            if stage != last_stage:
                log.info("阶段：%s", stage)
                last_stage = stage
            progress.update(event.get("overall_progress"), stage,
                            event.get("stage_current"), event.get("stage_total"),
                            force=(kind != "progress_update"))

    asyncio.run(consume())

    if mono is None:
        # 兜底：按 babeldoc 的命名规则找一遍
        try:
            out_dir = settings.get_output_dir()
        except Exception:
            out_dir = pdf.parent
        for name in (pdf.stem + ".zh.mono.pdf",
                     pdf.stem + ".no_watermark.zh.mono.pdf"):
            candidate = Path(out_dir) / name
            if candidate.is_file():
                mono = candidate
                break
    if mono is None or not Path(mono).is_file():
        raise RuntimeError("翻译结束但没有生成中文版 PDF（多为接口限流，详见日志）")

    target = publish(pdf, Path(mono), log)
    purge_working(pdf, log)
    return target, time.time() - started


# ---------------------------------------------------------------- 执行


def run_queue(jobs, args, engine_name, engine_args, qps, has_key):
    global THROTTLE

    log = logging.getLogger("pdfq")

    pending = jobs
    if not pending:
        print("没有要翻的文件。把 PDF 拖到快捷方式上，或 `pdfq a.pdf`。")
        return 0

    total = len(pending)
    print()
    print("  %s  %s 个任务 · %s · %s" % (
        c("PDF 翻译", "bold"),
        total,
        engine_name if has_key else engine_name + "（免费代理）",
        "并发 %d" % qps,
    ))

    # 开跑前顺手清掉上一轮留下的工作副本和过期日志。放在这里而不是退出时清，
    # 是因为被 Ctrl+C 或断电打断时根本没有"退出时"。排队中的任务不会被清。
    try:
        import cleanup as _cleanup

        _cleanup.auto_clean(jobs, log)
    except Exception:
        pass
    if not has_key and engine_name != "siliconflowfree":
        print("  %s" % c("注意：这个引擎没配 API_KEY，很可能直接失败。"
                        "运行 `pdfq config` 填上。", "yellow"))
    print()

    ok = failed = 0
    started_all = time.time()

    # 让调速线程随时知道当前在刷哪一行，调完速好把进度行重新画回去
    live = {"p": None}
    start_throttle_watch(lambda: live.get("p"))

    for index, job in enumerate(pending, 1):
        live["p"] = None
        path = Path(job["path"])
        if not path.is_file():
            job["state"] = FAILED
            job["error"] = "文件不存在"
            failed += 1
            print("  %s %s  %s" % (c("✗", "red"), path.name, c("文件不存在", "red")))
            continue

        job["state"] = RUNNING
        job["attempts"] = 0

        progress = Progress(index, total, path.name, sys.stdout.isatty())
        live["p"] = progress
        progress.update(0, "准备中", force=True)
        done = None
        last_error = ""
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_file = LOG_DIR / (safe_name(path.name) + ".log")
        file_log = open_log(log_file, reset=True)

        while job["attempts"] < MAX_ATTEMPTS:
            job["attempts"] += 1
            if job["attempts"] > 1:
                progress.clear()
                wait = RETRY_WAIT * (job["attempts"] - 1)
                print("  %s %s 第 %d 次重试，%d 秒后开始"
                      % (c("↻", "yellow"), path.name, job["attempts"], wait))
                time.sleep(wait)

            try:
                settings = build_settings(path, engine_args, qps)
                progress.started = time.time()
                log.info("第 %d 次尝试：%s", job["attempts"], path)
                done, seconds = translate_one(path, settings, engine_args, log, progress)
                job["state"] = DONE
                job["seconds"] = seconds
                job["error"] = ""
                job["at"] = time.time()
                break
            except KeyboardInterrupt:
                job["state"] = FAILED
                job["error"] = "已中断"
                raise
            except Exception as exc:
                last_error = (str(exc).strip() or exc.__class__.__name__)[:300]
                log.error("失败：%s", last_error)
                log.error("%s", traceback.format_exc())
                job["error"] = last_error

        close_log(file_log)

        progress.clear()
        live["p"] = None

        if job["state"] == DONE and done is not None:
            ok += 1
            print("  %s %s  %s" % (c("✓", "green"), path.name,
                                   c(fmt_dur(job["seconds"]), "green")))
        else:
            failed += 1
            if job["state"] != DONE:
                job["state"] = FAILED
            print("  %s %s  %s" % (c("✗", "red"), path.name, c(last_error or "失败", "red")))

    elapsed = time.time() - started_all
    print()
    parts = []
    if ok:
        parts.append(c("%d 成功" % ok, "green"))
    if failed:
        parts.append(c("%d 失败" % failed, "red"))
    print("  %s   总用时 %s" % ("  ·  ".join(parts), fmt_dur(elapsed)))
    if failed:
        print("  %s" % c("失败的文件重新拖进来再跑一次；日志在：" + str(LOG_DIR), "grey"))
    print()
    return 0 if failed == 0 else 1


def start_throttle_watch(get_progress):
    """后台盯着 429 率。

    只在每篇结束时评估是不够的：一篇 130 页的论文要跑二十几分钟，调速必须
    发生在翻译过程中才有意义。
    """
    if THROTTLE is None:
        return None

    def loop():
        while True:
            time.sleep(2.0)
            try:
                change = THROTTLE.review()
            except Exception:
                return
            if not change:
                continue
            old, new = change
            progress = get_progress()
            if progress is not None:
                progress.clear()
            print("  %s 并发 %d → %d（跟着 429 情况自动调）"
                  % (c("·", "yellow"), old, new))

    thread = threading.Thread(target=loop, name="throttle", daemon=True)
    thread.start()
    return thread


def safe_name(name):
    return re.sub(r"[^\w.\-]+", "_", name)[:80]


_LOG_HANDLES = []


def open_log(path, reset=True):
    """每篇一个日志文件，纯文本、无 ANSI，出问题时能直接看。

    ``reset`` 只在第一次尝试时为真：重试时如果还按覆盖模式打开，前一次
    撞了什么错就全被冲掉了——排查时最需要看的恰恰是失败那一次。
    """
    global _LOG_HANDLES
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = open(path, "w" if reset else "a", encoding="utf-8",
                  errors="replace", buffering=1)

    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_pdfq", False):
            root.removeHandler(handler)
    handler = logging.StreamHandler(stream)
    handler._pdfq = True
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    for noisy in ("httpx", "httpcore", "openai", "http11", "pdfminer", "peewee",
                  "urllib3", "asyncio", "PIL", "matplotlib", "fontTools"):
        lg = logging.getLogger(noisy)
        lg.setLevel(logging.WARNING)
        lg.propagate = False

    _LOG_HANDLES.append(stream)
    return stream


def close_log(stream):
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_pdfq", False):
            root.removeHandler(handler)
            try:
                handler.flush()
            except Exception:
                pass
    try:
        stream.close()
    except Exception:
        pass
    if stream in _LOG_HANDLES:
        _LOG_HANDLES.remove(stream)


def build_settings(pdf: Path, engine_args, qps):
    """为这一篇造 settings。每篇重建，避免上一篇的状态漏过来。"""
    from pdf2zh_next.config import ConfigManager

    extra = list(BASE_ARGS) + [
        "--qps", str(qps),
        "--pool-max-workers", str(min(POOL_CAP, max(qps, qps * POOL_FACTOR))),
        "--term-qps", str(qps),
        "--term-pool-max-workers", str(qps),
        "--report-interval", "0.5",
    ] + list(engine_args)

    sys.argv = ["pdf2zh-next", str(pdf), "--output", str(pdf.parent)] + extra
    settings = ConfigManager().initialize_config()
    settings.basic.input_files = {str(pdf)}
    force_no_watermark(settings, logging.getLogger("pdfq"))
    try:
        settings.translation.save_auto_extracted_glossary = False
    except Exception:
        pass
    # 主进程内翻译：high_level 在生成器开始时读这个开关
    settings.basic.debug = True
    patch_engine(settings, qps, logging.getLogger("pdfq"))
    return settings


# ---------------------------------------------------------------- 子命令


def cmd_config(args):
    cfg = read_config()
    touched = False
    for key, value in (("ENGINE", args.engine), ("API_KEY", args.key),
                       ("MODEL", args.model), ("BASE_URL", args.base_url),
                       ("MODEL_SOURCE", args.model_source),
                       ("QPS", str(args.set_qps) if args.set_qps else None)):
        if value is not None:
            cfg[key] = value
            touched = True
    if args.model_source:
        # 来源决定了引擎：切到公共节点就别留着 Magpie 的 ENGINE
        try:
            import model_source
            cfg = model_source.apply_source(cfg, args.model_source)
        except Exception as exc:
            logging.getLogger("pdfq").warning("应用模型来源失败：%s", exc)
    if touched:
        write_config(cfg)
        print("  已写入 %s" % CONFIG_FILE)

    cfg = read_config()
    engine, _, has_key, qps = resolve_engine()
    print()
    print("  配置文件  %s" % CONFIG_FILE)
    print("  引擎      %s%s" % (engine, "" if has_key or engine == "siliconflowfree"
                                 else c("  （缺 API_KEY）", "yellow")))
    try:
        import model_source
        source = model_source.source_of(cfg)
        print("  模型来源  %s" % model_source.label_of(source))
        if source != model_source.SOURCE_PUBLIC:
            _source, model, note = model_source.resolve_model(cfg)
            if cfg.get("MODEL"):
                print("  配置模型  %s" % cfg["MODEL"])
            print("  实际使用  %s%s" % (model, c("  （%s）" % note, "grey") if note else ""))
            if cfg.get("BASE_URL"):
                print("  接口地址  %s" % cfg["BASE_URL"])
    except Exception as exc:
        print("  模型来源  解析失败：%s" % exc)
    print("  并发      %s 请求/秒" % qps)
    if engine == "siliconflowfree":
        print()
        print("  %s" % c("公共免费节点的速度上限由服务端决定；想明显更快就在面板上切到"
                        "「在 Magpie 里选定」，或用自己的付费 key。", "grey"))
        print("  %s" % c("pdfq config --model-source pinned --set-qps 32", "grey"))
    print()
    return 0


def cmd_clean(jobs, args):
    """清理临时文件。默认只演练，--force 才真删。

    真正占地方的是 babeldoc 在 ~/.cache/babeldoc/working/ 下给每篇留的完整
    源 PDF 拷贝，那玩意在文件管理器里看不见，翻几十篇就能悄悄涨到几个 G。
    清理逻辑放在 cleanup.py 里，因为 pdfq 开跑前也会自动调一次。
    """
    try:
        import cleanup
    except Exception as exc:
        print("  找不到 cleanup.py：%s" % exc)
        return 1

    items = cleanup.collect(jobs)
    if not items:
        print("  没有需要清理的东西。")
        return 0

    total = sum(size for _label, _path, size in items)
    print()
    print("  可清理 %d 项，共 %s" % (len(items), cleanup.human(total)))
    for label, _path, size in items:
        print("    %-38s %8s" % (short_name(label, 38), cleanup.human(size)))

    if not args.force:
        print()
        print("  演练模式，没有删除。加 --force 真的清。")
        return 0

    freed = cleanup.apply(items)
    print()
    print("  %s 已释放 %s" % (c("✓", "green"), cleanup.human(freed)))
    return 0


def cmd_logs():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    os.startfile(str(LOG_DIR))
    return 0


# ---------------------------------------------------------------- 入口


def build_parser():
    parser = argparse.ArgumentParser(
        prog="pdfq", description="PDF 批量翻译 · 命令行",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""例子：
  pdfq a.pdf b.pdf 论文/     直接翻译这些文件
  pdfq --plan                只列出这轮会翻哪些，不开始
  pdfq config                查看引擎与并发设置
  pdfq logs                  打开日志文件夹
  pdfq clean [--force]       清理临时文件
""")
    parser.add_argument("command", nargs="?", default=None,
                        help="config | logs | clean")
    parser.add_argument("paths", nargs="*", help="PDF 文件或目录")
    parser.add_argument("--qps", type=int, default=None, help="每秒请求数")
    parser.add_argument("--force", action="store_true", help="clean 时真的删除临时文件")
    parser.add_argument("--no-adapt", action="store_true", help="关闭自适应限流")
    parser.add_argument("--no-color", action="store_true", help="关掉颜色")
    parser.add_argument("--plan", action="store_true",
                        help="只列出这一轮会翻哪些文件，不真的翻译")
    parser.add_argument("--engine", default=None, help="config: 设置引擎")
    parser.add_argument("--key", default=None, help="config: 设置 API_KEY")
    parser.add_argument("--model", default=None, help="config: 设置固定模型名")
    parser.add_argument("--model-source", dest="model_source", default=None,
                        choices=["public", "pinned", "magpie"],
                        help="config: 模型来源（public 公共免费 API，pinned 在 Magpie 里选定，magpie 跟随 Magpie 当前模型）")
    parser.add_argument("--base-url", dest="base_url", default=None, help="config: 设置接口地址")
    parser.add_argument("--set-qps", dest="set_qps", type=int, default=None,
                        help="config: 把并发写进配置文件")
    return parser


def main():
    global COLOR, THROTTLE

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = build_parser()
    args = parser.parse_args()
    if args.no_color:
        COLOR = False

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # 第一次运行就把配置文件落下来：光在帮助里写「去改 config.env」，
    # 用户打开文件夹发现根本没有这个文件，反而更迷糊。
    if not CONFIG_FILE.exists():
        try:
            write_config({"ENGINE": "siliconflowfree"})
        except OSError:
            pass
    jobs = []

    cmd = args.command
    # 第一个参数如果不是已知子命令，就当成文件路径
    if cmd and cmd not in ("config", "logs", "clean"):
        args.paths.insert(0, cmd)
        cmd = None

    if cmd == "config":
        return cmd_config(args)
    if cmd == "logs":
        return cmd_logs()
    if cmd == "clean":
        return cmd_clean(jobs, args)

    # ---- 组装本次任务：没有持久化队列，翻完即结束
    if args.paths:
        files = collect_inputs(args.paths)
        if not files:
            print("  %s 没找到可翻的 PDF：%s"
                  % (c("!", "yellow"), " ".join(args.paths)))
            print("     只收 .pdf，而且会跳过已经翻译过的译稿"
                  "（``*_中文.pdf``）和 babeldoc 的中间稿。")
            return 2
        jobs = [{"path": str(f), "state": PENDING, "attempts": 0,
                 "seconds": 0.0, "error": "", "at": 0} for f in files]
        print("  %s 本次 %d 个文件" % (c("+", "green"), len(jobs)))
    else:
        parser.print_help()
        return 0

    if getattr(args, "plan", False):
        print()
        print("  这一轮会翻 %d 个：" % len(jobs))
        for job in jobs:
            print("    %s" % Path(job["path"]).name)
        print()
        return 0

    # ---- 跑
    engine, engine_args, has_key, qps = resolve_engine(args.qps)
    THROTTLE = Throttle(qps, adapt=not args.no_adapt)
    THROTTLE.log = logging.getLogger("pdfq")
    install_http_retry()

    tick = time.time()
    print("  %s 加载翻译引擎…" % c("·", "grey"), end="", flush=True)
    import babeldoc.assets.assets as babeldoc_assets  # noqa: F401
    import pdf2zh_next.high_level  # noqa: F401

    # 缓存在用户目录、不在程序目录：新机器上先把随包的模型和字体播种过去，
    # 否则 warmup 会去网上重新拉 700MB。
    seed_assets(logging.getLogger("pdfq"))

    try:
        babeldoc_assets.warmup()
    except BaseException as exc:
        print("\r  %s 模型检查失败（继续尝试）：%s" % (c("!", "yellow"), exc))
    else:
        print("\r  %s 引擎就绪（%.1f 秒）" % (c("·", "grey"), time.time() - tick))

    try:
        return run_queue(jobs, args, engine, engine_args, qps, has_key)
    except KeyboardInterrupt:
        print()
        print("  已中断。把同一批文件再拖进来就会重新开始。")
        return 130


if __name__ == "__main__":
    try:
        code = main()
    except SystemExit as exc:
        # argparse 的 --help / 参数错误会抛 SystemExit；别把它当成"出错"打 traceback
        code = int(exc.code or 0) if isinstance(exc.code, int) else 0
    except KeyboardInterrupt:
        code = 130
    except BaseException as exc:
        print()
        print("  出错了：%s" % (str(exc) or exc.__class__.__name__))
        traceback.print_exc()
        code = 1
    for stream in list(_LOG_HANDLES):
        try:
            stream.close()
        except Exception:
            pass
    try:
        sys.stdout.flush()
    except Exception:
        pass
    # onnxruntime / torch 会留后台线程，正常 sys.exit 可能卡住，直接退
    os._exit(code)
