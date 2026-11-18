"""Windows-native extras via ``ctypes`` (no pywin32): DPI awareness, dark title
bar, drag-and-drop, pasting files, the global hotkey and the tray icon.

Hotkey service design
---------------------
* One dedicated thread owns the window, the hotkey, the tray icon and the message
  loop (``GetMessage`` must run on the thread that created the window).
* The ``INPUT`` union includes ``MOUSEINPUT`` so ``sizeof(INPUT)`` is right and
  ``SendInput`` accepts the events — no ``keybd_event`` hack.
* Instead of injecting fake key-ups for keys the user is still holding, we wait
  for them to be released, and "mask" the Alt release so apps don't open their menu bar.
* The user's clipboard is restored after grabbing the selection.
"""

from __future__ import annotations

import ctypes
import logging
import queue
import sys
import threading
import time
from ctypes import wintypes
from typing import Callable, List, Optional, Tuple

log = logging.getLogger("buzz")

if sys.platform != "win32":  # pragma: no cover
    raise ImportError("winsys is Windows-only")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

# --- prototypes ---------------------------------------------------------------
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.CallWindowProcW.restype = LRESULT
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
user32.SetWindowLongPtrW.restype = ctypes.c_void_p
user32.GetParent.argtypes = [wintypes.HWND]
user32.GetParent.restype = wintypes.HWND
user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = ctypes.c_int
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                   wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.LoadImageW.restype = wintypes.HANDLE
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                  wintypes.HWND, ctypes.c_void_p]
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
user32.RegisterWindowMessageW.restype = wintypes.UINT
user32.SendInput.argtypes = [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
shell32.DragQueryFileW.restype = wintypes.UINT
shell32.DragFinish.argtypes = [wintypes.HANDLE]
dwmapi.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]

WM_DESTROY, WM_CLOSE, WM_COMMAND, WM_HOTKEY = 0x0002, 0x0010, 0x0111, 0x0312
WM_DROPFILES, WM_APP, WM_USER = 0x0233, 0x8000, 0x0400
WM_LBUTTONUP, WM_RBUTTONUP, WM_CONTEXTMENU, WM_NULL = 0x0202, 0x0205, 0x007B, 0x0000
CF_UNICODETEXT, CF_HDROP = 13, 15
GMEM_MOVEABLE = 0x0002


# =============================================================================
# Process / window cosmetics
# =============================================================================
def set_dpi_awareness() -> None:
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor v2
        return
    except Exception:
        pass
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def set_app_id(app_id: str) -> None:
    try:
        shell32.SetCurrentProcessExplicitAppUserModelID(ctypes.c_wchar_p(app_id))
    except Exception:
        pass


def _colorref(hex_color: str) -> int:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return r | (g << 8) | (b << 16)


def toplevel_hwnd(tk_widget) -> int:
    hwnd = user32.GetParent(tk_widget.winfo_id())
    return hwnd or tk_widget.winfo_id()


def style_titlebar(tk_root, dark: bool, caption: Optional[str] = None, text: Optional[str] = None) -> None:
    """Dark title bar (Win10 1809+) and, on Windows 11, a caption colour that matches the app."""
    hwnd = toplevel_hwnd(tk_root)
    val = ctypes.c_int(1 if dark else 0)
    for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (new / pre-20H1)
        if dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), 4) == 0:
            break
    if caption:
        c = ctypes.c_int(_colorref(caption))
        dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(c), 4)   # DWMWA_CAPTION_COLOR
        dwmapi.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(c), 4)   # DWMWA_BORDER_COLOR
    if text:
        t = ctypes.c_int(_colorref(text))
        dwmapi.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(t), 4)   # DWMWA_TEXT_COLOR
    # Windows keeps showing the title bar it already painted (plain white) until the next
    # non-client repaint — which used to happen only once you clicked the window. Force it now.
    try:
        # SWP_NOMOVE | NOSIZE | NOZORDER | NOACTIVATE | FRAMECHANGED
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020)
        user32.RedrawWindow(hwnd, 0, 0, 0x0001 | 0x0400 | 0x0100)  # RDW_INVALIDATE | FRAME | UPDATENOW
    except Exception:
        pass


