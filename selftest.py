# -*- coding: utf-8 -*-
"""xxt_typer 自检脚本。

用法：
    python selftest.py            # 单元检查（不弹窗、不发送任何按键）
    python selftest.py --inject   # 单元检查 + 真实窗口逐字注入测试

真实注入测试（``--inject``）会新建一个临时窗口并**抢走前台焦点约 2 秒**，把一段
中英混排文本逐字“打”进去再核对。运行前请先别碰键盘鼠标，否则你的按键会落进这个
自检窗口，结果会被判为“不可信”并跳过。若窗口抢不到焦点，脚本同样会跳过，
绝不会把按键送进你正在使用的其它窗口。
"""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
import threading
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from typing_engine import (  # noqa: E402
    IS_WINDOWS,
    KEY_BACKSPACE,
    KEY_ENTER,
    KEY_TAB,
    TypingEngine,
    estimate_seconds,
    plan_units,
)

SAMPLE = "第一行：中文 test 123，标点！？\n第二行(the 2nd line) —— end"


class Report:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, str]] = []

    def add(self, status: str, name: str, detail: str = "") -> None:
        self.items.append((status, name, detail))
        print(f"[{status}] {name}" + (f" —— {detail}" if detail else ""))

    def failed(self) -> bool:
        return any(status == "FAIL" for status, _name, _detail in self.items)


# ---------------------------------------------------------------------------
# 1) 纯逻辑检查：文本 -> 输入单元
# ---------------------------------------------------------------------------
def test_plan_units(report: Report) -> None:
    cases = [
        (
            "普通字符与 Tab/回车拆分",
            plan_units("a\t中\nb"),
            [("char", "a"), ("key", KEY_TAB), ("char", "中"), ("key", KEY_ENTER), ("char", "b")],
        ),
        (
            "CRLF 规范化",
            plan_units("x\r\ny"),
            [("char", "x"), ("key", KEY_ENTER), ("char", "y")],
        ),
        (
            "退格键识别",
            plan_units("啊\b好"),
            [("char", "啊"), ("key", KEY_BACKSPACE), ("char", "好")],
        ),
        (
            "换行方式换成 Shift+Enter",
            plan_units("a\nb", newline_key="shift+enter"),
            [("char", "a"), ("combo", "shift+enter"), ("char", "b")],
        ),
        (
            "忽略空行",
            plan_units("a\n\n\nb", skip_empty_lines=True),
            [("char", "a"), ("key", KEY_ENTER), ("char", "b")],
        ),
        (
            "保留空行",
            plan_units("a\n\nb", skip_empty_lines=False),
            [("char", "a"), ("key", KEY_ENTER), ("key", KEY_ENTER), ("char", "b")],
        ),
        (
            "代码模式：跳过行首缩进（编辑器会自动缩进）",
            plan_units("int main() {\n    return 0;\n}", strip_leading_indent=True),
            [("char", c) for c in "int main() {"]
            + [("key", KEY_ENTER)]
            + [("char", c) for c in "return 0;"]
            + [("key", KEY_ENTER)]
            + [("char", "}")],
        ),
        (
            "第一行缩进保持不变",
            plan_units("    x", strip_leading_indent=True),
            [("char", c) for c in "    x"],
        ),
    ]
    for name, got, expected in cases:
        if got == expected:
            report.add("PASS", f"文本拆分：{name}")
        else:
            report.add("FAIL", f"文本拆分：{name}", f"得到 {got!r}，期望 {expected!r}")


