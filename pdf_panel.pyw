# -*- coding: utf-8 -*-
"""PDF 翻译 - 一体化控制台（单窗口）

把「引擎状态」和「待翻译列表」合成同一个窗口：
  1) 模型来源二选一：默认（公共免费 API，SiliconFlow Free）或跟随
     Magpie（走本机反代，模型跟着 Magpie 当前选的走）；只显示状态 +
     可改并发 QPS。配置读写与 pdfq.py / select_model.py 共用 config.env。
  2) 待翻译列表：支持拖放 PDF / 目录，或点「添加 PDF」。
  3) 开始翻译：在独立控制台窗口里跑 pdfq.py，只翻当前列表里的文件。

不再维护跨次运行的历史队列：关掉窗口就是一次任务的结束，下次拖什么翻什么。

用法：双击桌面「PDF翻译」快捷方式（本文件）。
"""

import os
import subprocess
import sys


HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# 便携版 Python 的 Tcl/Tk 库在 python\Library\lib\ 下，必须在 import tkinter 前告知
_TCL = os.path.join(HERE, "python", "Library", "lib", "tcl8.6")
_TK = os.path.join(HERE, "python", "Library", "lib", "tk8.6")
if os.path.isdir(_TCL):
    os.environ.setdefault("TCL_LIBRARY", _TCL)
if os.path.isdir(_TK):
    os.environ.setdefault("TK_LIBRARY", _TK)

import ctypes
from ctypes import wintypes

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import select_model as sm  # noqa: E402  (复用 read_config / write_config / 网关拉取)
import model_source as ms  # noqa: E402  (模型来源：公共免费 API / 跟随 Magpie)

APP_TITLE = "PDF 翻译"
# 不逐个挑模型：两个来源二选一，默认是公共免费 API
DEFAULT_ENGINE = ms.PUBLIC_ENGINE
DEFAULT_MODEL = ms.DEFAULT_MODEL
FONT = ("Microsoft YaHei UI", 10)
FONT_BOLD = ("Microsoft YaHei UI", 11, "bold")
BG = "#F6F5F2"
CARD = "#FFFFFF"
MUTED = "#5B5B5B"
ACCENT = "#2F6F62"

# ---------------------------------------------------------------------------
# 拖放：Win32 WM_DROPFILES（不引入 tkinterdnd2）
# ---------------------------------------------------------------------------


class DropTarget:
    WM_DROPFILES = 0x0233
    GWLP_WNDPROC = -4
    WM_COPYDATA = 0x004A
    WM_COPYGLOBALDATA = 0x0049
    MSGFLT_ALLOW = 1

    def __init__(self, root, on_drop=None):
        self.root = root
        self.on_drop = on_drop
        self.old_proc = {}
        self.keep = []
        self.ok = False
        # 窗口过程里绝对不能碰 Tk（Tcl 不允许在消息派发中重入，会直接
        # 0xC0000409 fail-fast 闪退）。这里只做纯 Python 入队，主线程轮询取。
        self.pending = []

        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.shell32 = ctypes.WinDLL("shell32", use_last_error=True)

        LRESULT = ctypes.c_ssize_t
        self.WNDPROC = ctypes.WINFUNCTYPE(
            LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        self.user32.GetParent.restype = wintypes.HWND
        self.user32.GetParent.argtypes = [wintypes.HWND]
        self.user32.SetWindowLongPtrW.restype = ctypes.c_void_p
        self.user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int,
                                                  ctypes.c_void_p]
        for fn in (self.user32.CallWindowProcW, self.user32.DefWindowProcW):
            fn.restype = LRESULT
            fn.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT,
                           wintypes.WPARAM, wintypes.LPARAM]
        self.shell32.DragQueryFileW.restype = wintypes.UINT
        self.shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT,
                                                wintypes.LPWSTR, wintypes.UINT]
        self.shell32.DragFinish.argtypes = [wintypes.HANDLE]
        self.shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]

    def install(self):
        try:
            client = int(self.root.winfo_id())
            frame = self.user32.GetParent(wintypes.HWND(client))
            targets = [client] + ([int(frame)] if frame and int(frame) != client else [])
            for hwnd in targets:
                self._subclass(hwnd)
            self.ok = bool(targets)
        except Exception:
            self.ok = False
        return self.ok

    def _subclass(self, hwnd):
        self.shell32.DragAcceptFiles(wintypes.HWND(hwnd), True)
        for msg in (self.WM_DROPFILES, self.WM_COPYDATA, self.WM_COPYGLOBALDATA):
            try:
                self.user32.ChangeWindowMessageFilterEx(
                    wintypes.HWND(hwnd), msg, self.MSGFLT_ALLOW, None)
            except Exception:
                pass

        @self.WNDPROC
        def proc(h, msg, wparam, lparam):
            if msg == self.WM_DROPFILES:
                try:
                    paths = self._extract(wparam)
                except Exception:
                    paths = []
                if paths:
                    # 只入队：这里调 root.after() 会让 Tcl 在消息派发中重入而崩
                    self.pending.append(paths)
                return 0
            old = self.old_proc.get(hwnd)
            if old:
                return self.user32.CallWindowProcW(old, h, msg, wparam, lparam)
            return self.user32.DefWindowProcW(None, h, msg, wparam, lparam)

        old = self.user32.SetWindowLongPtrW(
            wintypes.HWND(hwnd), self.GWLP_WNDPROC, ctypes.cast(proc, ctypes.c_void_p))
        self.old_proc[hwnd] = old or None
        self.keep.append(proc)

    def _extract(self, hdrop):
        count = self.shell32.DragQueryFileW(wintypes.HANDLE(hdrop), 0xFFFFFFFF, None, 0)
        paths = []
        for index in range(count):
            length = self.shell32.DragQueryFileW(wintypes.HANDLE(hdrop), index, None, 0)
            buf = ctypes.create_unicode_buffer(length + 1)
            self.shell32.DragQueryFileW(wintypes.HANDLE(hdrop), index, buf, length + 1)
            if buf.value:
                paths.append(buf.value)
        self.shell32.DragFinish(wintypes.HANDLE(hdrop))
        return paths


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------


