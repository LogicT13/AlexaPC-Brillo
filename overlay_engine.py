"""overlay_engine.py :: Software dimmer / blackout overlay for the secondary monitor.

Architecture
------------
* Uses pure Win32 API (ctypes) - no tkinter dependency.
* Creates a layered window in its own dedicated thread using Win32 CreateWindowExW.
* Thread-safe state machine: commands are posted via PostThreadMessage (WM_APP).
* Three states:
    0 - OFF       : window hidden (normal desktop)
    1 - DIM       : black overlay ~60% opacity (154/255)
    2 - BLACKOUT  : solid black 100% opacity

Safety
------
* ESC key (WM_KEYDOWN VK_ESCAPE) in the overlay -> immediate state 0 (hidden).
* Ctrl+Alt+B and Ctrl+Alt+ESC handled by hotkey_listener.py via RegisterHotKey.
* No autostart configured.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# State constants
# ---------------------------------------------------------------------------
STATE_OFF = 0
STATE_DIM = 1
STATE_BLACKOUT = 2

DIM_ALPHA = 154       # ~60% opacity (0-255)
BLACKOUT_ALPHA = 255  # 100% opacity

# Custom Windows message: WM_APP + offset, used to post state changes
WM_APP_SET_STATE = 0x8000 + 1  # WM_APP = 0x8000

# Win32 constants
WS_EX_LAYERED    = 0x00080000
WS_EX_TRANSPARENT= 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST    = 0x00000008  # Note: use SetWindowPos for TOPMOST
WS_POPUP         = 0x80000000
GWL_EXSTYLE      = -20
HWND_TOPMOST     = wintypes.HWND(-1)
SWP_NOMOVE       = 0x0002
SWP_NOSIZE       = 0x0001
SWP_NOACTIVATE   = 0x0010
LWA_ALPHA        = 0x00000002
WM_DESTROY       = 0x0002
WM_KEYDOWN       = 0x0100
WM_CLOSE         = 0x0010
VK_ESCAPE        = 0x1B
CS_HREDRAW       = 0x0002
CS_VREDRAW       = 0x0001
COLOR_WINDOW     = 5
IDC_ARROW        = 32512
SW_HIDE          = 0
SW_SHOWNA        = 8  # show without activating
WM_QUIT          = 0x0012


# ---------------------------------------------------------------------------
# Monitor detection
# ---------------------------------------------------------------------------

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# Fix ctypes 64-bit pointer truncation by specifying argtypes for HWND parameters
user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND, 
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, 
    ctypes.c_uint
]
user32.SetLayeredWindowAttributes.argtypes = [
    wintypes.HWND, ctypes.c_uint, ctypes.c_ubyte, ctypes.c_uint
]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_uint]
user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]

user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [
    ctypes.c_uint, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_uint,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HWND, wintypes.HINSTANCE, ctypes.c_void_p
]
kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

def _enum_monitors() -> list[dict]:
    monitors: list[dict] = []

    MonitorEnumProc = ctypes.WINFUNCTYPE(
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.POINTER(wintypes.RECT),
        ctypes.c_long,
    )

    def _cb(hMonitor, hdcMonitor, lprcMonitor, dwData) -> int:
        r = lprcMonitor.contents
        monitors.append({
            "left":   r.left,
            "top":    r.top,
            "right":  r.right,
            "bottom": r.bottom,
            "width":  r.right - r.left,
            "height": r.bottom - r.top,
        })
        return 1

    proc = MonitorEnumProc(_cb)
    ctypes.windll.user32.EnumDisplayMonitors(None, None, proc, 0)
    return monitors


def get_secondary_monitor() -> Optional[dict]:
    monitors = _enum_monitors()
    if len(monitors) < 2:
        logger.warning("Only one monitor detected.")
        return None
    for m in monitors:
        if m["left"] != 0 or m["top"] != 0:
            return m
    return monitors[1] if len(monitors) > 1 else None


# ---------------------------------------------------------------------------
# Win32 WNDCLASSEX structure
# ---------------------------------------------------------------------------

class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize",        ctypes.c_uint),
        ("style",         ctypes.c_uint),
        ("lpfnWndProc",   ctypes.c_void_p),
        ("cbClsExtra",    ctypes.c_int),
        ("cbWndExtra",    ctypes.c_int),
        ("hInstance",     wintypes.HANDLE),
        ("hIcon",         wintypes.HANDLE),
        ("hCursor",       wintypes.HANDLE),
        ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName",  wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm",       wintypes.HANDLE),
    ]


# ---------------------------------------------------------------------------
# Overlay Engine
# ---------------------------------------------------------------------------

class OverlayEngine:
    """Singleton overlay controller using pure Win32 API."""

    _instance: Optional["OverlayEngine"] = None
    _class_lock = threading.Lock()

    def __new__(cls) -> "OverlayEngine":
        with cls._class_lock:
            if cls._instance is None:
                obj = super().__new__(cls)
                obj._initialised = False
                cls._instance = obj
        return cls._instance

    def __init__(self) -> None:
        if self._initialised:
            return
        self._initialised = True

        self._state_lock = threading.Lock()
        self._current_state: int = STATE_OFF
        self._hwnd: int = 0
        self._thread_id: int = 0
        self._monitor: Optional[dict] = None
        self._ready = threading.Event()

        self._thread = threading.Thread(
            target=self._window_thread, daemon=True, name="OverlayWin32"
        )
        self._thread.start()
        self._ready.wait(timeout=5.0)

    # ------------------------------------------------------------------
    # Win32 window thread
    # ------------------------------------------------------------------

    def _window_thread(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        self._monitor = get_secondary_monitor()
        if self._monitor is None:
            logger.error("Secondary monitor not found.")
            self._ready.set()
            return

        m = self._monitor
        logger.info(
            "Secondary monitor detected: %d,%d %dx%d",
            m["left"], m["top"], m["width"], m["height"]
        )

        hInstance = kernel32.GetModuleHandleW(None)
        class_name = "BrilloOverlay"

        # Window procedure - use LRESULT (c_longlong on 64-bit) for return
        WndProcType = ctypes.WINFUNCTYPE(
            ctypes.c_longlong,   # LRESULT
            wintypes.HWND,
            ctypes.c_uint,       # UINT msg
            ctypes.c_ulonglong,  # WPARAM
            ctypes.c_longlong,   # LPARAM
        )

        # Fix DefWindowProcW return type for 64-bit
        _DefWindowProc = user32.DefWindowProcW
        _DefWindowProc.restype = ctypes.c_longlong
        _DefWindowProc.argtypes = [
            wintypes.HWND, ctypes.c_uint,
            ctypes.c_ulonglong, ctypes.c_longlong
        ]

        WM_TIMER = 0x0113
        TIMER_ID = 1

        def wnd_proc(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
            if msg == WM_KEYDOWN and wparam == VK_ESCAPE:
                logger.warning("ESC pressed in overlay - forcing OFF.")
                self._apply_state(STATE_OFF)
                with self._state_lock:
                    self._current_state = STATE_OFF
                return 0
            if msg == WM_TIMER and wparam == TIMER_ID:
                # Aggressively enforce TOPMOST periodically
                # Only apply if we are supposed to be visible (state > 0)
                if self._current_state > 0:
                    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
                return 0
            if msg == WM_DESTROY:
                user32.KillTimer(hwnd, TIMER_ID)
                user32.PostQuitMessage(0)
                return 0
            return _DefWindowProc(hwnd, msg, wparam, lparam)

        self._wnd_proc = WndProcType(wnd_proc)  # keep reference alive

        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.style = CS_HREDRAW | CS_VREDRAW
        wc.lpfnWndProc = ctypes.cast(self._wnd_proc, ctypes.c_void_p).value
        wc.hInstance = hInstance
        wc.hbrBackground = ctypes.windll.gdi32.CreateSolidBrush(0x00000000)  # black
        wc.lpszClassName = class_name
        wc.hCursor = user32.LoadCursorW(None, IDC_ARROW)

        if not user32.RegisterClassExW(ctypes.byref(wc)):
            err = kernel32.GetLastError()
            # 1410 = class already registered - OK to continue
            if err != 1410:
                logger.error("RegisterClassExW failed: %d", err)
                self._ready.set()
                return

        ex_style = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_TOPMOST
        hwnd = user32.CreateWindowExW(
            ex_style,
            class_name,
            "BrilloOverlay",
            WS_POPUP,
            m["left"], m["top"],
            m["width"], m["height"],
            None, None, hInstance, None
        )

        if not hwnd:
            logger.error("CreateWindowExW failed: %d", kernel32.GetLastError())
            self._ready.set()
            return

        self._hwnd = hwnd
        self._thread_id = kernel32.GetCurrentThreadId()

        # Start timer to enforce topmost every 500ms
        user32.SetTimer(hwnd, TIMER_ID, 500, None)

        # Set TOPMOST explicitly just in case
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

        # Start with opacity 0 (OFF state) but KEEP it 'visible' to OS
        # This prevents Windows from blocking ShowWindow from a background process
        ctypes.windll.user32.SetLayeredWindowAttributes(hwnd, 0, 0, LWA_ALPHA)
        user32.ShowWindow(hwnd, SW_SHOWNA)

        logger.debug("Overlay Win32 window created (hwnd=%d)", hwnd)
        self._ready.set()

        # Message loop
        # NOTE: PostThreadMessage delivers to the *thread* queue with hwnd=NULL.
        # GetMessageW(NULL) picks these up but they do NOT go through wnd_proc.
        # We must check msg.hwnd == 0 to handle thread messages here directly.
        msg_struct = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg_struct), None, 0, 0) != 0:
            # Thread message (hwnd is None/0) — handle WM_APP_SET_STATE directly
            if not msg_struct.hWnd and msg_struct.message == WM_APP_SET_STATE:
                state = int(msg_struct.wParam)
                self._apply_state(state)
            else:
                user32.TranslateMessage(ctypes.byref(msg_struct))
                user32.DispatchMessageW(ctypes.byref(msg_struct))

        logger.debug("Overlay message loop ended.")

    def _apply_state(self, state: int) -> None:
        """Apply visual state - MUST be called from the window thread."""
        hwnd = self._hwnd
        if not hwnd:
            return
        user32 = ctypes.windll.user32

        if state == STATE_OFF:
            ctypes.windll.user32.SetLayeredWindowAttributes(hwnd, 0, 0, LWA_ALPHA)
            logger.info("Overlay: OFF")

        elif state == STATE_DIM:
            ctypes.windll.user32.SetLayeredWindowAttributes(
                hwnd, 0, DIM_ALPHA, LWA_ALPHA
            )
            user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
            logger.info("Overlay: DIM (%.0f%%)", (DIM_ALPHA / 255) * 100)

        elif state == STATE_BLACKOUT:
            ctypes.windll.user32.SetLayeredWindowAttributes(
                hwnd, 0, BLACKOUT_ALPHA, LWA_ALPHA
            )
            user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
            logger.info("Overlay: BLACKOUT (100%%)")

    # ------------------------------------------------------------------
    # Thread-safe public API
    # ------------------------------------------------------------------

    def _post(self, state: int) -> None:
        """Post a state-change message to the window thread (thread-safe)."""
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(
                self._thread_id, WM_APP_SET_STATE, state, 0
            )
        else:
            logger.warning("Overlay thread not ready yet.")

    def set_state(self, state: int) -> None:
        state = max(0, min(2, state))
        with self._state_lock:
            self._current_state = state
        self._post(state)

    def cycle(self) -> int:
        with self._state_lock:
            next_state = (self._current_state + 1) % 3
            self._current_state = next_state
        self._post(next_state)
        return next_state

    def get_state(self) -> int:
        with self._state_lock:
            return self._current_state

    def get_state_name(self) -> str:
        names = {STATE_OFF: "off", STATE_DIM: "dim", STATE_BLACKOUT: "blackout"}
        return names.get(self.get_state(), "unknown")

    def shutdown(self) -> None:
        self.set_state(STATE_OFF)
        if self._hwnd:
            ctypes.windll.user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