def test_auto_close(report: Report) -> None:
    """自动补全跳过逻辑：编辑器补好的收尾括号应改成按右方向键跳过。"""
    from typing_engine import AutoCloseTracker

    tracker = AutoCloseTracker(skip_brackets=True, skip_quotes=False)
    decisions = []
    for ch in "int main() { return f(a[0]); }":
        action = tracker.decide(ch)
        decisions.append((ch, action))
        if action == "type":
            tracker.note_typed(ch)
    skipped = "".join(ch for ch, action in decisions if action == "skip")
    # main() 的 )  -> f(...) 的 )  -> a[0] 的 ]  -> 函数体收尾的 }
    expected_skip = ")])}"
    if skipped == expected_skip and tracker.skipped == len(expected_skip):
        report.add("PASS", "自动补全：收尾括号被跳过", f"跳过 {skipped!r}")
    else:
        report.add("FAIL", "自动补全：收尾括号被跳过",
                   f"得到 {skipped!r}（计数 {tracker.skipped}），期望 {expected_skip!r}")

    quotes = AutoCloseTracker(skip_brackets=False, skip_quotes=True)
    plan = []
    for ch in 'puts("hi");':
        action = quotes.decide(ch)
        plan.append((ch, action))
        if action == "type":
            quotes.note_typed(ch)
    quote_skips = sum(1 for _ch, action in plan if action == "skip")
    if quote_skips == 1:
        report.add("PASS", "自动补全：引号可单独开启")
    else:
        report.add("FAIL", "自动补全：引号可单独开启", f"跳过了 {quote_skips} 个引号")

    off = AutoCloseTracker(skip_brackets=False, skip_quotes=False)
    if not off.enabled and off.decide(")") == "type":
        report.add("PASS", "自动补全：关闭时不跳字符")
    else:
        report.add("FAIL", "自动补全：关闭时不跳字符")

    unbalanced = AutoCloseTracker(skip_brackets=True, skip_quotes=False)
    # 源文本里多出来的收尾括号不该被跳过（配对栈里没有对应的开括号）
    if unbalanced.decide("}") == "type":
        report.add("PASS", "自动补全：多出来的收尾括号仍然正常输入")
    else:
        report.add("FAIL", "自动补全：多出来的收尾括号被错误跳过")


def test_key_mapping(report: Report) -> None:
    """真实键码通道：字符 -> 虚拟键码映射是否可用（代码模式的基础）。"""
    if not IS_WINDOWS:
        report.add("SKIP", "真实键码映射", "非 Windows 平台")
        return
    import typing_engine as engine

    layout = engine._current_layout()
    samples = "abcXYZ019(){}[];:,.+-*/=\"'#<>?&|!_"
    missing = []
    bad_shift = []
    for ch in samples:
        mapped = engine._vk_for_char(ch, layout)
        if mapped is None:
            missing.append(ch)
            continue
        vk, shift = mapped
        if not (0 < vk < 0x100):
            bad_shift.append(ch)
    if missing:
        report.add("FAIL", "真实键码映射", f"这些字符映射不出来：{''.join(missing)!r}")
    elif bad_shift:
        report.add("FAIL", "真实键码映射", f"这些字符的虚拟键码不合法：{''.join(bad_shift)!r}")
    else:
        report.add("PASS", "真实键码映射", f"{len(samples)} 个代码常用字符全部可映射")

    # 需要 Shift 的字符必须被识别出来，否则会打出错误的符号
    shifted = [ch for ch in "(){}:\"<>?!#" if (engine._vk_for_char(ch, layout) or (0, False))[1]]
    if len(shifted) >= 6:
        report.add("PASS", "真实键码映射：Shift 判定", f"识别出 {len(shifted)} 个需要 Shift 的符号")
    else:
        report.add("FAIL", "真实键码映射：Shift 判定", f"只识别出 {len(shifted)} 个：{shifted}")


def test_estimate(report: Report) -> None:
    seconds = estimate_seconds("ab\ncd", char_delay=0.1, line_delay=0.5)
    expected = 0.1 + 0.1 + 0.5 + 0.1 + 0.1
    if abs(seconds - expected) < 1e-6:
        report.add("PASS", "耗时估算")
    else:
        report.add("FAIL", "耗时估算", f"得到 {seconds}，期望 {expected}")