def nudge_frame(tk_root) -> bool:
    """Resize the window by 1 px and back, which makes Windows repaint a stale (white) title bar.

    Returns False — and does nothing — if the window is hidden, minimized or maximized.
    """
    hwnd = toplevel_hwnd(tk_root)
    if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd) or user32.IsZoomed(hwnd):
        return False
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False
    w, h = rect.right - rect.left, rect.bottom - rect.top
    flags = 0x0002 | 0x0004 | 0x0010  # SWP_NOMOVE | NOZORDER | NOACTIVATE
    user32.SetWindowPos(hwnd, 0, 0, 0, w + 1, h, flags)
    user32.SetWindowPos(hwnd, 0, 0, 0, w, h, flags)
    return True


# =============================================================================
# Drag & drop of files onto the window (WM_DROPFILES)
# =============================================================================
def _hdrop_paths(hdrop) -> List[str]:
    count = shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
    paths = []
    for i in range(count):
        n = shell32.DragQueryFileW(hdrop, i, None, 0)
        buf = ctypes.create_unicode_buffer(n + 1)
        shell32.DragQueryFileW(hdrop, i, buf, n + 1)
        paths.append(buf.value)
    return paths


class FileDrop:
    """Subclasses the Tk top-level window so files dropped from Explorer reach *callback*."""

    def __init__(self, tk_root, callback: Callable[[List[str]], None]) -> None:
        self.callback = callback
        self.hwnd = toplevel_hwnd(tk_root)
        self._proc = WNDPROC(self._wndproc)
        shell32.DragAcceptFiles(self.hwnd, True)
        self._old = user32.SetWindowLongPtrW(self.hwnd, -4, ctypes.cast(self._proc, ctypes.c_void_p))

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_DROPFILES:
            try:
                paths = _hdrop_paths(wparam)
            finally:
                shell32.DragFinish(wparam)
            try:
                self.callback(paths)
            except Exception:
                log.exception("drop handler failed")
            return 0
        return user32.CallWindowProcW(self._old, hwnd, msg, wparam, lparam)


# =============================================================================
# Clipboard
# =============================================================================
def _open_clipboard(hwnd=None, tries: int = 10) -> bool:
    for _ in range(tries):
        if user32.OpenClipboard(hwnd):
            return True
        time.sleep(0.02)
    return False


def clipboard_files() -> List[str]:
    """Files copied in Explorer (Ctrl+C), for paste-to-send."""
    if not user32.IsClipboardFormatAvailable(CF_HDROP) or not _open_clipboard():
        return []
    try:
        h = user32.GetClipboardData(CF_HDROP)
        return _hdrop_paths(h) if h else []
    finally:
        user32.CloseClipboard()


def get_clipboard_text(hwnd=None) -> Optional[str]:
    if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT) or not _open_clipboard(hwnd):
        return None
    try:
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = kernel32.GlobalLock(h)
        try:
            return ctypes.wstring_at(p) if p else None
        finally:
            kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


def clear_clipboard_if_text(expected: str) -> bool:
    """Empty the clipboard only if it still holds exactly *expected* text.

    Check and clear happen under one clipboard lock, so something copied in the
    meantime can never be wiped by mistake.
    """
    if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT) or not _open_clipboard():
        return False
    try:
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return False
        p = kernel32.GlobalLock(h)
        try:
            current = ctypes.wstring_at(p) if p else None
        finally:
            kernel32.GlobalUnlock(h)
        if current is None or current.strip() != expected.strip():
            return False
        user32.EmptyClipboard()
        return True
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text: str, hwnd=None) -> bool:
    if not _open_clipboard(hwnd):
        return False
    try:
        user32.EmptyClipboard()
        data = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(data)
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not h:
            return False
        p = kernel32.GlobalLock(h)
        ctypes.memmove(p, data, size)
        kernel32.GlobalUnlock(h)
        if not user32.SetClipboardData(CF_UNICODETEXT, h):
            kernel32.GlobalFree(h)
            return False
        return True
    finally:
        user32.CloseClipboard()


