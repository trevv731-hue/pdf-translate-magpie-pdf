# -*- coding: utf-8 -*-
"""PDF 翻译 - 模型与引擎交互选择器"""

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
CFG_FILE = os.path.join(HERE, "config.env")
MAGPIE_KEY = "sk-magpie-key-1cdbbb977588c0599a122cce1803ec5515ee3bafae75f317"
MAGPIE_URL = "http://127.0.0.1:3425/v1"

import model_source as ms  # noqa: E402  (模型来源：默认 / 跟随 Magpie)

MODELS = [
    ("trae-cn/deepseek-v4.1-flash-max", "Trae CN DeepSeek Flash Max (推荐)"),
    ("codearts/deepseek-v4.1-flash", "CodeArts DeepSeek Flash"),
    ("zcode/GLM-5.3-Flash", "ZCode GLM 5.3 Flash"),
    ("zcode/GLM-5.3-Flash-Trial", "ZCode GLM 5.3 Flash 试用"),
    ("qwenwork/flash", "千问标准版 (flash)"),
    ("qwenwork/pro", "千问高级版 (pro)"),
    ("qwenwork/qwen3.8-max-preview", "Qwen3.8-Max"),
]


def fetch_magpie_models(timeout=3.0):
    """从 magpie 本地网关拉实时模型列表；失败返回 None。"""
    import json
    import urllib.request
    req = urllib.request.Request(
        MAGPIE_URL.rstrip("/") + "/models",
        headers={"Authorization": "Bearer " + MAGPIE_KEY},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        items = payload.get("data") or []
        out = []
        for it in items:
            mid = (it.get("id") or "").strip()
            if not mid:
                continue
            label = it.get("display_name") or it.get("magpie_label") or mid
            out.append((mid, label))
        return out or None
    except Exception:
        return None

def read_config():
    cfg = {}
    if os.path.isfile(CFG_FILE):
        try:
            with open(CFG_FILE, "r", encoding="utf-8-sig") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    cfg[k.strip().upper()] = v.strip().strip("'\"")
        except Exception:
            pass
    return cfg

def write_config(cfg):
    lines = [
        "# PDF 翻译引擎配置",
        "# MODEL_SOURCE: public（默认，公共免费 API）| pinned（在 Magpie 里选定）| magpie（跟随 Magpie 当前模型）",
        "ENGINE=%s" % cfg.get("ENGINE", "siliconflowfree"),
        "MODEL_SOURCE=%s" % (cfg.get("MODEL_SOURCE") or ms.SOURCE_PUBLIC),
    ]
    for key in ("API_KEY", "MODEL", "BASE_URL", "QPS"):
        if cfg.get(key):
            lines.append("%s=%s" % (key, cfg[key]))
    with open(CFG_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")

def main():
    # 强制在 Windows 终端使用 UTF-8
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    while True:
        cfg = read_config()
        curr_engine = cfg.get("ENGINE", "siliconflowfree")
        curr_model = cfg.get("MODEL", "")
        curr_base = cfg.get("BASE_URL", "")
        curr_qps = cfg.get("QPS", "10")

        live_models = fetch_magpie_models()
        models = live_models if live_models else MODELS
        source, active_model, source_note = ms.resolve_model_cached(cfg)

        clear_screen()
        print("=" * 60)
        print("            PDF 翻译 - 模型与引擎选择")
        print("=" * 60)
        print()
        print("[当前配置状态]")
        print("  模型来源: %s" % ms.label_of(source))
        if source != ms.SOURCE_PUBLIC:
            print("  当前引擎: %s" % curr_engine)
            print("  实际使用: %s  (%s)" % (active_model, source_note))
            print("  配置模型: %s" % (curr_model or ms.DEFAULT_MODEL))
            print("  接口地址: %s" % curr_base)
        else:
            print("  当前引擎: %s（公共免费节点，无需 Magpie / API Key）" % curr_engine)
        print("  当前并发: %s 请求/秒" % curr_qps)
        if live_models:
            print("  Magpie 网关: 在线，%d 个模型 (实时同步)" % len(live_models))
        else:
            print("  Magpie 网关: 离线，使用内置模型列表")
        print("-" * 60)
        print()
        print("[Magpie 反代模型 (高速推荐)]")
        shown = models[:12]
        for i, (m_id, m_desc) in enumerate(shown, 1):
            mark = " [当前]" if source == ms.SOURCE_MAGPIE and active_model == m_id else ""
            print("  %d. %-40s (%s)%s" % (i, m_id, m_desc, mark))
        if len(models) > len(shown):
            print("  ... 其余 %d 个模型用选项 8 输入模型 ID" % (len(models) - len(shown)))
        print()
        print("[其他操作]")
        print("  M. 切换模型来源 (当前: %s)" % ms.label_of(source))
        print("  8. 自定义输入 Magpie 模型名称 (手动输入 provider/model)")
        print("  9. 切回公共免费节点 (SiliconFlow Free，无需 Magpie)")
        print("  A. 在 Magpie 里选模型 (打开 Magpie 的 Agents 页，选完这里自动跟上)")
        print("  0. 调整并发 QPS (当前: %s)" % curr_qps)
        print("  Q. 退出")
        print()
        print("=" * 60)

        try:
            choice = input("请输入选项编号 [0-9, A, M, Q]: ").strip()
        except EOFError:
            break
        if not choice:
            continue
        if choice.upper() == "Q":
            break

        if choice.upper() == "M":
            order = [ms.SOURCE_PUBLIC, ms.SOURCE_PINNED, ms.SOURCE_MAGPIE]
            try:
                new_source = order[(order.index(source) + 1) % len(order)]
            except ValueError:
                new_source = ms.SOURCE_PINNED
            write_config(ms.apply_source(cfg, new_source))
            print("\n[√] 模型来源已切为: %s" % ms.label_of(new_source))
            time.sleep(1.2)
            continue

        if choice.upper() == "A":
            write_config(ms.apply_source(cfg, ms.SOURCE_PINNED))
            opened = False
            try:
                if os.path.isfile(ms.MAGPIE_EXE):
                    subprocess.Popen([ms.MAGPIE_EXE], cwd=HERE)
                    opened = True
            except Exception:
                opened = False
            print("\n[√] 已切到「%s」" % ms.label_of(ms.SOURCE_PINNED))
            print("    %s" % ("Magpie 已打开：在 Agents 页的 PDF Translate 那一行选模型，"
                             "选完它会把模型写进 config.env。" if opened else
                             "手动打开 Magpie，在 Agents 页找到 PDF Translate 那一行选模型。"))
            input("    按回车继续…")
            continue

        if choice in [str(i) for i in range(1, len(models) + 1)]:
            idx = int(choice) - 1
            picked_model = models[idx][0]
            new_cfg = {
                "ENGINE": "openaicompatible",
                "MODEL_SOURCE": ms.SOURCE_PINNED,
                "API_KEY": MAGPIE_KEY,
                "MODEL": picked_model,
                "BASE_URL": MAGPIE_URL,
                "QPS": curr_qps or "10",
            }
            write_config(new_cfg)
            print("\n[√] 已成功切换为模型: %s" % picked_model)
            time.sleep(1.2)
        elif choice == "8":
            custom_model = input("\n请输入 Magpie 模型名称 (例如 trae-cn/deepseek-v4.1-flash-max): ").strip()
            if custom_model:
                new_cfg = {
                    "ENGINE": "openaicompatible",
                    "MODEL_SOURCE": ms.SOURCE_PINNED,
                    "API_KEY": MAGPIE_KEY,
                    "MODEL": custom_model,
                    "BASE_URL": MAGPIE_URL,
                    "QPS": curr_qps or "10",
                }
                write_config(new_cfg)
                print("\n[√] 已成功切换为自定义模型: %s" % custom_model)
                time.sleep(1.2)
        elif choice == "9":
            new_cfg = {
                "ENGINE": "siliconflowfree",
                "MODEL_SOURCE": ms.SOURCE_PUBLIC,
                "QPS": curr_qps or "10",
            }
            write_config(new_cfg)
            print("\n[√] 已成功切回公共免费节点 (SiliconFlow Free)")
            time.sleep(1.2)
        elif choice == "0":
            new_qps = input("\n请输入每秒请求并发数 QPS (建议 5-30，回车取消): ").strip()
            if new_qps.isdigit():
                cfg["QPS"] = new_qps
                write_config(cfg)
                print("\n[√] QPS 已更新为: %s" % new_qps)
                time.sleep(1.2)
        else:
            print("\n无效输入，请重试。")
            time.sleep(1)

if __name__ == "__main__":
    main()