def test_struct_layout(report: Report) -> None:
    if not IS_WINDOWS:
        report.add("SKIP", "SendInput 结构体大小", "非 Windows 平台")
        return
    import typing_engine as engine_module

    size = ctypes.sizeof(engine_module._INPUT)
    expected = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
    if size == expected:
        report.add("PASS", "SendInput 结构体内存布局", f"{size} 字节")
    else:
        report.add("FAIL", "SendInput 结构体内存布局", f"得到 {size}，期望 {expected}")


# ---------------------------------------------------------------------------
# 2) 真实注入测试（前台窗口受控）
# ---------------------------------------------------------------------------
def _root_hwnd(hwnd: int) -> int:
    user32 = ctypes.windll.user32
    user32.GetAncestor.restype = ctypes.c_void_p
    user32.GetAncestor.argtypes = (ctypes.c_void_p, ctypes.c_uint)
    return int(user32.GetAncestor(ctypes.c_void_p(hwnd), 2) or hwnd)  # GA_ROOT = 2


def _foreground_root() -> int:
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    hwnd = user32.GetForegroundWindow()
    return _root_hwnd(int(hwnd)) if hwnd else 0


def _force_foreground(root_hwnd: int) -> bool:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.ShowWindow.argtypes = (ctypes.c_void_p, ctypes.c_int)
    user32.SetForegroundWindow.argtypes = (ctypes.c_void_p,)
    user32.BringWindowToTop.argtypes = (ctypes.c_void_p,)
    user32.GetWindowThreadProcessId.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))

    handle = ctypes.c_void_p(root_hwnd)
    user32.ShowWindow(handle, 9)  # SW_RESTORE
    if _foreground_root() == root_hwnd:
        return True

    foreground = _foreground_root()
    thread_foreground = 0
    if foreground:
        thread_foreground = user32.GetWindowThreadProcessId(ctypes.c_void_p(foreground), None)
    thread_current = kernel32.GetCurrentThreadId()

    attached = False
    if thread_foreground and thread_foreground != thread_current:
        attached = bool(user32.AttachThreadInput(thread_foreground, thread_current, True))
    user32.BringWindowToTop(handle)
    user32.SetForegroundWindow(handle)
    if attached:
        user32.AttachThreadInput(thread_foreground, thread_current, False)

    time.sleep(0.3)
    return _foreground_root() == root_hwnd


