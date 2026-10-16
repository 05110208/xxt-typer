# -*- coding: utf-8 -*-
"""学习通「禁止粘贴」作业框 —— 逐字键盘模拟输入助手（图形界面）。

为什么需要它
------------
学习通部分作业页用 JavaScript 拦截了粘贴（``paste`` / ``Ctrl+V``），剪贴板内容进不去。
本工具不碰剪贴板：把文本拆成一个个真实按键，用系统输入接口送进当前前台窗口，
浏览器收到的是普通键盘输入，所以粘贴限制无效。

图形界面用法
------------
1. 双击 ``run.bat``（或 ``python xxt_typer.py``）打开窗口；
2. 把答案粘到上面的大文本框（这个框没有限制，随便粘）；
3. 点「开始输入 (F8)」，在倒计时结束前用鼠标点一下学习通的答题框；
4. 工具逐字输入，随时可按 F8 暂停/继续、F9 停止。

命令行用法（可选）
------------------
    python xxt_typer.py --file 答案.txt --delay 30 --countdown 5
    type 答案.txt | python xxt_typer.py --file - --countdown 8

全局快捷键（浏览器在前台也有效）：F8 开始/暂停/继续，F9 停止。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
import traceback
from tkinter import filedialog, messagebox, ttk

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from typing_engine import (  # noqa: E402
    AutoCloseTracker,
    TypingEngine,
    estimate_seconds,
    ime_open,
)

try:
    from pynput import keyboard as pynput_keyboard
except Exception:  # pragma: no cover - 缺少依赖时仍可用按钮操作
    pynput_keyboard = None

CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
LAST_TEXT_PATH = os.path.join(SCRIPT_DIR, "last_input.txt")
LOG_PATH = os.path.join(SCRIPT_DIR, "xxt_typer.log")


# ---------------------------------------------------------------------------
# 日志与异常兜底
# 图形界面用 pythonw.exe 启动时没有控制台，一旦出错会“静默消失”。
# 所以所有关键步骤、所有未捕获异常都写进 xxt_typer.log，出错时还会弹窗提示。
# ---------------------------------------------------------------------------
def log(message: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [pid {os.getpid()}] {message}"
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:
        pass
    try:
        if sys.stderr is not None:
            print(line, file=sys.stderr)
    except Exception:
        pass


def _show_error_dialog(title: str, message: str) -> None:
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, message)
        root.destroy()
    except Exception:
        pass


def install_exception_hooks() -> None:
    def main_hook(exc_type, exc_value, exc_tb) -> None:
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        log("未捕获异常（主线程）:\n" + text)
        _show_error_dialog(
            "xxt_typer 出错",
            f"{exc_type.__name__}: {exc_value}\n\n详细信息已写入同目录下的 xxt_typer.log",
        )

    sys.excepthook = main_hook

    if hasattr(threading, "excepthook"):
        def thread_hook(args) -> None:
            text = "".join(
                traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
            )
            name = getattr(args.thread, "name", "?")
            log(f"未捕获异常（线程 {name}）:\n" + text)

        threading.excepthook = thread_hook


# ---------------------------------------------------------------------------
# 前台窗口工具：用来确认按键到底会送进哪个窗口
# ---------------------------------------------------------------------------
def _root_of(hwnd: int) -> int:
    """取窗口的最外层句柄（GA_ROOT）。"""
    if not hwnd or not sys.platform.startswith("win"):
        return 0
    try:
        user32 = ctypes.windll.user32
        user32.GetAncestor.restype = ctypes.c_void_p
        user32.GetAncestor.argtypes = (ctypes.c_void_p, ctypes.c_uint)
        return int(user32.GetAncestor(ctypes.c_void_p(hwnd), 2) or hwnd)
    except Exception:
        return 0


def _foreground_window() -> tuple:
    """返回当前前台窗口的 (hwnd, pid, 标题)。"""
    if not sys.platform.startswith("win"):
        return 0, 0, ""
    try:
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        hwnd = int(user32.GetForegroundWindow() or 0)
        if not hwnd:
            return 0, 0, ""
        pid = ctypes.c_ulong(0)
        user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
        length = int(user32.GetWindowTextLengthW(ctypes.c_void_p(hwnd)))
        buffer = ctypes.create_unicode_buffer(max(1, length + 1))
        user32.GetWindowTextW(ctypes.c_void_p(hwnd), buffer, len(buffer))
        return hwnd, pid.value, buffer.value
    except Exception:
        return 0, 0, ""


def _is_own_window(hwnd: int, own_hwnd: int) -> bool:
    """判断某个窗口是不是本工具自己的窗口。"""
    if not hwnd or not own_hwnd:
        return False
    return _root_of(hwnd) == _root_of(own_hwnd)


MODE_LABELS = {
    "Unicode 直发（文字推荐：支持中文，不挑输入法）": TypingEngine.MODE_UNICODE,
    "真实键码（代码推荐：编辑器自动补全/缩进才正常）": TypingEngine.MODE_KEYS,
    "pynput 键码（个别老编辑器兼容模式）": TypingEngine.MODE_PYNPUT,
}

NEWLINE_LABELS = {
    "回车 Enter（普通文本框、代码编辑器）": "enter",
    "Shift + Enter（富文本编辑器的软换行）": "shift+enter",
}

# 两种工作模式：切换时套用一套推荐设置，之后仍可单独微调
SCENE_LABELS = {
    "文字模式（答题框 / 纯文字）": "text",
    "代码模式（IDE / 在线代码编辑器）": "code",
}

SCENE_PRESETS = {
    "text": {
        "mode_label": "Unicode 直发（文字推荐：支持中文，不挑输入法）",
        "char_delay_ms": 30,
        "line_delay_ms": 200,
        "newline_label": "回车 Enter（普通文本框、代码编辑器）",
        "skip_empty_lines": False,
        "strip_leading_indent": False,
        "auto_skip_brackets": False,
        "auto_skip_quotes": False,
    },
    "code": {
        "mode_label": "真实键码（代码推荐：编辑器自动补全/缩进才正常）",
        "char_delay_ms": 15,
        "line_delay_ms": 120,
        "newline_label": "回车 Enter（普通文本框、代码编辑器）",
        "skip_empty_lines": False,
        "strip_leading_indent": True,
        "auto_skip_brackets": True,
        "auto_skip_quotes": False,
    },
}

DEFAULT_CONFIG = {
    "scene_label": "文字模式（答题框 / 纯文字）",
    "mode_label": "Unicode 直发（文字推荐：支持中文，不挑输入法）",
    "newline_label": "回车 Enter（普通文本框、代码编辑器）",
    "char_delay_ms": 30,
    "line_delay_ms": 200,
    "countdown_s": 3.0,
    "skip_empty_lines": False,
    "strip_leading_indent": False,
    "auto_skip_brackets": False,
    "auto_skip_quotes": False,
    "geometry": "960x740",
}

APP_TITLE = "学习通禁止粘贴 · 逐字键盘输入助手"


# ---------------------------------------------------------------------------
# 图形界面
# ---------------------------------------------------------------------------
class TyperApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.config = self._load_config()
        self.commands: "queue.Queue[tuple]" = queue.Queue()
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.listener = None
        self.state = "idle"
        self._pump_job = None
        # 倒计时结束后，最多等这么多秒让用户把焦点切到目标窗口
        self.target_wait_seconds = 30.0
        # 找到目标窗口后，再留这么多秒给用户确认（期间显示"即将输入到哪个窗口"）
        self.target_confirm_seconds = 2.0
        # 自动测试时可以关掉模态错误弹窗，避免卡在对话框上
        self.show_error_dialogs = True

        self._build_ui()
        self._start_hotkeys()
        self._apply_state()
        self._update_stats()
        self._pump()

        # 让 tkinter 回调里的异常也被记下来，而不是只往看不见的 stderr 丢
        self.root.report_callback_exception = self._on_tk_error
        log(
            "界面已打开："
            f"输入方式={self.var_mode.get()!r} 字符间隔={self.var_char_delay.get()}ms "
            f"换行等待={self.var_line_delay.get()}ms 倒计时={self.var_countdown.get()}s "
            f"全局快捷键={'可用' if self.listener is not None else '不可用'}"
        )

        root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_tk_error(self, exc_type, exc_value, exc_tb) -> None:
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        log("界面回调异常:\n" + text)
        try:
            self._set_status(f"界面内部错误：{exc_value}（详情见 xxt_typer.log）")
        except Exception:
            pass

    # -- 界面 -------------------------------------------------------------
    def _build_ui(self) -> None:
        self.root.title(APP_TITLE)
        self.root.geometry(self.config.get("geometry", DEFAULT_CONFIG["geometry"]))
        self.root.minsize(780, 560)

        try:
            ttk.Style().theme_use("vista")
        except tk.TclError:
            pass

        header = ttk.Frame(self.root, padding=(12, 10, 12, 2))
        header.pack(fill="x")
        ttk.Label(
            header,
            text="① 把答案粘到下面的大文本框（这个框可以正常粘贴）",
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            header,
            text="② 点「开始输入 (F8)」，在倒计时结束前用鼠标点一下学习通的答题框   "
                 "③ 工具会像你打字一样逐字输入；F8 暂停/继续，F9 立即停止",
            foreground="#555555",
        ).pack(anchor="w", pady=(2, 0))

        body = ttk.Frame(self.root, padding=(12, 8, 12, 2))
        body.pack(fill="both", expand=True)
        self.text = tk.Text(body, wrap="word", undo=True, font=("Microsoft YaHei UI", 11))
        self.text.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(body, command=self.text.yview)
        scroll.pack(side="right", fill="y")
        self.text.configure(yscrollcommand=scroll.set)
        self.text.bind("<KeyRelease>", lambda _event: self._update_stats())
        self.text.bind("<<Paste>>", lambda _event: self.root.after(20, self._update_stats))
        self.text.bind("<<Cut>>", lambda _event: self.root.after(20, self._update_stats))

        stats = ttk.Frame(self.root, padding=(12, 0))
        stats.pack(fill="x")
        self.var_stats = tk.StringVar(value="")
        ttk.Label(stats, textvariable=self.var_stats, foreground="#555555").pack(side="left")

        opts = ttk.LabelFrame(self.root, text="输入设置", padding=(12, 8))
        opts.pack(fill="x", padx=12, pady=(6, 4))

        self.var_scene = tk.StringVar(value=self.config["scene_label"])
        self.var_mode = tk.StringVar(value=self.config["mode_label"])
        self.var_newline = tk.StringVar(value=self.config["newline_label"])
        self.var_char_delay = tk.IntVar(value=self.config["char_delay_ms"])
        self.var_line_delay = tk.IntVar(value=self.config["line_delay_ms"])
        self.var_countdown = tk.DoubleVar(value=self.config["countdown_s"])
        self.var_skip_empty = tk.BooleanVar(value=self.config["skip_empty_lines"])
        self.var_strip_indent = tk.BooleanVar(value=self.config["strip_leading_indent"])
        self.var_skip_brackets = tk.BooleanVar(value=self.config["auto_skip_brackets"])
        self.var_skip_quotes = tk.BooleanVar(value=self.config["auto_skip_quotes"])

        ttk.Label(opts, text="使用模式：").grid(row=0, column=0, sticky="w")
        self.cmb_scene = ttk.Combobox(
            opts, textvariable=self.var_scene, values=list(SCENE_LABELS),
            state="readonly", width=30,
        )
        self.cmb_scene.grid(row=0, column=1, columnspan=2, sticky="w", pady=2)
        self.cmb_scene.bind("<<ComboboxSelected>>", self._on_scene_changed)
        ttk.Label(opts, text="（切换模式会套用一套推荐设置，之后仍可单独微调）",
                  foreground="#777777").grid(row=0, column=3, sticky="w")

        ttk.Label(opts, text="输入方式：").grid(row=1, column=0, sticky="w", pady=2)
        self.cmb_mode = ttk.Combobox(
            opts, textvariable=self.var_mode, values=list(MODE_LABELS),
            state="readonly", width=42,
        )
        self.cmb_mode.grid(row=1, column=1, columnspan=3, sticky="w")

        ttk.Label(opts, text="换行方式：").grid(row=2, column=0, sticky="w", pady=2)
        self.cmb_newline = ttk.Combobox(
            opts, textvariable=self.var_newline, values=list(NEWLINE_LABELS),
            state="readonly", width=42,
        )
        self.cmb_newline.grid(row=2, column=1, columnspan=3, sticky="w")

        ttk.Label(opts, text="字符间隔(毫秒)：").grid(row=3, column=0, sticky="w", pady=2)
        ttk.Spinbox(opts, from_=0, to=2000, increment=5, width=7,
                    textvariable=self.var_char_delay, command=self._update_stats
                    ).grid(row=3, column=1, sticky="w")
        ttk.Label(opts, text="换行后等待(毫秒)：").grid(row=3, column=2, sticky="w", padx=(16, 0))
        ttk.Spinbox(opts, from_=0, to=5000, increment=50, width=7,
                    textvariable=self.var_line_delay, command=self._update_stats
                    ).grid(row=3, column=3, sticky="w")
        ttk.Label(opts, text="开始前倒计时(秒)：").grid(row=4, column=0, sticky="w", pady=2)
        ttk.Spinbox(opts, from_=0, to=60, increment=1, width=7,
                    textvariable=self.var_countdown, command=self._update_stats
                    ).grid(row=4, column=1, sticky="w")

        code_opts = ttk.LabelFrame(self.root, text="代码模式选项（文字模式下可忽略）", padding=(12, 6))
        code_opts.pack(fill="x", padx=12, pady=(0, 4))
        ttk.Checkbutton(
            code_opts,
            text="编辑器会自动补全括号：输入收尾的 ) ] } 时按右方向键跳过，避免出现 }} 这样的重复",
            variable=self.var_skip_brackets, command=self._update_stats,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=1)
        ttk.Checkbutton(
            code_opts,
            text="同上，但引号 \" ' 也一起处理（有些编辑器会补全引号）",
            variable=self.var_skip_quotes, command=self._update_stats,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=1)
        ttk.Checkbutton(
            code_opts,
            text="回车后编辑器会自动缩进：跳过源文本的行首空格，避免变成双倍缩进",
            variable=self.var_strip_indent, command=self._update_stats,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=1)
        ttk.Checkbutton(
            opts, text="忽略空行（默认保留，代码/排版建议不要勾）",
            variable=self.var_skip_empty, command=self._update_stats,
        ).grid(row=5, column=0, columnspan=4, sticky="w", pady=2)

        buttons = ttk.Frame(self.root, padding=(12, 4))
        buttons.pack(fill="x")
        ttk.Button(buttons, text="载入文件…", command=self.load_file).pack(side="left")
        ttk.Button(buttons, text="保存内容…", command=self.save_file).pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text="清空", command=self.clear_text).pack(side="left", padx=(6, 0))
        self.btn_start = ttk.Button(buttons, text="开始输入 (F8)", command=self.toggle)
        self.btn_start.pack(side="left", padx=(18, 0))
        self.btn_stop = ttk.Button(buttons, text="停止 (F9)", command=self.stop)
        self.btn_stop.pack(side="left", padx=(6, 0))

        status = ttk.Frame(self.root, padding=(12, 6, 12, 10))
        status.pack(fill="x")
        self.var_status = tk.StringVar(value="就绪")
        ttk.Label(status, textvariable=self.var_status, width=52, anchor="w").pack(side="left")
        self.progress = ttk.Progressbar(status, mode="determinate", maximum=1, value=0, length=240)
        self.progress.pack(side="left", padx=(10, 10))
        self.var_progress = tk.StringVar(value="0 / 0")
        ttk.Label(status, textvariable=self.var_progress, width=16, anchor="w").pack(side="left")

    # -- 配置与文本 -------------------------------------------------------
    def _load_config(self) -> dict:
        config = dict(DEFAULT_CONFIG)
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
                stored = json.load(handle)
            if isinstance(stored, dict):
                config.update({key: stored[key] for key in config if key in stored})
        except Exception:
            pass
        if config.get("mode_label") not in MODE_LABELS:
            config["mode_label"] = DEFAULT_CONFIG["mode_label"]
        if config.get("newline_label") not in NEWLINE_LABELS:
            config["newline_label"] = DEFAULT_CONFIG["newline_label"]
        if config.get("scene_label") not in SCENE_LABELS:
            config["scene_label"] = DEFAULT_CONFIG["scene_label"]
        return config

    def _on_scene_changed(self, _event=None) -> None:
        """切换使用模式时套用一套推荐设置。"""
        scene = SCENE_LABELS.get(self.var_scene.get())
        if scene is None:
            return
        preset = SCENE_PRESETS[scene]
        self.var_mode.set(preset["mode_label"])
        self.var_newline.set(preset["newline_label"])
        self.var_char_delay.set(preset["char_delay_ms"])
        self.var_line_delay.set(preset["line_delay_ms"])
        self.var_skip_empty.set(preset["skip_empty_lines"])
        self.var_strip_indent.set(preset["strip_leading_indent"])
        self.var_skip_brackets.set(preset["auto_skip_brackets"])
        self.var_skip_quotes.set(preset["auto_skip_quotes"])
        self._update_stats()
        tip = ("代码模式：请先按 Shift 把输入法切到英文，否则真实键码会变成拼音；"
               "编辑器自动补全的收尾括号会被自动跳过。"
               if scene == "code" else
               "文字模式：用 Unicode 直发字符，中文/全角标点最稳，不受输入法影响。")
        self._set_status(tip)
        log(f"切换使用模式：{self.var_scene.get()}（{tip}）")

    def _save_config(self) -> None:
        self.config.update({
            "scene_label": self.var_scene.get(),
            "mode_label": self.var_mode.get(),
            "newline_label": self.var_newline.get(),
            "char_delay_ms": int(self.var_char_delay.get()),
            "line_delay_ms": int(self.var_line_delay.get()),
            "countdown_s": float(self.var_countdown.get()),
            "skip_empty_lines": bool(self.var_skip_empty.get()),
            "strip_leading_indent": bool(self.var_strip_indent.get()),
            "auto_skip_brackets": bool(self.var_skip_brackets.get()),
            "auto_skip_quotes": bool(self.var_skip_quotes.get()),
            "geometry": self.root.geometry(),
        })
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
                json.dump(self.config, handle, ensure_ascii=False, indent=2)
        except Exception:
            pass

    @staticmethod
    def _save_last_text(text: str) -> None:
        try:
            with open(LAST_TEXT_PATH, "w", encoding="utf-8") as handle:
                handle.write(text)
        except Exception:
            pass

    def load_file(self) -> None:
        path = filedialog.askopenfilename(
            title="选择要输入的文本文件",
            filetypes=[("文本文件", "*.txt *.md *.csv *.json"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            text = _read_text_file(path)
        except Exception as exc:
            messagebox.showerror("读取失败", f"{path}\n{exc}")
            return
        self.text.delete("1.0", "end")
        self.text.insert("1.0", text)
        self._update_stats()
        self._set_status(f"已载入 {os.path.basename(path)}")

    def save_file(self) -> None:
        content = self.text.get("1.0", "end-1c")
        if not content:
            messagebox.showinfo("没有内容", "文本框是空的。")
            return
        path = filedialog.asksaveasfilename(
            title="保存文本", defaultextension=".txt",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content)
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))
            return
        self._set_status(f"已保存到 {os.path.basename(path)}")

    def clear_text(self) -> None:
        self.text.delete("1.0", "end")
        self._update_stats()

    # -- 快捷键 -----------------------------------------------------------
    def _start_hotkeys(self) -> None:
        if pynput_keyboard is None:
            self._set_status("未安装 pynput，全局快捷键不可用（按钮仍可使用）")
            return

        def on_press(key) -> None:
            if key == pynput_keyboard.Key.f8:
                self._emit("hotkey", "toggle")
            elif key == pynput_keyboard.Key.f9:
                self._emit("hotkey", "stop")

        try:
            self.listener = pynput_keyboard.Listener(on_press=on_press)
            self.listener.daemon = True
            self.listener.start()
        except Exception as exc:
            self.listener = None
            self._set_status(f"全局快捷键启动失败：{exc}（按钮仍可使用）")

    # -- 状态 -------------------------------------------------------------
    def _emit(self, kind: str, payload) -> None:
        self.commands.put((kind, payload))

    def _set_status(self, message: str) -> None:
        self.var_status.set(message)

    def _update_stats(self) -> None:
        content = self.text.get("1.0", "end-1c")
        lines = content.count("\n") + (1 if content else 0)
        seconds = estimate_seconds(
            content,
            char_delay=self.var_char_delay.get() / 1000.0,
            line_delay=self.var_line_delay.get() / 1000.0,
            newline_key=NEWLINE_LABELS.get(self.var_newline.get(), "enter"),
            skip_empty_lines=bool(self.var_skip_empty.get()),
            strip_leading_indent=bool(self.var_strip_indent.get()),
        )
        if content:
            self.var_stats.set(f"共 {len(content)} 个字符 / {lines} 行，预计需要 {seconds:.0f} 秒")
        else:
            self.var_stats.set("文本框为空。可直接按 Ctrl+V 把答案粘到这里。")

    def _apply_state(self) -> None:
        state = self.state
        if state == "idle":
            self.btn_start.configure(text="开始输入 (F8)", state="normal")
            self.btn_stop.configure(state="disabled")
            self.text.configure(state="normal")
        elif state == "countdown":
            self.btn_start.configure(text="取消倒计时 (F8)", state="normal")
            self.btn_stop.configure(state="normal")
            self.text.configure(state="disabled")
        elif state == "typing":
            self.btn_start.configure(text="暂停 (F8)", state="normal")
            self.btn_stop.configure(state="normal")
            self.text.configure(state="disabled")
        elif state == "paused":
            self.btn_start.configure(text="继续 (F8)", state="normal")
            self.btn_stop.configure(state="normal")
            self.text.configure(state="disabled")

    # -- 主流程（主线程） -------------------------------------------------
    def toggle(self) -> None:
        if self.state == "idle":
            self.start()
        elif self.state == "countdown":
            self.stop_event.set()
            self._set_status("已取消，未输入任何内容")
            log("用户取消了倒计时")
        elif self.state == "typing":
            self.pause_event.set()
            self.state = "paused"
            self._apply_state()
            self._set_status("已暂停 —— 按 F8 或点「继续」接着输入")
            log("已暂停")
        elif self.state == "paused":
            self.pause_event.clear()
            self.state = "typing"
            self._apply_state()
            self._set_status("继续输入中…（F8 暂停，F9 停止）")
            log("已继续")

    def stop(self) -> None:
        if self.state == "idle":
            return
        self.stop_event.set()
        self.pause_event.clear()
        self._set_status("正在停止…")
        log("收到停止指令")

    def start(self) -> None:
        content = self.text.get("1.0", "end-1c")
        if not content.strip():
            messagebox.showinfo("没有内容", "请先把要输入的文字粘到上面的文本框里。")
            return

        self._save_config()
        self._save_last_text(content)

        options = {
            "mode": MODE_LABELS.get(self.var_mode.get(), TypingEngine.MODE_UNICODE),
            "char_delay": max(0, int(self.var_char_delay.get())) / 1000.0,
            "line_delay": max(0, int(self.var_line_delay.get())) / 1000.0,
            "countdown": max(0.0, float(self.var_countdown.get())),
            "newline_key": NEWLINE_LABELS.get(self.var_newline.get(), "enter"),
            "skip_empty_lines": bool(self.var_skip_empty.get()),
            "strip_leading_indent": bool(self.var_strip_indent.get()),
            "auto_skip_brackets": bool(self.var_skip_brackets.get()),
            "auto_skip_quotes": bool(self.var_skip_quotes.get()),
            # 必须在主线程取窗口句柄：Tk 不是线程安全的，工作线程里取会拿不到
            "own_hwnd": self._own_root_hwnd(),
        }

        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.progress.configure(value=0, maximum=1)
        self.var_progress.set("0 / 0")
        self.state = "countdown"
        self._apply_state()

        log(
            f"开始输入：{len(content)} 个字符，使用模式={self.var_scene.get()}，"
            f"输入方式={options['mode']}，换行={options['newline_key']}，"
            f"字符间隔={options['char_delay'] * 1000:.0f}ms，"
            f"换行等待={options['line_delay'] * 1000:.0f}ms，倒计时={options['countdown']}s，"
            f"跳过补全括号={options['auto_skip_brackets']}，跳过补全引号={options['auto_skip_quotes']}，"
            f"跳过行首缩进={options['strip_leading_indent']}"
        )
        self.worker = threading.Thread(
            target=self._run_typing, args=(content, options), daemon=True
        )
        self.worker.start()

    # -- 输入线程 ---------------------------------------------------------
    def _own_root_hwnd(self) -> int:
        try:
            return _root_of(int(self.root.winfo_id()))
        except Exception:
            return 0

    def _run_typing(self, content: str, options: dict) -> None:
        try:
            deadline = time.time() + options["countdown"]
            while True:
                if self.stop_event.is_set():
                    log("倒计时期间被取消")
                    self._emit("finished", {"typed": 0, "total": 0, "stopped": True,
                                            "elapsed": 0.0, "reason": "已取消"})
                    return
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self._emit("status", f"{remaining:.1f} 秒后开始 —— 请现在点击学习通的答题框")
                time.sleep(0.1)

            # ---- 目标窗口守卫：先等到活动窗口不是自己，再给 2 秒确认 ----
            own = int(options.get("own_hwnd") or 0)
            log(f"本工具窗口句柄={own}")
            wait_deadline = time.time() + max(2.0, float(self.target_wait_seconds))

            while True:
                # 第一步：等一个不属于本工具的前台窗口
                hwnd = pid = 0
                title = ""
                while True:
                    if self.stop_event.is_set():
                        log("等待目标窗口期间被取消")
                        self._emit("finished", {"typed": 0, "total": 0, "stopped": True,
                                                "elapsed": 0.0, "reason": "已取消"})
                        return
                    hwnd, pid, title = _foreground_window()
                    if hwnd and not _is_own_window(hwnd, own):
                        break
                    if time.time() > wait_deadline:
                        log("等待目标窗口超时：活动窗口一直是本工具自己")
                        self._emit("status",
                                   "没等到目标窗口，已取消：请先点一下学习通的答题框，再按 F8")
                        self._emit("finished", {"typed": 0, "total": 0, "stopped": True,
                                                "elapsed": 0.0, "reason": "未选择目标窗口"})
                        return
                    self._emit("status", "现在活动窗口还是本工具自己 —— 请用鼠标点一下学习通的答题框…")
                    time.sleep(0.2)

                # 第二步：确认期，把即将输入到哪个窗口告诉用户，期间可以按 F9 取消
                target_root = _root_of(hwnd)
                confirm_end = time.time() + max(0.0, float(self.target_confirm_seconds))
                changed = False
                while time.time() < confirm_end:
                    if self.stop_event.is_set():
                        log("确认期内被取消")
                        self._emit("finished", {"typed": 0, "total": 0, "stopped": True,
                                                "elapsed": 0.0, "reason": "已取消"})
                        return
                    if _root_of(_foreground_window()[0]) != target_root:
                        changed = True
                        break
                    left = confirm_end - time.time()
                    self._emit("status", f"{left:.1f} 秒后开始输入到「{title}」…（F9 取消）")
                    time.sleep(0.1)
                if not changed:
                    if _root_of(_foreground_window()[0]) == target_root:
                        break
                self._emit("status", "目标窗口变了，请重新点一下学习通的答题框…")
                wait_deadline = time.time() + max(2.0, float(self.target_wait_seconds))

            log(f"目标窗口：pid={pid} 标题={title!r}")

            # 代码模式用真实键码，中文输入法开着会把字母变成拼音 —— 提醒但不阻断
            if options["mode"] == TypingEngine.MODE_KEYS and ime_open():
                log("注意：检测到前台窗口的中文输入法处于打开状态，真实键码可能被输入法截走")
                self._emit("status", "注意：中文输入法还开着，请按 Shift 切到英文，否则代码会变拼音…")
                time.sleep(1.2)

            self._emit("status", f"正在输入到「{title or '当前窗口'}」…（F8 暂停，F9 停止）")
            self._emit("countdown_done", None)

            auto_close = AutoCloseTracker(
                skip_brackets=options["auto_skip_brackets"],
                skip_quotes=options["auto_skip_quotes"],
            )
            engine = TypingEngine(
                mode=options["mode"],
                char_delay=options["char_delay"],
                line_delay=options["line_delay"],
                newline_key=options["newline_key"],
                skip_empty_lines=options["skip_empty_lines"],
                strip_leading_indent=options["strip_leading_indent"],
                auto_close=auto_close,
                on_progress=lambda done, total, _unit: self._emit("progress", (done, total)),
                on_log=lambda message: self._emit("status", message),
            )
            result = engine.type_text(content, stop_event=self.stop_event, pause_event=self.pause_event)
            log(
                f"输入结束：{result.get('typed', 0)}/{result.get('total', 0)} 个字符，"
                f"用时 {result.get('elapsed', 0.0):.1f}s，停止={result.get('stopped')}，"
                f"Unicode 回退={result.get('fallback', 0)} 次，"
                f"跳过自动补全的收尾符={result.get('skipped', 0)} 个"
            )
            self._emit("finished", result)
        except Exception as exc:
            log("输入过程出错:\n" + traceback.format_exc())
            self._emit("error", f"{type(exc).__name__}: {exc}")

    # -- 队列轮询（主线程） -----------------------------------------------
    def _pump(self) -> None:
        try:
            while True:
                try:
                    kind, payload = self.commands.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle(kind, payload)
                except Exception:
                    log(f"处理界面消息 {kind!r} 时出错:\n" + traceback.format_exc())
        finally:
            # 无论发生什么都要继续排下一轮，否则界面会失去响应
            try:
                self._pump_job = self.root.after(60, self._pump)
            except tk.TclError:
                pass  # 窗口已经销毁

    def _handle(self, kind: str, payload) -> None:
        if kind == "hotkey":
            if payload == "toggle":
                self.toggle()
            elif payload == "stop":
                self.stop()
        elif kind == "status":
            self._set_status(str(payload))
        elif kind == "progress":
            done, total = payload
            self.progress.configure(maximum=max(1, total), value=done)
            self.var_progress.set(f"{done} / {total}")
        elif kind == "countdown_done":
            self.state = "typing"
            self._apply_state()
        elif kind == "finished":
            self.state = "idle"
            self.pause_event.clear()
            self._apply_state()
            result = payload
            if result.get("stopped"):
                self._set_status(f"已停止：本次输入 {result['typed']} / {result['total']} 个字符")
            else:
                self.progress.configure(value=result.get("total", 0))
                self.var_progress.set(f"{result.get('typed', 0)} / {result.get('total', 0)}")
                self._set_status(
                    f"完成：{result.get('typed', 0)} 个字符，用时 {result.get('elapsed', 0):.1f} 秒"
                )
                try:
                    self.root.bell()
                except tk.TclError:
                    pass
        elif kind == "error":
            self.state = "idle"
            self._apply_state()
            self._set_status("出错了")
            if self.show_error_dialogs:
                messagebox.showerror("输入过程出错", str(payload))

    # -- 关闭 -------------------------------------------------------------
    def _on_close(self) -> None:
        self.stop_event.set()
        self.pause_event.clear()
        if self._pump_job is not None:
            try:
                self.root.after_cancel(self._pump_job)
            except Exception:
                pass
            self._pump_job = None
        if self.listener is not None:
            try:
                self.listener.stop()
            except Exception:
                pass
        self._save_config()
        self._save_last_text(self.text.get("1.0", "end-1c"))
        log("窗口关闭")
        self.root.destroy()


def _read_text_file(path: str) -> str:
    """尽量按常见中文编码读取文本文件。"""
    for encoding in ("utf-8-sig", "utf-8", "gbk", "big5"):
        try:
            with open(path, "r", encoding=encoding) as handle:
                return handle.read()
        except UnicodeDecodeError:
            continue
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def _enable_dpi_awareness() -> None:
    if not sys.platform.startswith("win"):
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def run_gui() -> int:
    _enable_dpi_awareness()
    log(f"界面启动（python={sys.executable}，参数={sys.argv[1:]}）")
    root = tk.Tk()
    app = TyperApp(root)
    # 上一次的内容还在的话，友好提示一下
    if os.path.exists(LAST_TEXT_PATH) and not app.text.get("1.0", "end-1c"):
        try:
            previous = _read_text_file(LAST_TEXT_PATH)
        except Exception:
            previous = ""
        if previous.strip():
            if messagebox.askyesno("恢复上次内容", "要不要把上次文本框里的内容载入回来？"):
                app.text.insert("1.0", previous)
                app._update_stats()
    root.mainloop()
    log("主循环结束（窗口已关闭）")
    return 0


# ---------------------------------------------------------------------------
# 命令行模式
# ---------------------------------------------------------------------------
def run_cli(args: argparse.Namespace) -> int:
    if args.text is not None:
        content = args.text
    elif args.file == "-":
        content = sys.stdin.read()
    else:
        content = _read_text_file(args.file)

    if not content.strip():
        print("内容为空，什么都不用输入。")
        return 2

    stop_event = threading.Event()
    listener = None
    if pynput_keyboard is not None:
        def on_press(key) -> None:
            if key == pynput_keyboard.Key.f9:
                stop_event.set()
                print("\n[F9] 收到停止指令…")
        try:
            listener = pynput_keyboard.Listener(on_press=on_press)
            listener.daemon = True
            listener.start()
        except Exception as exc:
            print(f"（提示）全局停止键 F9 不可用：{exc}；可用 Ctrl+C 中断。")

    countdown = max(0.0, float(args.countdown))
    if countdown:
        print(f"{countdown:.0f} 秒后开始输入，请现在用鼠标点一下目标答题框…（F9 停止）")
        deadline = time.time() + countdown
        try:
            while time.time() < deadline and not stop_event.is_set():
                time.sleep(0.1)
        except KeyboardInterrupt:
            stop_event.set()

    engine = TypingEngine(
        mode=args.mode,
        char_delay=max(0, args.delay) / 1000.0,
        line_delay=max(0, args.line_delay) / 1000.0,
        newline_key=args.newline,
        skip_empty_lines=bool(args.skip_empty_lines),
        strip_leading_indent=bool(args.strip_leading_indent),
        auto_close=AutoCloseTracker(
            skip_brackets=not args.no_auto_skip,
            skip_quotes=bool(args.auto_skip_quotes),
        ),
        on_progress=lambda done, total, _unit: _print_progress(done, total),
    )
    try:
        result = engine.type_text(content, stop_event=stop_event)
    except KeyboardInterrupt:
        stop_event.set()
        print("\n已中断。")
        return 130
    finally:
        if listener is not None:
            try:
                listener.stop()
            except Exception:
                pass

    print()
    if result["stopped"]:
        print(f"已停止：输入了 {result['typed']} / {result['total']} 个字符。")
        return 130
    print(f"完成：{result['typed']} 个字符，用时 {result['elapsed']:.1f} 秒，"
          f"跳过自动补全的收尾符 {result['skipped']} 个。")
    return 0


def _print_progress(done: int, total: int) -> None:
    if total <= 0:
        return
    if done % 20 == 0 or done == total:
        percent = done * 100 // total
        sys.stdout.write(f"\r输入中… {done}/{total} ({percent}%)")
        sys.stdout.flush()


# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    install_exception_hooks()
    parser = argparse.ArgumentParser(
        description="学习通等禁止粘贴页面的逐字键盘模拟输入工具（pynput）。",
    )
    parser.add_argument("--file", "-f", help="从文本文件读取内容；用 - 表示标准输入")
    parser.add_argument("--text", "-t", help="直接给出要输入的文本")
    parser.add_argument("--delay", type=int, default=30, help="字符间隔毫秒，默认 30")
    parser.add_argument("--line-delay", type=int, default=200, help="换行后的等待毫秒，默认 200")
    parser.add_argument("--countdown", type=float, default=5.0, help="开始前倒计时秒数，默认 5")
    parser.add_argument("--mode", choices=[TypingEngine.MODE_UNICODE, TypingEngine.MODE_KEYS,
                                           TypingEngine.MODE_PYNPUT],
                        default=TypingEngine.MODE_UNICODE,
                        help="输入方式：unicode（文字，默认）/ keys（代码，真实键码）/ pynput")
    parser.add_argument("--code", action="store_true",
                        help="代码模式预设：真实键码 + 跳过自动补全的括号 + 跳过行首缩进")
    parser.add_argument("--newline", choices=["enter", "shift+enter"], default="enter",
                        help="换行方式，默认 enter")
    parser.add_argument("--skip-empty-lines", action="store_true", help="跳过空行（默认保留空行）")
    parser.add_argument("--keep-empty-lines", action="store_true",
                        help="（旧写法，空行默认已保留）")
    parser.add_argument("--no-auto-skip", action="store_true",
                        help="关闭“跳过编辑器自动补全的收尾括号”")
    parser.add_argument("--auto-skip-quotes", action="store_true",
                        help="把引号 \" ' 也纳入自动跳过")
    parser.add_argument("--strip-leading-indent", action="store_true",
                        help="换行后跳过行首缩进（编辑器会自动缩进时用）")
    args = parser.parse_args(argv)

    if args.code:
        args.mode = TypingEngine.MODE_KEYS
        args.strip_leading_indent = True
        args.newline = args.newline or "enter"
        if args.delay == 30:
            args.delay = 15
        if args.line_delay == 200:
            args.line_delay = 120

    if args.file or args.text:
        return run_cli(args)
    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
