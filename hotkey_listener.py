"""hotkey_listener.py :: Global hotkey listener using ctypes / Win32 RegisterHotKey.

Registers a system-wide hotkey (default: Ctrl+Alt+B) that cycles the
overlay through its three states without needing a window to be focused.

This runs its own message loop in a daemon thread.

Alternative hotkeys can be set via the config:
    HOTKEY_MODIFIERS  – bitmask (see MOD_* constants below)
    HOTKEY_VK         – virtual key code (0x42 = 'B')
"""

from __future__ import annotations

import ctypes
import logging
import threading
from typing import Optional

from overlay_engine import OverlayEngine

logger = logging.getLogger(__name__)

# Win32 modifier bitmasks
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000  # prevents repeated triggers on hold

# Default: Ctrl + Alt + B
DEFAULT_MODIFIERS = MOD_CONTROL | MOD_ALT | MOD_NOREPEAT
DEFAULT_VK = 0x42  # Virtual key 'B'

# Emergency: Ctrl + Alt + ESC  (VK_ESCAPE = 0x1B)
PANIC_MODIFIERS = MOD_CONTROL | MOD_ALT | MOD_NOREPEAT
PANIC_VK = 0x1B  # VK_ESCAPE

HOTKEY_ID_CYCLE = 1
HOTKEY_ID_PANIC = 2


class HotkeyListener:
    """Registers and processes global hotkeys in a dedicated thread."""

    def __init__(
        self,
        engine: OverlayEngine,
        cycle_modifiers: int = DEFAULT_MODIFIERS,
        cycle_vk: int = DEFAULT_VK,
    ) -> None:
        self._engine = engine
        self._cycle_modifiers = cycle_modifiers
        self._cycle_vk = cycle_vk
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._message_loop, daemon=True, name="HotkeyListener"
        )
        self._thread.start()
        logger.info(
            "Hotkey listener started. "
            "Cycle: Ctrl+Alt+B | Panic (off): Ctrl+Alt+ESC"
        )

    def _message_loop(self) -> None:
        user32 = ctypes.windll.user32

        # Register cycle hotkey
        if not user32.RegisterHotKey(None, HOTKEY_ID_CYCLE, self._cycle_modifiers, self._cycle_vk):
            logger.error(
                "Failed to register cycle hotkey (Ctrl+Alt+B). "
                "Another app may be using it."
            )
        else:
            logger.debug("Cycle hotkey Ctrl+Alt+B registered (id=%d).", HOTKEY_ID_CYCLE)

        # Register panic / escape hotkey
        if not user32.RegisterHotKey(None, HOTKEY_ID_PANIC, PANIC_MODIFIERS, PANIC_VK):
            logger.error(
                "Failed to register panic hotkey (Ctrl+Alt+ESC)."
            )
        else:
            logger.debug("Panic hotkey Ctrl+Alt+ESC registered (id=%d).", HOTKEY_ID_PANIC)

        msg = ctypes.wintypes.MSG()
        try:
            while not self._stop_event.is_set():
                # PeekMessage with timeout-like behaviour
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret == 0 or ret == -1:
                    break

                if msg.message == 0x0312:  # WM_HOTKEY
                    hotkey_id = msg.wParam
                    if hotkey_id == HOTKEY_ID_CYCLE:
                        new_state = self._engine.cycle()
                        logger.info(
                            "Hotkey Ctrl+Alt+B triggered → state: %s",
                            self._engine.get_state_name(),
                        )
                    elif hotkey_id == HOTKEY_ID_PANIC:
                        self._engine.set_state(0)  # STATE_OFF
                        logger.warning(
                            "PANIC hotkey Ctrl+Alt+ESC → overlay killed."
                        )

                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            user32.UnregisterHotKey(None, HOTKEY_ID_CYCLE)
            user32.UnregisterHotKey(None, HOTKEY_ID_PANIC)
            logger.debug("Hotkeys unregistered.")

    def stop(self) -> None:
        self._stop_event.set()
        # Post a quit message to unblock GetMessageW
        ctypes.windll.user32.PostThreadMessageW(
            self._thread.ident if self._thread else 0,
            0x0012,  # WM_QUIT
            0,
            0,
        )
