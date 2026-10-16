# -*- coding: utf-8 -*-
"""字符级键盘模拟引擎（网页答题框 / 代码编辑器通用的"逐字输入"内核）。

本模块不碰剪贴板，而是把文本拆成一个个"按键"送进当前前台窗口，
因此学习通等网页用 JavaScript 拦截粘贴（``paste`` / ``Ctrl+V``）的做法对它无效。

三条发送通道
------------
1. ``MODE_UNICODE`` —— 文字模式默认。
   Windows ``SendInput`` + ``KEYEVENTF_UNICODE``，直接把字符送进前台窗口。
   支持中文、全角标点，**不受输入法状态影响**。
   代价：编辑器/浏览器看到的是 ``key="Unidentified"`` 的按键，它自己的智能行为
   （回车自动缩进、括号自动补全、输入收尾括号时跳过已补全的括号）不会触发。

2. ``MODE_KEYS`` —— 代码模式默认，推荐给 IDE / 在线代码编辑器。
   按当前键盘布局用**真实虚拟键码 + 扫描码 + Shift**发送（等同真人敲键盘），
   编辑器收到完整的 ``key`` / ``code`` / ``keyCode``，于是自动补全、跳过已补全的
   收尾括号、回车自动缩进都会正常工作。中文等非 ASCII 字符（例如代码里的中文注释）
   自动回退到 Unicode 通道。要求输入法处于英文状态，工具会检测并提醒。

3. ``MODE_PYNPUT`` —— 兼容模式，用 pynput 的虚拟键码发送，个别老编辑器只认它。

换行
----
``newline_key`` 可选 ``enter``（默认）或 ``shift+enter``：富文本编辑器里
``Shift+Enter`` 是软换行（``<br>``），有时比回车更符合"保留排版"的需要。
回车一律带扫描码发送，避免被编辑器当成无效按键丢掉。

编辑器自动补全
--------------
代码模式下可以打开 :class:`AutoCloseTracker`：我们自己打出的开括号会入栈，
当源文本后面出现对应的收尾括号时，编辑器其实已经替我们补好了，此时发送
**右方向键**跳过去，而不是再输入一个，避免出现 ``}}``、``))`` 这类重复。
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from typing import Callable, List, Optional, Sequence, Tuple

from pynput.keyboard import Controller as _PynputController
from pynput.keyboard import Key

__all__ = [
    "AutoCloseTracker",
    "TypingEngine",
    "describe_events",
    "estimate_seconds",
    "ime_open",
    "plan_units",
    "start_dry_run",
    "stop_dry_run",
    "IS_WINDOWS",
    "windows_unicode_available",
    "layout_keys_available",
]

IS_WINDOWS = sys.platform.startswith("win")

# 文本中被当成"按键"而不是"字符"的东西
KEY_ENTER = "enter"
KEY_TAB = "tab"
KEY_BACKSPACE = "backspace"
KEY_SHIFT_ENTER = "shift+enter"

_VK = {
    KEY_ENTER: 0x0D,        # VK_RETURN
    KEY_TAB: 0x09,          # VK_TAB
    KEY_BACKSPACE: 0x08,    # VK_BACK
}
VK_SHIFT = 0x10
VK_RIGHT = 0x27             # 扩展键

_PYNPUT_KEY = {
    KEY_ENTER: Key.enter,
    KEY_TAB: Key.tab,
    KEY_BACKSPACE: Key.backspace,
}


# ---------------------------------------------------------------------------
# Windows 输入接口封装
# ---------------------------------------------------------------------------
INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

# 自检用的"干跑"出口：设置后只记录将要发送的按键事件，不真的发送
_dry_run_sink: Optional[list] = None

_VK_NAMES = {
    0x08: "BACKSPACE",
    0x09: "TAB",
    0x0D: "ENTER",
    0x10: "SHIFT",
    0x11: "CTRL",
    0x12: "ALT",
    0x1B: "ESC",
    0x20: "SPACE",
    0x25: "LEFT",
    0x26: "UP",
    0x27: "RIGHT",
    0x28: "DOWN",
}


def start_dry_run() -> list:
    """开始"干跑"：接下来所有按键只记录不发送，返回记录列表。"""
    global _dry_run_sink
    _dry_run_sink = []
    return _dry_run_sink


def stop_dry_run() -> None:
    global _dry_run_sink
    _dry_run_sink = None


def describe_events(events: Sequence[Tuple[int, int, int]]) -> List[str]:
    """把记录下来的按键事件翻译成人能看的文本，便于断言与日志。"""
    described = []
    for vk, scan, flags in events:
        arrow = "↑" if flags & KEYEVENTF_KEYUP else "↓"
        if flags & KEYEVENTF_UNICODE:
            described.append(f"UNICODE(0x{scan:04X}){arrow}")
        else:
            name = _VK_NAMES.get(vk, f"VK{vk:02X}")
            extra = "[ext]" if flags & KEYEVENTF_EXTENDEDKEY else ""
            described.append(f"{name}{extra}(scan={scan:02X}){arrow}")
    return described

if IS_WINDOWS:  # pragma: no cover - 平台相关
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = (
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", _ULONG_PTR),
        )

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = (
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", _ULONG_PTR),
        )

    class _HARDWAREINPUT(ctypes.Structure):
        _fields_ = (
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        )

    class _INPUTUNION(ctypes.Union):
        _fields_ = (("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT))

    class _INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = (("type", wintypes.DWORD), ("u", _INPUTUNION))

    _user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int)
    _user32.SendInput.restype = wintypes.UINT
    _user32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
    _user32.MapVirtualKeyW.restype = wintypes.UINT
    _user32.VkKeyScanExW.argtypes = (wintypes.WCHAR, ctypes.c_void_p)
    _user32.VkKeyScanExW.restype = ctypes.c_short
    _user32.GetForegroundWindow.restype = ctypes.c_void_p
    _user32.GetWindowThreadProcessId.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD))
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.GetKeyboardLayout.argtypes = (wintypes.DWORD,)
    _user32.GetKeyboardLayout.restype = ctypes.c_void_p

    def _key_input(vk: int = 0, scan: int = 0, flags: int = 0) -> "_INPUT":
        inp = _INPUT(type=INPUT_KEYBOARD)
        inp.ki = _KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
        return inp

    def _send_inputs(inputs: Sequence["_INPUT"]) -> int:
        n = len(inputs)
        if _dry_run_sink is not None:  # 自检用：只记录，不真发
            for item in inputs:
                _dry_run_sink.append((item.ki.wVk, item.ki.wScan, item.ki.dwFlags))
            return n
        arr = (_INPUT * n)(*inputs)
        sent = _user32.SendInput(n, arr, ctypes.sizeof(_INPUT))
        if sent != n:
            raise ctypes.WinError(ctypes.get_last_error())
        return sent

    def _scan_code(vk: int) -> int:
        """虚拟键码 -> 扫描码。带上扫描码，应用才能拿到正确的 KeyboardEvent.code。"""
        return int(_user32.MapVirtualKeyW(vk, 0)) & 0xFF

    def _send_unicode_char(ch: str) -> None:
        """把一个字符作为 Unicode 字符送入前台窗口（BMP 外按代理对发送）。"""
        raw = ch.encode("utf-16-le")
        for i in range(0, len(raw), 2):
            code = int.from_bytes(raw[i:i + 2], "little")
            _send_inputs((
                _key_input(scan=code, flags=KEYEVENTF_UNICODE),
                _key_input(scan=code, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
            ))

    def _vk_down(vk: int, extended: bool = False) -> None:
        flags = KEYEVENTF_EXTENDEDKEY if extended else 0
        _send_inputs((_key_input(vk=vk, scan=_scan_code(vk), flags=flags),))

    def _vk_up(vk: int, extended: bool = False) -> None:
        flags = KEYEVENTF_EXTENDEDKEY if extended else 0
        _send_inputs((_key_input(vk=vk, scan=_scan_code(vk), flags=flags | KEYEVENTF_KEYUP),))

    def _send_vk(vk: int, extended: bool = False) -> None:
        _vk_down(vk, extended)
        _vk_up(vk, extended)

    def _send_shifted_vk(vk: int, shift: bool) -> None:
        if shift:
            _vk_down(VK_SHIFT)
        _send_vk(vk)
        if shift:
            _vk_up(VK_SHIFT)

    def _current_layout() -> int:
        """前台窗口所在线程的键盘布局（布局跟着窗口走，不能用本进程的）。"""
        try:
            hwnd = _user32.GetForegroundWindow()
            tid = _user32.GetWindowThreadProcessId(hwnd, None) if hwnd else 0
            return int(_user32.GetKeyboardLayout(tid) or 0)
        except Exception:
            return 0

    def _vk_for_char(ch: str, layout: int) -> Optional[Tuple[int, bool]]:
        """按当前布局把字符映射成 (虚拟键码, 是否需要 Shift)；映射不了返回 None。"""
        try:
            result = _user32.VkKeyScanExW(ch, ctypes.c_void_p(layout))
        except Exception:
            return None
        if result == -1:
            return None
        vk = result & 0xFF
        state = (result >> 8) & 0xFF
        if state & 0x06:  # 需要 Ctrl / Alt，放弃，交给 Unicode 通道
            return None
        return vk, bool(state & 0x01)

    def windows_unicode_available() -> bool:
        return True

    def layout_keys_available() -> bool:
        return True

    def ime_open() -> bool:
        """前台窗口上的输入法是否处于打开状态（中文输入法开着时真实键码会变成拼音）。"""
        try:
            imm32 = ctypes.WinDLL("imm32", use_last_error=True)
            imm32.ImmGetContext.argtypes = (ctypes.c_void_p,)
            imm32.ImmGetContext.restype = ctypes.c_void_p
            imm32.ImmGetOpenStatus.argtypes = (ctypes.c_void_p,)
            imm32.ImmGetOpenStatus.restype = wintypes.BOOL
            imm32.ImmReleaseContext.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
            imm32.ImmReleaseContext.restype = wintypes.BOOL

            hwnd = _user32.GetForegroundWindow()
            if not hwnd:
                return False
            context = imm32.ImmGetContext(ctypes.c_void_p(hwnd))
            if not context:
                return False
            try:
                return bool(imm32.ImmGetOpenStatus(ctypes.c_void_p(context)))
            finally:
                imm32.ImmReleaseContext(ctypes.c_void_p(hwnd), ctypes.c_void_p(context))
        except Exception:
            return False

else:  # pragma: no cover - 非 Windows 平台保留接口

    def _send_unicode_char(ch: str) -> None:  # type: ignore[misc]
        raise RuntimeError("Unicode 直发通道仅在 Windows 上可用")

    def _send_vk(vk: int, extended: bool = False) -> None:  # type: ignore[misc]
        raise RuntimeError("虚拟键码通道仅在 Windows 上可用")

    def _send_shifted_vk(vk: int, shift: bool) -> None:  # type: ignore[misc]
        raise RuntimeError("虚拟键码通道仅在 Windows 上可用")

    def windows_unicode_available() -> bool:
        return False

    def layout_keys_available() -> bool:
        return False

    def ime_open() -> bool:
        return False


# ---------------------------------------------------------------------------
# 自动补全跟踪：编辑器替我们补好的收尾括号，改成按右方向键跳过去
# ---------------------------------------------------------------------------
_BRACKET_PAIRS = {")": "(", "]": "[", "}": "{"}
_QUOTE_PAIRS = {'"': '"', "'": "'"}


class AutoCloseTracker:
    """跟踪我们自己打出去的开括号，判断某个收尾符是不是编辑器已经替我们补好了。

    启用后：源文本里的 ``)`` ``]`` ``}``（可选引号）如果是与前面刚打出的开括号配对的，
    说明编辑器在打 ``(`` 时自动补上了收尾符，此时应该按**右方向键**跳过，
    而不是再输入一遍（否则会出现 ``}}`` ``))`` 这种重复）。
    """

    def __init__(self, skip_brackets: bool = True, skip_quotes: bool = False) -> None:
        pairs: dict = {}
        openers: set = set()
        if skip_brackets:
            pairs.update(_BRACKET_PAIRS)
            openers.update(_BRACKET_PAIRS.values())
        if skip_quotes:
            pairs.update(_QUOTE_PAIRS)
            openers.update(_QUOTE_PAIRS.values())
        self._pairs = pairs
        self._openers = openers
        self._stack: List[str] = []
        self.skipped = 0

    @property
    def enabled(self) -> bool:
        return bool(self._pairs)

    def decide(self, ch: str) -> str:
        """返回 ``"skip"``（按右方向键跳过）或 ``"type"``（正常输入该字符）。"""
        opener = self._pairs.get(ch)
        if opener is not None and self._stack and self._stack[-1] == opener:
            self._stack.pop()
            self.skipped += 1
            return "skip"
        return "type"

    def note_typed(self, ch: str) -> None:
        """记录一个真正输入出去的字符（用于维护配对栈）。"""
        if ch in self._openers:
            self._stack.append(ch)

    def reset(self) -> None:
        self._stack.clear()


# ---------------------------------------------------------------------------
# 文本 -> 输入单元
# ---------------------------------------------------------------------------
def plan_units(
    text: str,
    *,
    newline_key: str = KEY_ENTER,
    skip_empty_lines: bool = False,
    strip_leading_indent: bool = False,
) -> List[Tuple[str, str]]:
    """把文本拆成待输入单元。

    返回 ``[(kind, value), ...]``：

    * ``("char", 单个字符)``
    * ``("key", "enter"/"tab"/"backspace")``
    * ``("combo", "shift+enter")``

    ``strip_leading_indent``：编辑器回车后会自动缩进，此时源文本的行首空格会变成
    双倍缩进，打开这个开关就跳过每行（第一行除外）行首的空格和 Tab。
    """
    if newline_key not in (KEY_ENTER, KEY_SHIFT_ENTER):
        raise ValueError(f"未知换行方式: {newline_key!r}")

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    units: List[Tuple[str, str]] = []

    for index, line in enumerate(lines):
        is_last = index == len(lines) - 1
        if skip_empty_lines and not is_last and line.strip() == "":
            continue
        if strip_leading_indent and index > 0 and not is_last:
            line = line.lstrip(" \t")
        for ch in line:
            if ch == "\t":
                units.append(("key", KEY_TAB))
            elif ch == "\b":
                units.append(("key", KEY_BACKSPACE))
            else:
                units.append(("char", ch))
        if not is_last:
            units.append(("combo", KEY_SHIFT_ENTER) if newline_key == KEY_SHIFT_ENTER
                         else ("key", KEY_ENTER))
    return units


class TypingEngine:
    """逐字发送文本。

    参数
    ----
    mode            : ``"unicode"`` / ``"keys"`` / ``"pynput"``，见模块说明。
    char_delay      : 每个字符之间的间隔（秒）。
    line_delay      : 每次换行之后的间隔（秒），让编辑器有时间处理自动缩进。
    newline_key     : ``"enter"`` 或 ``"shift+enter"``。
    skip_empty_lines: 是否丢掉空行。
    strip_leading_indent: 换行后跳过行首缩进（编辑器自动缩进时用）。
    auto_close      : :class:`AutoCloseTracker` 或 ``None``。
    on_progress     : ``callable(done, total, unit)``。
    on_log          : ``callable(str)``，记录回退/异常等信息。
    """

    MODE_UNICODE = "unicode"
    MODE_KEYS = "keys"
    MODE_PYNPUT = "pynput"

    def __init__(
        self,
        mode: str = MODE_UNICODE,
        char_delay: float = 0.03,
        line_delay: float = 0.15,
        newline_key: str = KEY_ENTER,
        skip_empty_lines: bool = False,
        strip_leading_indent: bool = False,
        auto_close: Optional[AutoCloseTracker] = None,
        on_progress: Optional[Callable[[int, int, Tuple[str, str]], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
    ) -> None:
        if mode not in (self.MODE_UNICODE, self.MODE_KEYS, self.MODE_PYNPUT):
            raise ValueError(f"未知输入方式: {mode!r}")
        self.mode = mode
        self.char_delay = max(0.0, float(char_delay))
        self.line_delay = max(0.0, float(line_delay))
        self.newline_key = newline_key
        self.skip_empty_lines = bool(skip_empty_lines)
        self.strip_leading_indent = bool(strip_leading_indent)
        self.auto_close = auto_close if auto_close is not None else AutoCloseTracker(False, False)
        self.on_progress = on_progress
        self.on_log = on_log

        self.fallback_count = 0   # 回退到 Unicode 通道的次数
        self.layout_refreshes = 0
        self._kb = _PynputController()
        self._layout = _current_layout() if layout_keys_available() else 0

    # -- 内部工具 ---------------------------------------------------------
    def _log(self, message: str) -> None:
        if self.on_log is not None:
            try:
                self.on_log(message)
            except Exception:
                pass

    def _tap_key(self, name: str) -> None:
        if name == KEY_SHIFT_ENTER:
            self._send_combo()
            return
        # 只要能用系统通道就用它（带扫描码，编辑器才认这个回车）
        if self.mode in (self.MODE_UNICODE, self.MODE_KEYS) and windows_unicode_available():
            _send_vk(_VK[name])
            return
        key = _PYNPUT_KEY[name]
        self._kb.press(key)
        self._kb.release(key)

    def _send_combo(self) -> None:
        """Shift + Enter（富文本编辑器里的软换行）。"""
        if self.mode in (self.MODE_UNICODE, self.MODE_KEYS) and windows_unicode_available():
            _vk_down(VK_SHIFT)
            _send_vk(_VK[KEY_ENTER])
            _vk_up(VK_SHIFT)
            return
        self._kb.press(Key.shift)
        self._kb.press(Key.enter)
        self._kb.release(Key.enter)
        self._kb.release(Key.shift)

    def _send_char(self, ch: str) -> None:
        if self.mode == self.MODE_KEYS and layout_keys_available():
            mapped = _vk_for_char(ch, self._layout)
            if mapped is not None:
                vk, shift = mapped
                _send_shifted_vk(vk, shift)
                return
            # 中文等当前布局打不出来的字符，回退到 Unicode 通道
            self.fallback_count += 1
            if self.fallback_count == 1:
                self._log(f"字符 {ch!r} 当前键盘布局打不出来，已改用 Unicode 直发")
            _send_unicode_char(ch)
            return

        if self.mode == self.MODE_UNICODE and windows_unicode_available():
            _send_unicode_char(ch)
            return

        try:
            self._kb.type(ch)
        except Exception as exc:  # 非 ASCII 字符在部分平台上 pynput 发不出去
            if windows_unicode_available():
                self.fallback_count += 1
                if self.fallback_count == 1:
                    self._log(f"pynput 无法直接输入 {ch!r}，已自动切换为 Unicode 直发（{exc}）")
                _send_unicode_char(ch)
            else:
                raise

    def _skip_one(self) -> None:
        """按一下右方向键（跳过编辑器自动补上的收尾符）。"""
        if windows_unicode_available():
            _send_vk(VK_RIGHT, extended=True)
        else:
            self._kb.press(Key.right)
            self._kb.release(Key.right)

    # -- 主流程 -----------------------------------------------------------
    def type_text(
        self,
        text: str,
        *,
        stop_event: Optional[threading.Event] = None,
        pause_event: Optional[threading.Event] = None,
    ) -> dict:
        """把 ``text`` 输入到当前前台窗口。

        返回统计字典：``{"total", "typed", "stopped", "elapsed", "paused_time",
        "fallback", "skipped"}``。
        ``stop_event`` 置位则立即停止；``pause_event`` 置位则挂起等待。
        """
        units = plan_units(
            text,
            newline_key=self.newline_key,
            skip_empty_lines=self.skip_empty_lines,
            strip_leading_indent=self.strip_leading_indent,
        )
        total = len(units)
        typed = 0
        stopped = False
        paused_time = 0.0
        started = time.time()
        self.auto_close.reset()

        for unit in units:
            if stop_event is not None and stop_event.is_set():
                stopped = True
                break

            if pause_event is not None and pause_event.is_set():
                pause_started = time.time()
                while pause_event.is_set():
                    if stop_event is not None and stop_event.is_set():
                        stopped = True
                        break
                    time.sleep(0.05)
                paused_time += time.time() - pause_started
                if stopped:
                    break

            kind, value = unit
            if kind == "combo":
                self._tap_key(value)
                delay = self.line_delay
            elif kind == "key":
                self._tap_key(value)
                delay = self.line_delay
            else:
                if self.auto_close.enabled and self.auto_close.decide(value) == "skip":
                    self._skip_one()
                else:
                    self._send_char(value)
                    if self.auto_close.enabled:
                        self.auto_close.note_typed(value)
                delay = self.char_delay

            typed += 1
            if self.on_progress is not None:
                try:
                    self.on_progress(typed, total, unit)
                except Exception:
                    pass
            if delay:
                time.sleep(delay)

            # 前台窗口换了（输入法/布局也可能跟着换），定期重新取一次布局
            if self.mode == self.MODE_KEYS and typed % 80 == 0:
                self._layout = _current_layout()
                self.layout_refreshes += 1

        return {
            "total": total,
            "typed": typed,
            "stopped": stopped,
            "elapsed": time.time() - started,
            "paused_time": paused_time,
            "fallback": self.fallback_count,
            "skipped": self.auto_close.skipped,
        }


def estimate_seconds(
    text: str,
    *,
    char_delay: float = 0.03,
    line_delay: float = 0.15,
    newline_key: str = KEY_ENTER,
    skip_empty_lines: bool = False,
    strip_leading_indent: bool = False,
) -> float:
    """粗略估算输入整段文本需要多少秒。"""
    units = plan_units(
        text,
        newline_key=newline_key,
        skip_empty_lines=skip_empty_lines,
        strip_leading_indent=strip_leading_indent,
    )
    total = 0.0
    for kind, _value in units:
        total += line_delay if kind in ("key", "combo") and _value in (KEY_ENTER, KEY_SHIFT_ENTER) else char_delay
    return total
