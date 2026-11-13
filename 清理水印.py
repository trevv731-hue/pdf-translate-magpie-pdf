# -*- coding: utf-8 -*-
"""清理已有译本 PDF 里 babeldoc 写入的推广水印与 AI 标记。

用法：
    python 清理水印.py <pdf1> [pdf2 ...]
    python 清理水印.py --dir <目录>   # 递归处理目录下所有 *_中文.pdf / *.zh.mono.pdf / *.zh.dual.pdf

处理前会先把原文件复制为 <原名>.watermark-bak.pdf（已存在则跳过），以便回滚。
"""
import argparse
import os
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

MARKERS = ("funstory.ai", "BabelDOC", "yadt.io", "开源 PDF 翻译库", "Translation_generated_by_AI")


def is_watermarked(pdf_path: Path) -> tuple[bool, bool]:
    """返回 (有可见水印, 有 AI 属性标记)。"""
    try:
        import fitz
    except Exception:
        return False, False
    visible = False
    meta_dirty = False
    try:
        with fitz.open(str(pdf_path)) as doc:
            if doc.page_count > 0:
                text = doc[0].get_text()
                visible = any(m in text for m in ("funstory.ai", "BabelDOC", "yadt.io"))
            meta = doc.metadata or {}
            meta_dirty = any("BabelDOC" in str(v) or "Translation_generated_by_AI" in str(v)
                            for v in meta.values())
    except Exception:
        return False, False
    return visible, meta_dirty


def backup(path: Path) -> Path:
    bak = path.with_name(path.name + ".watermark-bak.pdf")
    if not bak.exists():
        shutil.copy2(path, bak)
    return bak


def clean_one(path: Path) -> str:
    try:
        import fitz
    except Exception as exc:
        return f"跳过（无 fitz）：{exc}"
    visible, meta_dirty = is_watermarked(path)
    if not visible and not meta_dirty:
        return "无需处理（无水印/无标记）"

    backup(path)

    redacted_lines = 0
    try:
        with fitz.open(str(path)) as doc:
            # 只有首页有水印，但为了保险扫全部页
            for page in doc:
                targets = []
                d = page.get_text("dict")
                for b in d.get("blocks", []):
                    for line in b.get("lines", []):
                        txt = "".join(s.get("text", "") for s in line.get("spans", []))
                        if any(m in txt for m in ("funstory.ai", "BabelDOC", "yadt.io", "开源 PDF 翻译库")):
                            targets.append(line["bbox"])
                for rect in targets:
                    r = fitz.Rect(rect)
                    # 稍微放大，避免字形残留
                    r = r + (-2, -2, 2, 2)
                    page.add_redact_annot(r, fill=False)
                    redacted_lines += 1
                if targets:
                    page.apply_redactions(images=0, graphics=0)

            # 还原属性
            meta = dict(doc.metadata or {})
            marks = ("BabelDOC", "Translation_generated_by_AI")
            if any(m in str(meta.get("producer", "")) or m in str(meta.get("creator", "")) for m in marks):
                # 读备份里的原文属性作参考（备份是处理前的译本，producer 也是 BabelDOC；
                # 这里只能把标记抹掉，producer 留空或沿用 creator）
                if any(m in str(meta.get("producer", "")) for m in marks):
                    meta["producer"] = ""
                if any(m in str(meta.get("creator", "")) for m in marks):
                    # creator 通常是 "原作者, 原 producer"，把 BabelDOC 段删掉
                    c = meta.get("creator", "") or ""
                    c = c.replace("BabelDOC", "").strip(" ,")
                    meta["creator"] = c
                doc.set_metadata(meta)

            doc.saveIncr()
    except Exception as exc:
        return f"失败：{exc}"

    return f"已清理（水印行 {redacted_lines}，属性{'+' if meta_dirty else ''}）"


def gather_paths(args) -> list[Path]:
    out = []
    if args.dir:
        root = Path(args.dir)
        for p in root.rglob("*.pdf"):
            if p.name.endswith(".watermark-bak.pdf"):
                continue
            out.append(p)
    for a in args.files:
        p = Path(a)
        if p.is_dir():
            for x in p.rglob("*.pdf"):
                if not x.name.endswith(".watermark-bak.pdf"):
                    out.append(x)
        else:
            out.append(p)
    # 只处理看起来是译本的文件（含水印或 AI 标记），但 gather 阶段先全部列出
    return sorted(set(out))


def main():
    ap = argparse.ArgumentParser(description="清理译本 PDF 中的 babeldoc 水印")
    ap.add_argument("files", nargs="*", help="PDF 文件")
    ap.add_argument("--dir", help="目录（递归）")
    ap.add_argument("--all", action="store_true", help="处理目录下所有 PDF，不只译本")
    args = ap.parse_args()

    paths = gather_paths(args)
    if not paths:
        print("未找到 PDF")
        return 1

    # 默认只处理译本样式的文件名，避免误伤原文
    if not args.all and not args.files:
        filtered = []
        for p in paths:
            n = p.name.lower()
            if (n.endswith("_中文.pdf") or ".zh.mono.pdf" in n or ".zh.dual.pdf" in n
                    or ".no_watermark." in n or n.endswith(".mono.pdf")):
                filtered.append(p)
        if filtered:
            paths = filtered

    print(f"待处理：{len(paths)} 个文件")
    done = 0
    for p in paths:
        vis, md = is_watermarked(p)
        if not (vis or md):
            print(f"  - {p.name}: 跳过（无水印）")
            continue
        res = clean_one(p)
        print(f"  - {p.name}: {res}")
        if res.startswith("已清理"):
            done += 1
    print(f"\n完成：{done} 个文件已清理。原文件备份为 *.watermark-bak.pdf")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
