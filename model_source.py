# -*- coding: utf-8 -*-
"""模型来源：公共免费 API（默认）、在 Magpie 里选定、跟随 Magpie。

config.env 里用 MODEL_SOURCE 表达用户的选择：

    MODEL_SOURCE=public   默认：公共免费节点（SiliconFlow Free），
                          不需要 Magpie、不需要 API Key、不用挑模型
    MODEL_SOURCE=pinned   在 Magpie 里选定：模型由 Magpie 的 Agents 页面
                          写进 config.env（装上 magpie-plugin 后，PDF Translate
                          就是那一页里的一行），这里照它写的用，不再自己挑
    MODEL_SOURCE=magpie   跟随 Magpie：走本机 Magpie 反代，并且每次运行
                          都读 Magpie 现在给 agent 用的模型

「在 Magpie 里选定」和「跟随 Magpie」的区别：前者钉住你给 PDF 翻译单独选
的模型，不受别的 agent 换模型影响；后者跟着 Magpie 面板上那个「当前模型」
走。两者都走 Magpie 反代，换模型都在 Magpie 里换。

「跟随 Magpie」不是让 Magpie 自己路由（网关只认显式模型名），而是把 Magpie
当前给 agent 用的模型读出来再照抄：Magpie 面板里换模型，这里下次运行自动跟
上。读取顺序：

    1) ~/.config/magpie/applied.json 里最近一次带 model 的应用记录
    2) ~/.pi/agent/settings.json 的 defaultModel
    3) 都读不到就用 config.env 里 MODEL 那行的固定模型

读到的名字还会拿去 /v1/models 校验：名字不在列表里就按裸模型名（去掉
provider/ 前缀）再匹配一次，仍匹配不上就回退固定模型，绝不把不存在的
模型名发给网关。
"""

import json
import os
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULT_MODEL = "trae-cn/deepseek-v4.1-flash-max"
MAGPIE_URL = "http://127.0.0.1:3425/v1"
MAGPIE_KEY = "sk-magpie-key-1cdbbb977588c0599a122cce1803ec5515ee3bafae75f317"
# magpie 的命令行（"panel" 直接把它的窗口打开）
MAGPIE_EXE = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
    "Programs", "magpie", "magpie.exe")

# 三个来源
SOURCE_PUBLIC = "public"     # 默认：公共免费 API
SOURCE_PINNED = "pinned"     # 在 Magpie 里选定：用 config.env 里写死的模型
SOURCE_MAGPIE = "magpie"     # 跟随 Magpie 当前的模型
SOURCES = (SOURCE_PUBLIC, SOURCE_PINNED, SOURCE_MAGPIE)

# 旧配置里的写法照样认
_ALIASES = {
    "": SOURCE_PUBLIC,
    "default": SOURCE_PUBLIC,
    "free": SOURCE_PUBLIC,
    "public": SOURCE_PUBLIC,
    "siliconflowfree": SOURCE_PUBLIC,
    "siliconflow-free": SOURCE_PUBLIC,
    "magpie": SOURCE_MAGPIE,
    "follow": SOURCE_MAGPIE,
    "pinned": SOURCE_PINNED,
    "pin": SOURCE_PINNED,
    "chosen": SOURCE_PINNED,
    "agents": SOURCE_PINNED,
}

# 每个来源对应的引擎
PUBLIC_ENGINE = "siliconflowfree"
MAGPIE_ENGINE = "openaicompatible"

LABELS = {
    SOURCE_PUBLIC: "默认（公共免费 API）",
    SOURCE_PINNED: "在 Magpie 里选定",
    SOURCE_MAGPIE: "跟随 Magpie 当前模型",
}

_CACHE = {"at": 0.0, "key": None, "source": None, "model": None, "note": ""}


# --------------------------------------------------------------------- 基础


def source_of(cfg):
    """把配置里的 MODEL_SOURCE 归一成 public / pinned / magpie。"""
    raw = (cfg.get("MODEL_SOURCE") or "").strip().lower()
    return _ALIASES.get(raw, SOURCE_PUBLIC)


def label_of(source):
    return LABELS.get(source, LABELS[SOURCE_PUBLIC])


def engine_of(source):
    """两个走 Magpie 反代的来源都使 openaicompatible。"""
    return PUBLIC_ENGINE if source == SOURCE_PUBLIC else MAGPIE_ENGINE


def apply_source(cfg, source):
    """把某个来源写进配置字典（不改文件），返回新的 dict。

    只定 MODEL_SOURCE 与跟它对应的 ENGINE，其余字段原样留着：切回
    「跟随 Magpie」时还用得上原来的 MODEL / BASE_URL，不用重新填。
    """
    out = dict(cfg)
    source = source if source in SOURCES else SOURCE_PUBLIC
    out["MODEL_SOURCE"] = source
    out["ENGINE"] = engine_of(source)
    if source in (SOURCE_PINNED, SOURCE_MAGPIE):
        out["API_KEY"] = out.get("API_KEY") or MAGPIE_KEY
        out["BASE_URL"] = out.get("BASE_URL") or MAGPIE_URL
        out["MODEL"] = out.get("MODEL") or DEFAULT_MODEL
    return out


def normalized(cfg):
    """手改过的 config.env 也按 MODEL_SOURCE 归一，避免 ENGINE 与它打架。"""
    return apply_source(cfg, source_of(cfg))


# --------------------------------------------------------------------- 网关


