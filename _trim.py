"""精简便携版体积。只做有证据支撑的删除，并且先隔离再删。

删除依据：
1. /gradio + gradio_pdf     —— 只有 pdf2zh_next/gui.py 在模块顶层 import，
   我们的命令行路径从不导入 gui，实测 294 个已加载模块里没有 gradio。
2. models/fonts/fonts       —— 与上一级 models/fonts 逐字节相同的副本（md5 已比对）。
   旧版 _backup_20261029/pdf_queue.pyw 会把 models/ 复制进 ~/.cache/babeldoc，
   复制完成、缓存已有内容之后它就不再被读取（CACHE_FOLDER 硬编码在 ~/.cache，
   且没有环境变量可覆盖）。
3. python/share/jupyter     —— Jupyter 的运行时数据，翻译链路完全不涉及。
4. python/Lib/idlelib       —— IDLE 编辑器，界面都废弃了。
5. python310.pdb            —— 调试符号，只有调试器需要。

用法：venv/Scripts/python.exe _trim.py [--apply]
不带 --apply 只报告，不改动。
"""

import shutil
import sys
from pathlib import Path

TOOL = Path(__file__).resolve().parent
SP = TOOL / "venv" / "Lib" / "site-packages"


def size_of(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def move_to_quarantine(path: Path, label: str) -> int:
    """整包移出 site-packages：不删除，出问题搬回来就行。"""
    if not path.exists():
        return 0
    freed = size_of(path)
    target = TOOL / "_trimmed" / label
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target)
    shutil.move(str(path), str(target))
    return freed


def purge(path: Path) -> int:
    """确认过是副本/废弃物的才真删。"""
    if not path.exists():
        return 0
    freed = size_of(path)
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    return freed


def main():
    apply = "--apply" in sys.argv
    plan = []

    for name in ("gradio", "gradio_pdf"):
        target = SP / name
        plan.append(("隔离", target, size_of(target)))

    for rel in ("models/fonts/fonts", "python/share/jupyter", "python/Lib/idlelib",
                "python/python310.pdb"):
        target = TOOL / rel
        if target.exists():
            plan.append(("删除", target, size_of(target)))

    total = sum(item[2] for item in plan)
    print("待处理 %d 项，合计 %.0f MB" % (len(plan), total / 1024 / 1024))
    for action, target, freed in plan:
        print("  %s  %7.1f MB  %s" % (action, freed / 1024 / 1024,
                                      target.relative_to(TOOL)))

    if not apply:
        print("\n（演练模式，未改动。加 --apply 执行）")
        return 0

    freed_total = 0
    for action, target, _ in plan:
        if action == "隔离":
            freed_total += move_to_quarantine(target, target.name)
        else:
            freed_total += purge(target)
    print("\n实际释放 %.0f MB" % (freed_total / 1024 / 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