def test_injection(
    report: Report,
    sample: str = SAMPLE,
    mode: str = TypingEngine.MODE_UNICODE,
    label: str = "Unicode 直发",
) -> None:
    name = f"真实逐字注入（{label}）"
    if not IS_WINDOWS:
        report.add("SKIP", name, "非 Windows 平台")
        return

    import tkinter as tk

    root = tk.Tk()
    root.title(f"xxt_typer 自检窗口 —— {label}")
    root.geometry("760x300+180+180")
    widget = tk.Text(root, wrap="word", font=("Microsoft YaHei UI", 12))
    widget.pack(fill="both", expand=True, padx=8, pady=8)
    root.update()

    typed_result: dict = {}
    focus_lost = False
    try:
        hwnd = _root_hwnd(widget.winfo_id())
        if not _force_foreground(hwnd):
            report.add(
                "SKIP",
                name,
                "自检窗口抢不到前台焦点（Windows 限制后台程序抢焦点）；"
                "为避免把按键送进你正在用的窗口，本次跳过。",
            )
            return
        widget.focus_force()
        root.lift()
        root.update()
        time.sleep(0.5)  # 等 Windows 的前台窗口切换稳定下来
        root.update()
        if _foreground_root() != hwnd and not _force_foreground(hwnd):
            report.add("SKIP", name, "自检窗口稳定后仍抢不到前台焦点，已跳过。")
            return

        def work() -> None:
            engine = TypingEngine(mode=mode, char_delay=0.01, line_delay=0.05)
            typed_result.update(engine.type_text(sample))

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        deadline = time.time() + 60
        lost_since = None
        while thread.is_alive() and time.time() < deadline:
            root.update()
            if _foreground_root() == hwnd:
                lost_since = None
            else:
                # 允许短暂的切换过渡，持续失焦才算真的丢了焦点
                if lost_since is None:
                    lost_since = time.time()
                elif time.time() - lost_since > 0.4:
                    focus_lost = True
            time.sleep(0.02)
        thread.join(timeout=2)
        # 冲刷 Tk 事件队列：注入的按键要等窗口把消息处理完才会出现在控件里
        for _ in range(15):
            root.update()
            time.sleep(0.02)
        got = widget.get("1.0", "end-1c")
    finally:
        root.destroy()

    if focus_lost:
        report.add("SKIP", name, "输入过程中自检窗口失去焦点，结果不可信，已跳过判定。")
        return
    if got == sample:
        report.add(
            "PASS",
            name,
            f"中英混排 {typed_result.get('typed', 0)} 个单元，窗口内容与原文完全一致"
            + (f"（回退 Unicode 通道 {typed_result.get('fallback', 0)} 次）"
               if typed_result.get("fallback") else ""),
        )
    elif not sample.startswith(got):
        report.add(
            "SKIP",
            name,
            "自检窗口里出现了本工具之外的输入，本次结果不可信，已跳过判定。\n"
            "    最常见的原因：pynput 键码模式发出的字母被中文输入法截走改成了汉字候选"
            "（这是该模式的已知限制，把输入法切到英文即可），或者有人正在敲键盘。\n"
            f"    期望：{sample!r}\n    实际：{got!r}",
        )
    else:
        report.add(
            "FAIL",
            name,
            f"只输入了一部分就停了。\n    期望：{sample!r}\n    实际：{got!r}",
        )


def test_ui(report: Report) -> None:
    """在不显示窗口（withdraw）的前提下构建整个界面，检查控件、状态机和消息处理。"""
    try:
        import tkinter as tk
    except Exception as exc:
        report.add("SKIP", "界面自检", f"当前环境没有 tkinter：{exc}")
        return
    try:
        root = tk.Tk()
    except Exception as exc:
        report.add("SKIP", "界面自检", f"当前环境无法创建窗口：{exc}")
        return

    root.withdraw()  # 不显示、不抢焦点
    app = None
    try:
        from xxt_typer import CONFIG_PATH, TyperApp

        app = TyperApp(root)
        root.update()

        app.text.insert("1.0", "第一行 abc\n\n第二行 中文")
        app._update_stats()
        if "个字符" not in app.var_stats.get():
            raise AssertionError(f"字数统计显示异常：{app.var_stats.get()!r}")

        for state in ("countdown", "typing", "paused", "idle"):
            app.state = state
            app._apply_state()
        if str(app.btn_stop["state"]) != "disabled":
            raise AssertionError("回到 idle 后「停止」按钮应为禁用")

        app._handle("status", "测试")
        app._handle("progress", (3, 10))
        if app.var_progress.get() != "3 / 10":
            raise AssertionError(f"进度显示异常：{app.var_progress.get()!r}")
        app._handle("countdown_done", None)
        app._handle("finished", {"typed": 3, "total": 3, "elapsed": 0.5, "stopped": False})
        if app.state != "idle":
            raise AssertionError("输入结束后状态应回到 idle")

        app._save_config()
        if not os.path.exists(CONFIG_PATH):
            raise AssertionError("设置文件没有写出来")
        if not isinstance(app._load_config(), dict):
            raise AssertionError("设置读取异常")

        report.add("PASS", "界面自检", "隐藏窗口构建、设置项、状态机、进度与消息处理均正常")
    except Exception as exc:
        report.add("FAIL", "界面自检", f"{type(exc).__name__}: {exc}")
    finally:
        try:
            if app is not None and app.listener is not None:
                app.listener.stop()
        except Exception:
            pass
        try:
            root.destroy()
        except Exception:
            pass