# =============================================================================
# Keyboard injection (correct INPUT layout)
# =============================================================================
class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


KEYEVENTF_KEYUP = 0x0002
VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN, VK_C = 0x10, 0x11, 0x12, 0x5B, 0x5C, 0x43
VK_MASK = 0xE8  # unassigned — used to stop a lone Alt release from opening menus


def send_keys(events: List[Tuple[int, bool]]) -> int:
    arr = (INPUT * len(events))()
    for i, (vk, up) in enumerate(events):
        arr[i].type = 1
        arr[i].u.ki = KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP if up else 0, 0, 0)
    return user32.SendInput(len(events), ctypes.byref(arr), ctypes.sizeof(INPUT))


def _held(vk: int) -> bool:
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


# =============================================================================
# Hotkey specs
# =============================================================================
MODS = {"ALT": 0x1, "CTRL": 0x2, "CONTROL": 0x2, "SHIFT": 0x4, "WIN": 0x8}
_KEYS = {"F%d" % i: 0x6F + i for i in range(1, 25)}
_KEYS.update({chr(c): c for c in range(ord("A"), ord("Z") + 1)})
_KEYS.update({str(d): 0x30 + d for d in range(10)})
_KEYS.update({"INSERT": 0x2D, "PAUSE": 0x13, "HOME": 0x24, "END": 0x23})


def parse_hotkey(spec: str) -> Optional[Tuple[int, int]]:
    parts = [p.strip().upper() for p in (spec or "").split("+") if p.strip()]
    if not parts or parts[-1] not in _KEYS:
        return None
    mods = 0
    for m in parts[:-1]:
        if m not in MODS:
            return None
        mods |= MODS[m]
    key = parts[-1]
    if not key.startswith("F") and not mods & (MODS["CTRL"] | MODS["ALT"] | MODS["WIN"]):
        return None  # a bare or Shift+letter would hijack normal typing (capital letters!)
    return mods, _KEYS[key]


# =============================================================================
# Shell thread: hidden window + tray icon + global hotkey
# =============================================================================
class GUID(ctypes.Structure):
    _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD), ("d", ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT),
                ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", GUID), ("hBalloonIcon", wintypes.HICON)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wintypes.ATOM

NIM_ADD, NIM_MODIFY, NIM_DELETE, NIM_SETVERSION = 0, 1, 2, 4
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO, NIF_SHOWTIP = 0x1, 0x2, 0x4, 0x10, 0x80
TRAY_MSG = WM_APP + 1
CMD_HOTKEY = WM_APP + 2
CMD_TRAY = WM_APP + 3
CMD_NOTIFY = WM_APP + 4
CMD_QUIT = WM_APP + 5
MENU_OPEN, MENU_RECEIVE, MENU_SEND, MENU_QUIT = 1001, 1002, 1003, 1004


