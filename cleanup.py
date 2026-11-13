# -*- coding: utf-8 -*-
"""cleanup —— 清理 pdfq 跑翻译时留下的临时文件。

被清的东西有两类：

1. ``<cache>/working/<文件名>/`` —— babeldoc 给每篇放的一份完整源 PDF 拷贝
   和进度 json。翻完之后没人再读，但不清就一直涨：一篇十几 MB 的论文就是
   十几 MB，翻一百篇能白占一两 G。而且这个目录在 ``~/.cache/babeldoc``
   （本机是 D:\\Tools\\babeldoc-cache），文件管理器里根本看不见。
2. 旧的运行日志、pip 下载缓存、工具目录里的 ``__pycache__``。

安全边界：
- **正在跑或还排队的任务，工作副本一律不动**——那是中断后接着跑的上下文。
- 日志不按大小删，按天数删（默认 30 天），保留最近的方便事后查问题。
- 默认只演练，加 ``--apply`` 才真的删。

pdfq.py 在每次开跑之前会自动调用一次（见 ``auto_clean``），所以正常使用
不需要手动执行这个脚本；它主要是给你想手动回收空间时用的。

用法：
    python cleanup.py             # 看看能清多少
    python cleanup.py --apply     # 真的清
    python cleanup.py --days 7    # 日志只留 7 天
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

# 日志保留天数。太短会让"昨天那篇为什么失败"无从查起。
KEEP_LOG_DAYS = 30
# 至少保留这么多个最近的日志文件，避免刚跑完一批就被时间规则清空。
KEEP_LOG_FILES = 20
# 工作副本最近几小时内还在变动的，一律当成"可能正在翻"。没有持久化队列后
# 清理命令看不到别的进程里的任务状态，只能用时间兜底，避免误删正在写的文件。
KEEP_RECENT_HOURS = 6


def _size(path: Path) -> int:
    """目录或文件的字节数；不存在的算 0。"""
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.stat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def human(n: int) -> str:
    return "%.1f MB" % (n / 1048576) if n >= 1048576 else "%.0f KB" % (n / 1024)


def _cache_folder() -> Path | None:
    try:
        from babeldoc.const import CACHE_FOLDER

        return Path(CACHE_FOLDER)
    except Exception:
        return None


def _log_folder() -> Path | None:
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return None
    return Path(base) / "PDFTranslateQueue" / "logs"


def _protected_stems(jobs) -> set:
    """哪些文件名还有人在用（排队中/进行中），它们的工作副本不能删。"""
    stems = set()
    for job in jobs or []:
        if job.get("state") in ("pending", "running"):
            try:
                stems.add(Path(job["path"]).stem)
            except Exception:
                pass
    return stems


def collect(jobs=None, days=KEEP_LOG_DAYS):
    """列出可清理项，不删任何东西。返回 [(说明, 路径, 字节数)]。"""
    items = []
    protected = _protected_stems(jobs)

    cache = _cache_folder()
    if cache:
        work = cache / "working"
        if work.is_dir():
            recent_cutoff = time.time() - KEEP_RECENT_HOURS * 3600
            for entry in sorted(work.iterdir()):
                if not entry.is_dir() or entry.name in protected:
                    continue
                try:
                    if entry.stat().st_mtime >= recent_cutoff:
                        continue
                except OSError:
                    continue
                items.append(("工作副本 working/%s" % entry.name, entry, _size(entry)))

    logs = _log_folder()
    if logs and logs.is_dir():
        try:
            files = sorted(
                (p for p in logs.glob("*.log") if p.is_file()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            files = []
        cutoff = time.time() - days * 86400
        for index, path in enumerate(files):
            if index < KEEP_LOG_FILES:
                continue          # 最近这些无论如何都留着
            try:
                if path.stat().st_mtime < cutoff:
                    items.append(("过期日志 %s" % path.name, path, path.stat().st_size))
            except OSError:
                pass

    # 工具目录自己产生的缓存
    here = Path(__file__).resolve().parent
    for pycache in here.glob("__pycache__"):
        if pycache.is_dir():
            items.append(("编译缓存 __pycache__", pycache, _size(pycache)))

    return items


def apply(items, log=None):
    """执行清理，返回实际释放的字节数。"""
    freed = 0
    for label, path, size in items:
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            freed += size
            if log:
                log.info("已清理 %s（%s）", label, human(size))
        except OSError as exc:
            if log:
                log.warning("清理 %s 失败：%s", label, exc)
    return freed


def auto_clean(jobs, log):
    """给 pdfq.py 用的入口：开跑前顺手清一遍，静默、失败不影响翻译。"""
    try:
        items = collect(jobs)
        if not items:
            return 0
        freed = apply(items, log)
        if freed:
            log.info("启动清理：释放 %s", human(freed))
        return freed
    except Exception as exc:      # 清理永远不该挡住翻译
        log.warning("自动清理跳过：%s", exc)
        return 0


def main():
    parser = argparse.ArgumentParser(description="清理 pdfq 的临时文件与缓存")
    parser.add_argument("--apply", action="store_true", help="真的删除（默认只演练）")
    parser.add_argument("--days", type=int, default=KEEP_LOG_DAYS,
                        help="日志保留天数（默认 %d）" % KEEP_LOG_DAYS)
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    jobs = []
    try:
        import pdfq

        jobs = pdfq.load_queue()
    except Exception:
        pass

    items = collect(jobs, args.days)
    if not items:
        print("  没有需要清理的东西。")
        return 0

    total = sum(size for _label, _path, size in items)
    print("  可清理 %d 项，共 %s\n" % (len(items), human(total)))
    for label, path, size in items:
        print("    %-40s %8s" % (label, human(size)))

    if not args.apply:
        print("\n  演练模式，未删除。加 --apply 执行。")
        return 0

    freed = apply(items)
    print("\n  已释放 %s" % human(freed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