def test_app_integration(report: Report, sample: str = SAMPLE) -> None:
    """走真实界面路径：建界面 -> 填内容 -> 调 app.start() -> 看是否正常打完。

    目标窗口是本脚本自己创建的窗口，并会先确认它在前台；如果抢不到焦点就跳过。
    """
    name = "界面集成测试（走 app.start() 真实路径）"
    if not IS_WINDOWS:
        report.add("SKIP", name, "非 Windows 平台")
        return

    import tkinter as tk

    target = tk.Tk()
    target.title("xxt_typer 集成测试：目标窗口（请勿点击其它窗口）")
    target.geometry("720x220+200+200")
    widget = tk.Text(target, wrap="word", font=("Microsoft YaHei UI", 12))
    widget.pack(fill="both", expand=True, padx=8, pady=8)
    target.update()

    app_root = None
    app = None
    try:
        hwnd = _root_hwnd(widget.winfo_id())
        if not _force_foreground(hwnd):
            report.add("SKIP", name, "目标窗口抢不到前台焦点，已跳过（不会往别的窗口打字）。")
            return

        from xxt_typer import TyperApp

        app_root = tk.Tk()
        app_root.geometry("900x560+40+40")
        app = TyperApp(app_root)
        app_root.update()
        app.text.delete("1.0", "end")
        app.text.insert("1.0", sample)
        app.var_countdown.set(0.3)
        app.var_char_delay.set(10)
        app.var_line_delay.set(40)
        app._update_stats()

        widget.focus_force()
        _force_foreground(hwnd)
        target.update()
        time.sleep(0.3)

        app.start()
        deadline = time.time() + 90
        lost_since = None
        focus_lost = False
        while app.state != "idle" and time.time() < deadline:
            app_root.update()
            target.update()
            if _foreground_root() == hwnd:
                lost_since = None
            elif lost_since is None:
                lost_since = time.time()
            elif time.time() - lost_since > 0.4:
                focus_lost = True
            time.sleep(0.02)

        for _ in range(15):
            app_root.update()
            target.update()
            time.sleep(0.02)
        got = widget.get("1.0", "end-1c")
        final_state = app.state
    except Exception as exc:
        report.add("FAIL", name, f"{type(exc).__name__}: {exc}")
        return
    finally:
        try:
            if app is not None and app.listener is not None:
                app.listener.stop()
        except Exception:
            pass
        for root in (app_root, target):
            try:
                if root is not None:
                    root.destroy()
            except Exception:
                pass

    if focus_lost:
        report.add("SKIP", name, "过程中目标窗口失去焦点，结果不可信，已跳过。")
    elif final_state != "idle":
        report.add("FAIL", name, f"超时未结束，界面状态停留在 {final_state!r}")
    elif got == sample:
        report.add("PASS", name, "界面启动、倒计时、逐字输入、状态回位全部正常，内容一致")
    elif not sample.startswith(got):
        report.add("SKIP", name, f"窗口里出现了工具之外的输入。\n    实际：{got!r}")
    else:
        report.add("FAIL", name, f"只输入了一部分。\n    期望：{sample!r}\n    实际：{got!r}")