def magpie_models(timeout=2.5):
    """拉网关的实时模型列表；离线返回 None。"""
    req = urllib.request.Request(
        MAGPIE_URL.rstrip("/") + "/models",
        headers={"Authorization": "Bearer " + MAGPIE_KEY},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    ids = []
    for item in payload.get("data") or []:
        mid = (item.get("id") or "").strip()
        if mid:
            ids.append(mid)
    return ids or None


def _strip_magpie(name):
    name = (name or "").strip()
    if name.lower().startswith("magpie/"):
        name = name[len("magpie/"):]
    return name


# ------------------------------------------------------- 读 Magpie 的当前模型


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return None


def _magpie_config_dir():
    home = os.path.expanduser("~")
    for path in (
        os.path.join(home, ".config", "magpie"),
        os.path.join(os.environ.get("APPDATA") or "", "magpie"),
    ):
        if os.path.isdir(path):
            return path
    return os.path.join(home, ".config", "magpie")


# 这个 agent 在 magpie 里的 id：applied.json 里它自己那一条不能算作
# 「Magpie 当前给 agent 用的模型」，否则「跟随 Magpie」会变成自己跟自己。
SELF_AGENT_ID = "pdf-translate"
SELF_AGENT_ALIASES = (SELF_AGENT_ID, "pdftranslate", "pdfq")


def magpie_applied_model():
    """Magpie 最近一次应用给别的 agent 的模型（去掉 magpie/ 前缀）。

    applied.json 是「每个 agent 当前被写进去什么」的快照，含本工具自己
    （pdf-translate）那一行。自己那一行必须跳过：它不是用户对「当前模型」
    的选择，只是上一次给 PDF 翻译选的那个，拿它当「跟随目标」就变成自己
    跟自己了。
    """
    data = _read_json(os.path.join(_magpie_config_dir(), "applied.json"))
    if not isinstance(data, dict):
        return ""
    best, best_at = "", ""
    for agent_id, entry in data.items():
        if not isinstance(entry, dict):
            continue
        if str(agent_id).strip().lower() in SELF_AGENT_ALIASES:
            continue
        fields = entry.get("fields") or {}
        model = _strip_magpie(fields.get("model"))
        at = entry.get("at") or ""
        if model and at >= best_at:
            best, best_at = model, at
    return best


def pi_default_model():
    """Pi 自己的默认模型（Magpie 写进去的那份配置）。"""
    home = os.path.expanduser("~")
    data = _read_json(os.path.join(home, ".pi", "agent", "settings.json"))
    if isinstance(data, dict):
        return _strip_magpie(data.get("defaultModel"))
    return ""


def raw_magpie_model():
    """按优先级读出「Magpie 现在用的模型」，读不到返回空串。"""
    for getter in (magpie_applied_model, pi_default_model):
        try:
            model = getter()
        except Exception:
            model = ""
        if model:
            return model
    return ""


def _match_live(name, live):
    """把 name 对上网关列表里的真实 id；对不上返回空串。"""
    if name in live:
        return name
    bare = name.split("/", 1)[-1]
    for mid in live:
        if mid.split("/", 1)[-1] == bare:
            return mid
    return ""


# ------------------------------------------------------------------- 对外接口


def resolve_model(cfg, timeout=2.5):
    """返回 (来源, 真正要用的模型名, 说明文字)。

    来源是 SOURCE_PUBLIC 时模型名为空（公共节点不用选模型）。来源是
    SOURCE_PINNED 时用 config.env 里的 MODEL（Magpie 的 Agents 页面写的就是
    它），只校验它还在网关列表里。来源是 SOURCE_MAGPIE 时按上面的规则跟随。
    """
    source = source_of(cfg)
    fixed = (cfg.get("MODEL") or "").strip() or DEFAULT_MODEL

    if source == SOURCE_PUBLIC:
        return source, "", "公共免费节点，不需要选模型"

    if source == SOURCE_PINNED:
        live = magpie_models(timeout=timeout)
        if live is None:
            return source, fixed, "Magpie 网关离线，仍用选定的 %s" % fixed
        matched = _match_live(fixed, live)
        if not matched:
            return source, fixed, "选定的 %s 不在网关列表里，仍照它发" % fixed
        return source, matched, "在 Magpie 里选定"

    wanted = raw_magpie_model()
    if not wanted:
        return source, fixed, "读不到 Magpie 的当前模型，已回退固定模型 %s" % fixed

    live = magpie_models(timeout=timeout)
    if live is None:
        return source, fixed, "Magpie 网关离线，已回退固定模型 %s" % fixed

    matched = _match_live(wanted, live)
    if not matched:
        return source, fixed, "Magpie 当前模型 %s 不在网关列表里，已回退固定模型 %s" % (wanted, fixed)
    if matched != wanted:
        return source, matched, "跟随 Magpie（%s 对上了网关里的 %s）" % (wanted, matched)
    return source, matched, "跟随 Magpie"


def resolve_model_cached(cfg, ttl=20.0):
    """给界面用：短时间内重复刷新不要每次都打网关。"""
    import time

    key = (source_of(cfg), cfg.get("MODEL") or "")
    now = time.time()
    if _CACHE["source"] is not None and _CACHE["at"] and now - _CACHE["at"] < ttl \
            and _CACHE.get("key") == key:
        return _CACHE["source"], _CACHE["model"], _CACHE["note"]
    source, model, note = resolve_model(cfg)
    _CACHE.update({"at": now, "key": key, "source": source,
                   "model": model, "note": note})
    return source, model, note