class ShellThread:
    """Owns the global hotkey and tray icon. Results arrive on :attr:`events` as tuples:

    ``("codes", [code, ...])`` · ``("nolink",)`` · ``("hotkey_failed", spec)`` ·
    ``("open",)`` · ``("receive_clipboard",)`` · ``("send",)`` · ``("quit",)``
    """

    def __init__(self, icon_path: str, find_codes: Callable[[str], List[str]]) -> None:
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.icon_path = icon_path
        self.find_codes = find_codes
        self.hwnd = None
        self._ready = threading.Event()
        self._hotkey: Optional[Tuple[int, int]] = None
        self._hotkey_spec = ""
        self._pending_hotkey: Optional[str] = None
        self._tray_wanted = False
        self._tray_shown = False
        self._notify_args: "queue.Queue[Tuple[str, str]]" = queue.Queue()
        self._hicon = None
        self._busy = False
        self._v4 = False
        self._taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
        self._thread = threading.Thread(target=self._run, daemon=True, name="win-shell")
        self._thread.start()
        self._ready.wait(3)

    # ---------------------------------------------------------------- public
    def set_hotkey(self, spec: Optional[str]) -> None:
        self._pending_hotkey = spec or ""
        self._post(CMD_HOTKEY)

    def set_tray(self, on: bool) -> None:
        self._tray_wanted = on
        self._post(CMD_TRAY)

    def notify(self, title: str, message: str) -> None:
        self._notify_args.put((title[:63], message[:255]))
        self._post(CMD_NOTIFY)

    def stop(self) -> None:
        self._post(CMD_QUIT)
        self._thread.join(2)

    def _post(self, msg: int) -> None:
        if self.hwnd:
            user32.PostMessageW(self.hwnd, msg, 0, 0)

    # ---------------------------------------------------------------- thread
    def _run(self) -> None:
        hinst = kernel32.GetModuleHandleW(None)
        self._wndproc_ref = WNDPROC(self._wndproc)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc_ref
        wc.hInstance = hinst
        wc.lpszClassName = "BuzzDropShell"
        user32.RegisterClassW(ctypes.byref(wc))
        self.hwnd = user32.CreateWindowExW(0, "BuzzDropShell", "BuzzDrop", 0, 0, 0, 0, 0,
                                           None, None, hinst, None)
        try:
            self._hicon = user32.LoadImageW(None, self.icon_path, 1, 0, 0, 0x10 | 0x40)  # file | defaultsize
        except Exception:
            self._hicon = None
        self._ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        self._remove_tray()

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_HOTKEY:
                self._on_hotkey()
                return 0
            if msg == TRAY_MSG:
                event = lparam & 0xFFFF
                if event == WM_LBUTTONUP:
                    self.events.put(("open",))
                elif event == WM_CONTEXTMENU or (event == WM_RBUTTONUP and not self._v4):
                    self._show_menu()
                return 0
            if msg == WM_COMMAND:
                cmd = wparam & 0xFFFF
                self.events.put({MENU_OPEN: ("open",), MENU_RECEIVE: ("receive_clipboard",),
                                 MENU_SEND: ("send",), MENU_QUIT: ("quit",)}.get(cmd, ("open",)))
                return 0
            if msg == CMD_HOTKEY:
                self._apply_hotkey()
                return 0
            if msg == CMD_TRAY:
                self._apply_tray()
                return 0
            if msg == CMD_NOTIFY:
                while not self._notify_args.empty():
                    self._balloon(*self._notify_args.get())
                return 0
            if msg == CMD_QUIT:
                if self._hotkey:
                    user32.UnregisterHotKey(hwnd, 1)
                self._remove_tray()
                user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            if msg == self._taskbar_created and self._tray_shown:  # Explorer restarted
                self._tray_shown = False
                self._apply_tray()
                return 0
        except Exception:
            log.exception("shell window proc failed")
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # -- hotkey ------------------------------------------------------------
    def _apply_hotkey(self) -> None:
        spec = self._pending_hotkey
        if self._hotkey:
            user32.UnregisterHotKey(self.hwnd, 1)
            self._hotkey = None
        if not spec:
            return
        parsed = parse_hotkey(spec)
        if not parsed:
            self.events.put(("hotkey_failed", spec))
            return
        if user32.RegisterHotKey(self.hwnd, 1, parsed[0] | 0x4000, parsed[1]):  # MOD_NOREPEAT
            self._hotkey, self._hotkey_spec = parsed, spec
            log.info("Global hotkey %s registered", spec)
        else:
            log.warning("RegisterHotKey(%s) failed: %d", spec, ctypes.get_last_error())
            self.events.put(("hotkey_failed", spec))

    def _on_hotkey(self) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            text, previous = self._copy_selection()
            codes = self.find_codes(text or "") or self.find_codes(previous or "")
            self.events.put(("codes", codes) if codes else ("nolink",))
        finally:
            self._busy = False

    def _copy_selection(self) -> Tuple[Optional[str], Optional[str]]:
        """Copy whatever is selected in the foreground app and return (selection, old clipboard)."""
        mods_vk = (VK_MENU, VK_CONTROL, VK_SHIFT, VK_LWIN, VK_RWIN)
        if _held(VK_MENU) or _held(VK_LWIN) or _held(VK_RWIN):
            send_keys([(VK_MASK, False), (VK_MASK, True)])  # Alt/Win tap → no menu / Start menu
        key_vk = self._hotkey[1] if self._hotkey else 0
        deadline = time.monotonic() + 0.8
        while time.monotonic() < deadline and (any(_held(v) for v in mods_vk) or (key_vk and _held(key_vk))):
            time.sleep(0.01)
        still = [v for v in mods_vk if _held(v)]
        if still:  # user is still holding modifiers — release them for the copy
            send_keys([(v, True) for v in still])
        previous = get_clipboard_text(self.hwnd)
        seq = user32.GetClipboardSequenceNumber()
        sent = send_keys([(VK_CONTROL, False), (VK_C, False), (VK_C, True), (VK_CONTROL, True)])
        if sent != 4:
            log.warning("SendInput sent %d/4 (err %d)", sent, ctypes.get_last_error())
            return None, previous
        end = time.monotonic() + 0.45
        while time.monotonic() < end and user32.GetClipboardSequenceNumber() == seq:
            time.sleep(0.015)
        if user32.GetClipboardSequenceNumber() == seq:
            return None, previous  # nothing was selected
        time.sleep(0.03)
        text = get_clipboard_text(self.hwnd)
        if previous is not None and text != previous:
            set_clipboard_text(previous, self.hwnd)  # give the user their clipboard back
        return text, previous

    # -- tray ----------------------------------------------------------------
    def _nid(self) -> NOTIFYICONDATAW:
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        return nid

    def _apply_tray(self) -> None:
        if self._tray_wanted and not self._tray_shown:
            nid = self._nid()
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP
            nid.uCallbackMessage = TRAY_MSG
            nid.hIcon = self._hicon
            nid.szTip = "BuzzDrop"
            self._tray_shown = bool(shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)))
            nid.uVersion = 4
            self._v4 = bool(shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(nid)))
        elif not self._tray_wanted:
            self._remove_tray()

    def _remove_tray(self) -> None:
        if self._tray_shown:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid()))
            self._tray_shown = False

    def _balloon(self, title: str, message: str) -> None:
        temporary = not self._tray_shown
        if temporary:  # briefly add an icon so Windows can show the notification
            wanted = self._tray_wanted
            self._tray_wanted = True
            self._apply_tray()
            self._tray_wanted = wanted
        nid = self._nid()
        nid.uFlags = NIF_INFO
        nid.szInfoTitle = title
        nid.szInfo = message
        nid.dwInfoFlags = 0x4 | 0x20  # NIIF_USER | NIIF_LARGE_ICON
        nid.hBalloonIcon = self._hicon
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        if temporary:
            threading.Timer(8.0, lambda: self._post(CMD_TRAY)).start()

    def _show_menu(self) -> None:
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, 0, MENU_OPEN, "Open BuzzDrop")
        user32.AppendMenuW(menu, 0, MENU_RECEIVE, "Download link from clipboard")
        user32.AppendMenuW(menu, 0, MENU_SEND, "Send files…")
        user32.AppendMenuW(menu, 0x800, 0, None)  # separator
        user32.AppendMenuW(menu, 0, MENU_QUIT, "Quit")
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self.hwnd)
        user32.TrackPopupMenu(menu, 0x0020, pt.x, pt.y, 0, self.hwnd, None)  # TPM_RIGHTBUTTON
        user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(menu)