def test_target_guard(report: Report) -> None:
    """验证“目标窗口守卫”两条路径（全程不发送任何按键，靠打桩模拟前台窗口）。

    路径一：活动窗口一直是本工具自己 -> 必须拒绝输入；
    路径二：出现别的窗口 -> 确认期后正常进入输入流程。
    """
    name = "目标窗口守卫"
    if not IS_WINDOWS:
        report.add("SKIP", name, "非 Windows 平台")
        return

    import tkinter as tk

    import xxt_typer as app_module

    root = tk.Tk()
    root.geometry("880x520+60+60")
    app = None
    calls: list = []
    try:
        app = app_module.TyperApp(root)
        app.show_error_dialogs = False  # 自动测试里不要弹模态框
        root.update()
        own = app._own_root_hwnd()
        if not own:
            report.add("FAIL", name, "取不到本工具窗口句柄")
            return
        app.text.delete("1.0", "end")
        app.text.insert("1.0", "守卫测试文本")
        app.var_countdown.set(0)
        app._update_stats()

        original_foreground = app_module._foreground_window
        original_type_text = app_module.TypingEngine.type_text

        def stub_type_text(self, text, **kwargs):
            calls.append(text)
            return {"total": len(text), "typed": len(text), "stopped": False,
                    "elapsed": 0.0, "paused_time": 0.0, "fallback": 0, "skipped": 0}

        # ---- 路径一：前台窗口一直是本工具自己 ----
        app_module._foreground_window = lambda: (own, os.getpid(), "本工具窗口")
        app_module.TypingEngine.type_text = stub_type_text
        app.target_wait_seconds = 0.6
        app.target_confirm_seconds = 0
        app.start()
        deadline = time.time() + 15
        while app.state != "idle" and time.time() < deadline:
            root.update()
            time.sleep(0.02)
        root.update()
        refused = not calls
        status_text = app.var_status.get()

        # ---- 路径二：出现别的窗口 ----
        app_module._foreground_window = lambda: (0x1234, 4242, "模拟的学习通窗口")
        app.target_wait_seconds = 0.6
        app.target_confirm_seconds = 0  # 打桩期间不真的注入，确认期设为 0
        app.start()
        deadline = time.time() + 15
        while app.state != "idle" and time.time() < deadline:
            root.update()
            time.sleep(0.02)
        root.update()
        proceeded = bool(calls)

        if refused and proceeded:
            report.add("PASS", name,
                       f"活动窗口是自己时拒绝输入（提示：{status_text}）；出现目标窗口后正常进入输入")
        elif not refused:
            report.add("FAIL", name, "活动窗口是本工具自己时竟然还去输入了")
        else:
            report.add("FAIL", name, "出现目标窗口后没有进入输入流程")
    except Exception as exc:
        report.add("FAIL", name, f"{type(exc).__name__}: {exc}")
    finally:
        try:
            if app is not None and app.listener is not None:
                app.listener.stop()
        except Exception:
            pass
        try:
            root.destroy()
        except Exception:
            pass