class App:
    def __init__(self, root):
        self.root = root
        root.title(APP_TITLE)
        root.geometry("760x660")
        root.minsize(660, 560)
        root.configure(bg=BG)

        self.files = []
        self.var_qps = tk.StringVar(value="10")
        self.var_source = tk.StringVar(value=ms.SOURCE_PUBLIC)

        self._ensure_default_config()
        self._build()
        self.refresh_state()
        self.drop = DropTarget(root)
        self.drop.install()
        self.root.after(120, self._poll_drops)

    def _poll_drops(self):
        """把窗口过程收到的拖放路径搬到主线程处理（绝不在 WNDPROC 里碰 Tk）。"""
        try:
            while self.drop.pending:
                paths = self.drop.pending.pop(0)
                self.on_drop(paths)
        except Exception as exc:  # 拖进来的东西再怪也不该让面板退出
            try:
                self.note("拖放处理失败：%s" % exc)
            except Exception:
                pass
        self.root.after(120, self._poll_drops)

    # ---- 布局 ----
    def _build(self):
        outer = tk.Frame(self.root, bg=BG, padx=14, pady=12)
        outer.pack(fill="both", expand=True)

        tk.Label(outer, text=APP_TITLE, bg=BG, fg="#1F1F1F", font=("Microsoft YaHei UI", 16, "bold"))\
            .pack(anchor="w")
        tk.Label(outer, text="拖入 PDF 或目录 → 开始翻译。模型来源可在下面切换。",
                 bg=BG, fg=MUTED, font=FONT).pack(anchor="w", pady=(0, 10))

        # ---- 卡片 1：引擎（模型来源二选一，不逐个挑模型）----
        card = tk.Frame(outer, bg=CARD, highlightbackground="#E2E0DA", highlightthickness=1, padx=12, pady=10)
        card.pack(fill="x")
        tk.Label(card, text="引擎", bg=CARD, fg="#1F1F1F", font=FONT_BOLD).pack(anchor="w")

        self.lbl_state = tk.Label(card, text="", bg=CARD, fg=MUTED, font=FONT, justify="left", anchor="w")
        self.lbl_state.pack(fill="x", pady=(4, 8))

        row_src = tk.Frame(card, bg=CARD)
        row_src.pack(fill="x", pady=(0, 8))
        tk.Label(row_src, text="模型来源", bg=CARD, fg="#1F1F1F", font=FONT).pack(side="left")
        ttk.Radiobutton(row_src, text="默认（公共免费 API）", value=ms.SOURCE_PUBLIC,
                        variable=self.var_source, command=self.save_source).pack(side="left", padx=(10, 0))
        ttk.Radiobutton(row_src, text="在 Magpie 里选定", value=ms.SOURCE_PINNED,
                        variable=self.var_source, command=self.save_source).pack(side="left", padx=(8, 0))
        ttk.Radiobutton(row_src, text="跟随 Magpie 当前模型", value=ms.SOURCE_MAGPIE,
                        variable=self.var_source, command=self.save_source).pack(side="left", padx=(8, 0))

        row = tk.Frame(card, bg=CARD)
        row.pack(fill="x")
        tk.Label(row, text="并发 QPS", bg=CARD, fg="#1F1F1F", font=FONT).pack(side="left")
        ttk.Spinbox(row, from_=1, to=64, width=5, textvariable=self.var_qps).pack(side="left", padx=(8, 12))
        ttk.Button(row, text="保存", width=6, command=self.save_qps).pack(side="left")
        ttk.Button(row, text="刷新", width=6, command=self.refresh_state).pack(side="left", padx=(6, 0))
        ttk.Button(row, text="在 Magpie 里选模型…", width=18,
                   command=self.open_magpie).pack(side="left", padx=(12, 0))

        # ---- 卡片 2：待翻译 ----
        card2 = tk.Frame(outer, bg=CARD, highlightbackground="#E2E0DA", highlightthickness=1, padx=12, pady=10)

        head = tk.Frame(card2, bg=CARD)
        head.pack(fill="x")
        tk.Label(head, text="待翻译", bg=CARD, fg="#1F1F1F", font=FONT_BOLD).pack(side="left")
        self.lbl_count = tk.Label(head, text="0 项", bg=CARD, fg=MUTED, font=FONT)
        self.lbl_count.pack(side="left", padx=(8, 0))
        ttk.Button(head, text="清空", width=6, command=self.clear_files).pack(side="right")
        ttk.Button(head, text="添加 PDF", width=9, command=self.pick_files).pack(side="right", padx=(0, 6))

        box = tk.Frame(card2, bg=CARD)
        box.pack(fill="both", expand=True, pady=(8, 0))
        self.listbox = tk.Listbox(box, bg="#FCFCFA", fg="#1F1F1F", font=FONT,
                                  selectmode="extended", relief="flat",
                                  highlightthickness=1, highlightbackground="#E2E0DA",
                                  activestyle="none")
        sb = ttk.Scrollbar(box, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=sb.set)
        self.listbox.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        # ---- 操作行 ----
        actions = tk.Frame(outer, bg=BG)
        self.btn_go = tk.Button(actions, text="开始翻译", command=self.start,
                                bg=ACCENT, fg="white", font=("Microsoft YaHei UI", 10, "bold"),
                                activebackground="#255A4F", activeforeground="white",
                                relief="flat", padx=18, pady=6, cursor="hand2")
        self.btn_go.pack(side="left")
        for text, cmd in (("日志", lambda: self.run_pdfq(["logs"])),
                          ("清理临时文件", lambda: self.run_pdfq(["clean", "--force"]))):
            ttk.Button(actions, text=text, command=cmd).pack(side="left", padx=(6, 0))

        # ---- 状态条 ----
        self.lbl_note = tk.Label(outer, text="", bg=BG, fg=ACCENT, font=FONT, anchor="w")
        self.lbl_note.pack(side="bottom", fill="x", pady=(8, 0))
        actions.pack(side="bottom", fill="x", pady=(10, 0))
        # 待翻译卡片（expand=True）必须最后 pack，否则会先吃掉空间把按钮行挤出窗口
        card2.pack(side="top", fill="both", expand=True, pady=(10, 0))

    # ---- 数据 ----
    def _cfg(self):
        return sm.read_config()

    def _ensure_default_config(self):
        """配置缺字段时补上，保证「不用选也能跑」：默认走公共免费节点。"""
        cfg = self._cfg()
        if not cfg.get("MODEL_SOURCE"):
            # 老配置（只有 ENGINE）按引擎推回来，不擅自把用户切到公共节点
            source = (ms.SOURCE_MAGPIE
                      if (cfg.get("ENGINE") or "").lower() == ms.MAGPIE_ENGINE
                      else ms.SOURCE_PUBLIC)
            sm.write_config(ms.apply_source(cfg, source))
            return
        out = ms.normalized(cfg)
        if out != cfg:
            sm.write_config(out)

    def refresh_state(self):
        cfg = self._cfg()
        source = ms.source_of(cfg)
        self.var_source.set(source)
        self.var_qps.set(cfg.get("QPS", "10"))

        if source == ms.SOURCE_PUBLIC:
            text = ("当前引擎：公共免费节点（SiliconFlow Free）\n"
                    "模型来源：默认（公共免费 API）\n"
                    "无需 Magpie，也不需要 API Key")
        else:
            actual, note = ms.resolve_model(cfg)[1:]
            text = ("当前引擎：Magpie 反代（openaicompatible）\n"
                    "模型来源：%s\n"
                    "实际使用：%s%s\n"
                    "接口：%s") % (
                ms.label_of(source), actual, ("（%s）" % note) if note else "",
                cfg.get("BASE_URL", ""))
            if source == ms.SOURCE_PINNED:
                text += "\n换模型：点「在 Magpie 里选模型…」，在 Agents 页的 PDF Translate 行里选"
        live = sm.fetch_magpie_models()
        if live:
            text += "\n网关状态：在线，%d 个模型可用" % len(live)
        else:
            text += "\n网关状态：离线"
        self.lbl_state.config(text=text)
        self._render_count()

    def _render_count(self):
        self.lbl_count.config(text="%d 项" % len(self.files))
        if not self.files:
            self.listbox.delete(0, "end")
            self.listbox.insert("end", "（把 PDF 或文件夹拖进来）")

    # ---- 设置动作 ----
    def _qps(self):
        try:
            return max(1, min(64, int(self.var_qps.get())))
        except ValueError:
            return 10

    def save_qps(self):
        """只改并发，来源 / 引擎 / 模型 / key / 接口原样保留。"""
        cfg = self._cfg()
        out = ms.normalized(cfg)
        out["QPS"] = str(self._qps())
        sm.write_config(out)
        self.note("已保存 QPS=%s" % out["QPS"])
        self.refresh_state()

    def save_source(self):
        """切换模型来源：来源对应的引擎一起改，其它原样保留。"""
        cfg = self._cfg()
        source = self.var_source.get()
        if source not in ms.SOURCES:
            source = ms.SOURCE_PUBLIC
        out = ms.apply_source(cfg, source)
        out["QPS"] = cfg.get("QPS", "10")
        sm.write_config(out)
        self.note("模型来源已切为：%s" % ms.label_of(source))
        self.refresh_state()

    def open_magpie(self):
        """把 Magpie 打开到 Agents 页，用户在那里给 PDF Translate 选模型。

        选完 Magpie 会把模型、网关地址和它自己的 key 写进 config.env；
        这里只负责把来源切到「在 Magpie 里选定」，模型名不自己猜。
        """
        cfg = self._cfg()
        out = ms.apply_source(cfg, ms.SOURCE_PINNED)
        out["QPS"] = cfg.get("QPS", "10")
        sm.write_config(out)
        try:
            if not ms.magpie_models(timeout=1.5):
                raise OSError("gateway offline")
        except Exception:
            messagebox.showwarning(
                "Magpie 没在运行",
                "没连上 Magpie 网关（%s）。\n\n先启动 Magpie，再点这个按钮。" % ms.MAGPIE_URL)
            self.refresh_state()
            return
        try:
            subprocess.Popen([ms.MAGPIE_EXE], cwd=HERE)
        except Exception as exc:
            messagebox.showwarning(
                "没能打开 Magpie",
                "%s\n\n手动打开 Magpie，在 Agents 页找到 PDF Translate 那一行选模型。" % exc)
        self.note("已切到「在 Magpie 里选定」：在 Magpie › Agents › PDF Translate 里选模型")
        self.refresh_state()

    # ---- 文件 ----
    def on_drop(self, paths):
        self.add_files(paths)

    def pick_files(self):
        picked = filedialog.askopenfilenames(
            title="选择要翻译的 PDF", filetypes=[("PDF", "*.pdf"), ("所有文件", "*.*")])
        if picked:
            self.add_files(list(picked))

    def add_files(self, paths):
        added = 0
        for p in paths:
            try:
                p = os.path.abspath(p)
                if os.path.isdir(p):
                    for name in sorted(os.listdir(p)):
                        if name.lower().endswith(".pdf"):
                            f = os.path.join(p, name)
                            if f not in self.files:
                                self.files.append(f)
                                added += 1
                elif p.lower().endswith(".pdf"):
                    if p not in self.files:
                        self.files.append(p)
                        added += 1
            except OSError:
                continue  # 没权限 / 网络盘断开之类，跳过，别让面板退出
        self.listbox.delete(0, "end")
        for f in self.files:
            self.listbox.insert("end", f)
        self._render_count()
        self.note("已添加 %d 个文件" % added if added else "没有新的 PDF 可添加")

    def clear_files(self):
        self.files = []
        self.listbox.delete(0, "end")
        self._render_count()
        self.note("列表已清空")

    # ---- 执行 ----
    def run_pdfq(self, args):
        bat = os.path.join(HERE, "pdfq.bat")
        if not os.path.isfile(bat):
            messagebox.showerror("找不到 pdfq.bat", bat)
            return
        if args:
            joined = " ".join('"%s"' % a for a in args)
            cmd = 'cmd /c ""%s" %s"' % (bat, joined)
        else:
            cmd = 'cmd /c ""%s""' % bat
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        try:
            subprocess.Popen(cmd, cwd=HERE, creationflags=flags)
        except Exception as exc:
            messagebox.showerror("启动失败", str(exc))
            return
        self.note("已启动：pdfq %s" % (" ".join(args) or "（没有文件）"))

    def start(self):
        if not self.files:
            messagebox.showinfo("还没有文件", "先把 PDF 拖进窗口，或点「添加 PDF」。")
            return
        # 无历史队列：这一次列表里的文件就是全部任务，翻完即结束。
        self.run_pdfq(list(self.files))
        self.note("已提交 %d 个文件" % len(self.files))

    def note(self, text):
        self.lbl_note.config(text=text)


def main():
    try:  # 高分屏清晰
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