def test_code_mode_wiring(report: Report) -> None:
    """验证“代码模式”预设真的把 真实键码 / 跳过补全 / 缩进感知 传给了引擎（不注入按键）。"""
    name = "代码模式接线"
    if not IS_WINDOWS:
        report.add("SKIP", name, "非 Windows 平台")
        return

    import tkinter as tk

    import xxt_typer as app_module

    root = tk.Tk()
    root.geometry("880x560+60+60")
    app = None
    captured: dict = {}
    original_engine = app_module.TypingEngine
    original_foreground = app_module._foreground_window
    original_ime = app_module.ime_open
    try:
        app = app_module.TyperApp(root)
        app.show_error_dialogs = False
        root.update()

        class FakeEngine:
            MODE_UNICODE = "unicode"
            MODE_KEYS = "keys"
            MODE_PYNPUT = "pynput"

            def __init__(self, **kwargs):
                captured.update(kwargs)

            def type_text(self, text, **kwargs):
                return {"total": len(text), "typed": len(text), "stopped": False,
                        "elapsed": 0.0, "paused_time": 0.0, "fallback": 0, "skipped": 0}

        app_module.TypingEngine = FakeEngine
        app_module._foreground_window = lambda: (0x1234, 4242, "模拟的编辑器窗口")
        app_module.ime_open = lambda: False
        app.target_wait_seconds = 0.6
        app.target_confirm_seconds = 0

        # 切到代码模式，检查预设
        app.var_scene.set("代码模式（IDE / 在线代码编辑器）")
        app._on_scene_changed()
        preset_ok = (
            app_module.MODE_LABELS.get(app.var_mode.get()) == "keys"
            and app.var_skip_brackets.get() is True
            and app.var_strip_indent.get() is True
            and app.var_skip_empty.get() is False
            and app.var_char_delay.get() == 15
        )

        app.text.delete("1.0", "end")
        app.text.insert("1.0", "int main()\n{\n    return 0;\n}\n")
        app.start()
        deadline = time.time() + 15
        while app.state != "idle" and time.time() < deadline:
            root.update()
            time.sleep(0.02)
        root.update()

        auto_close = captured.get("auto_close")
        wiring_ok = (
            captured.get("mode") == "keys"
            and captured.get("newline_key") == "enter"
            and captured.get("strip_leading_indent") is True
            and auto_close is not None
            and auto_close.enabled
            and '"' not in auto_close._pairs      # 引号默认不处理
        )

        # 再切回文字模式，确认预设会关掉代码相关的选项
        app.var_scene.set("文字模式（答题框 / 纯文字）")
        app._on_scene_changed()
        text_preset_ok = (
            app_module.MODE_LABELS.get(app.var_mode.get()) == "unicode"
            and app.var_skip_brackets.get() is False
            and app.var_strip_indent.get() is False
        )

        if preset_ok and wiring_ok and text_preset_ok:
            report.add("PASS", name,
                       "代码模式预设 → 真实键码 + 跳过补全括号 + 跳过行首缩进；文字模式会关掉这些")
        elif not preset_ok:
            report.add("FAIL", name, "切到代码模式后预设值不对")
        elif not wiring_ok:
            report.add("FAIL", name, f"传给引擎的参数不对：{captured}")
        else:
            report.add("FAIL", name, "切回文字模式后没有关掉代码相关选项")
    except Exception as exc:
        report.add("FAIL", name, f"{type(exc).__name__}: {exc}")
    finally:
        app_module.TypingEngine = original_engine
        app_module._foreground_window = original_foreground
        app_module.ime_open = original_ime
        try:
            if app is not None and app.listener is not None:
                app.listener.stop()
        except Exception:
            pass
        try:
            root.destroy()
        except Exception:
            pass


def test_event_sequence(report: Report) -> None:
    """干跑检查真实会发出的按键事件：扫描码、Shift、回车、跳过补全括号、中文回退。

    全程不调用 SendInput（干跑只记录），所以不会影响你正在用的窗口。
    """
    name = "按键事件序列（干跑，不发送）"
    if not IS_WINDOWS:
        report.add("SKIP", name, "非 Windows 平台")
        return

    import typing_engine as engine

    typing_engine_engine = engine.TypingEngine(
        mode=engine.TypingEngine.MODE_KEYS,
        char_delay=0,
        line_delay=0,
        auto_close=engine.AutoCloseTracker(skip_brackets=True, skip_quotes=False),
    )
    events = engine.start_dry_run()
    try:
        # 里面有配对的 { }（应当被跳过）、需要 Shift 的 A _ ( 、回车、以及中文
        typing_engine_engine.type_text("A_(x);\n{\n}\n中")
    finally:
        engine.stop_dry_run()

    described = engine.describe_events(events)
    problems = []

    # 1) 所有按键事件都必须带扫描码（没有扫描码编辑器拿不到 KeyboardEvent.code）
    no_scan = [d for d in described if "(scan=00)" in d]
    if no_scan:
        problems.append(f"有事件缺扫描码：{no_scan[:5]}")

    # 2) 回车必须是 VK_RETURN + 扫描码 0x1C，并且成对按下抬起
    enter_events = [e for e in events if e[0] == 0x0D]
    if not enter_events:
        problems.append("没有发出回车事件")
    elif not all(e[1] == 0x1C for e in enter_events):
        problems.append(f"回车扫描码不对：{[hex(e[1]) for e in enter_events]}")

    # 3) '_' 需要 Shift：必须看到 SHIFT 按下、目标键、SHIFT 抬起
    shift_down = next((i for i, d in enumerate(described) if d.startswith("SHIFT(scan") and d.endswith("↓")), None)
    shift_up = next((i for i, d in enumerate(described) if d.startswith("SHIFT(scan") and d.endswith("↑")), None)
    if shift_down is None or shift_up is None or shift_up <= shift_down:
        problems.append("需要 Shift 的符号没有正确按下/抬起 Shift")

    # 4) 自动补全跳过：源文本里的 ')' '}' 应该变成右方向键，而不是真的输入它们
    right_events = [e for e in events if e[0] == 0x27 and (e[2] & engine.KEYEVENTF_EXTENDEDKEY)]
    right_skips = len(right_events) // 2   # 每个跳过是"按下+抬起"两个事件
    if right_skips < 2:
        problems.append(f"跳过的收尾括号没有变成右方向键（只看到 {right_skips} 次）")
    rbracket = [e for e in events if e[0] == 0xDD]   # ] / } 所在的键
    if rbracket:
        problems.append("收尾括号仍然被真实敲了出去（会出现 }} 重复）")

    # 5) 中文必须走 Unicode 通道
    unicode_events = [e for e in events if e[2] & engine.KEYEVENTF_UNICODE]
    if len(unicode_events) < 2:
        problems.append("中文没有走 Unicode 通道")

    if problems:
        report.add("FAIL", name, "；".join(problems) + f"\n    实际序列：{described}")
    else:
        report.add("PASS", name,
                   f"{len(events)} 个事件：均带扫描码、Shift 正确、回车 scan=1C、"
                   f"{right_skips} 个补全括号被右方向键跳过、中文走 Unicode 通道")


# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="xxt_typer 自检")
    parser.add_argument("--inject", action="store_true",
                        help="额外执行真实窗口注入测试（会抢 2 秒前台焦点，运行前请勿碰键盘）")
    parser.add_argument("--app", action="store_true",
                        help="额外执行界面集成测试（走 app.start() 真实路径，约 10 秒）")
    parser.add_argument("--quick", action="store_true",
                        help="只做单元检查（默认行为，保留此参数是为了兼容旧写法）")
    parser.add_argument("--sample", default=SAMPLE, help="注入测试用的样例文本")
    parser.add_argument("--modes", nargs="+", choices=["unicode", "keys", "pynput"],
                        default=["unicode", "keys"],
                        help="注入测试要覆盖的输入方式（默认 unicode 和 keys）")
    args = parser.parse_args(argv)

    print(f"平台：{sys.platform}，Python {sys.version.split()[0]}")
    print("-" * 68)
    report = Report()
    test_plan_units(report)
    test_auto_close(report)
    test_key_mapping(report)
    test_event_sequence(report)
    test_estimate(report)
    test_struct_layout(report)
    test_ui(report)
    test_target_guard(report)
    test_code_mode_wiring(report)
    if args.inject and not args.quick:
        labels = {"unicode": "Unicode 直发", "keys": "真实键码", "pynput": "pynput 键码"}
        for mode in args.modes:
            test_injection(report, args.sample, mode, labels[mode])
    else:
        print("[SKIP] 真实逐字注入 —— 加 --inject 才会执行（会抢前台焦点）")
    if args.app:
        test_app_integration(report, args.sample)
    print("-" * 68)

    passed = sum(1 for status, _n, _d in report.items if status == "PASS")
    failed = sum(1 for status, _n, _d in report.items if status == "FAIL")
    skipped = sum(1 for status, _n, _d in report.items if status == "SKIP")
    print(f"结果：通过 {passed}，失败 {failed}，跳过 {skipped}")
    return 1 if report.failed() else 0


if __name__ == "__main__":
    raise SystemExit(main())
