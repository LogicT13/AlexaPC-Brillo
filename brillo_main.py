"""brillo_main.py :: Entry point for the Samsung F24T350FH dimmer / blackout controller.

Usage
-----
    python brillo_main.py [--debug]

What it starts
--------------
1. OverlayEngine     – tkinter overlay in a daemon thread
2. ControlServer     – HTTP server on localhost:52800 in a daemon thread
3. HotkeyListener    – Win32 RegisterHotKey in a daemon thread
4. (optional) Fauxmo – emulates Belkin WeMo devices for Alexa voice control

Fauxmo is launched as a subprocess so its asyncio loop doesn't conflict with
tkinter's main loop.  It is started automatically and killed on exit.

Safety rules
------------
* Script does NOT configure autostart (no Startup folder, no Task Scheduler).
* Press Ctrl+C in this terminal to shut everything down gracefully.
* Press Ctrl+Alt+ESC (global) or ESC inside the overlay as a panic kill.
* Rebooting the ThinkPad always restores normal desktop.
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import os
import signal
import subprocess
import sys
import time

# Windows Console Constants
STD_INPUT_HANDLE = -10
ENABLE_QUICK_EDIT_MODE = 0x0040
ENABLE_EXTENDED_FLAGS = 0x0080


def _disable_quickedit() -> None:
    """Disable QuickEdit mode in the Windows console to prevent the process
    from freezing when the user accidentally clicks inside the terminal.
    """
    if os.name != "nt":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        h_stdin = kernel32.GetStdHandle(STD_INPUT_HANDLE)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(h_stdin, ctypes.byref(mode)):
            # Remove ENABLE_QUICK_EDIT_MODE flag
            new_mode = (mode.value & ~ENABLE_QUICK_EDIT_MODE) | ENABLE_EXTENDED_FLAGS
            kernel32.SetConsoleMode(h_stdin, new_mode)
    except Exception as e:
        logging.getLogger(__name__).debug("Could not disable QuickEdit: %s", e)


from control_server import ControlServer
from hotkey_listener import HotkeyListener
from overlay_engine import OverlayEngine

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def _setup_logging(debug: bool = False) -> None:
    level = logging.DEBUG if debug else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(level=level, format=fmt, stream=sys.stdout)


# ---------------------------------------------------------------------------
# Fauxmo subprocess helpers
# ---------------------------------------------------------------------------

_FAUXMO_CONFIG = os.path.join(os.path.dirname(__file__), "fauxmo.json")
_fauxmo_proc: subprocess.Popen | None = None
_MAX_FAUXMO_RESTARTS = 3  # stop restarting after this many consecutive failures


def _start_fauxmo(debug: bool = False) -> subprocess.Popen | None:
    """Launch Fauxmo as a subprocess pointing at our config.

    stdout/stderr are NOT captured - they go directly to this terminal.
    This avoids readline() blocking the monitor loop.
    """
    cmd = ["fauxmo", "--config", _FAUXMO_CONFIG]
    if debug:
        cmd.append("-v")

    # Prevent Fauxmo from popping up a new visible console window 
    # when the main script is run in the background (via VBS/pythonw)
    cflags = 0
    if os.name == "nt":
        cflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

    try:
        proc = subprocess.Popen(
            cmd,
            # No PIPE - let Fauxmo print directly to the terminal
            stdout=None,
            stderr=None,
            cwd=os.path.dirname(__file__),
            creationflags=cflags,
        )
        logging.getLogger(__name__).info(
            "Fauxmo started (pid=%d) with config: %s", proc.pid, _FAUXMO_CONFIG
        )
        return proc
    except FileNotFoundError:
        logging.getLogger(__name__).error(
            "Fauxmo executable not found. Install with: pip install fauxmo"
        )
        return None


def _stop_fauxmo(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
        logging.getLogger(__name__).info("Fauxmo stopped.")
    except Exception as exc:
        logging.getLogger(__name__).warning("Could not stop Fauxmo: %s", exc)
        proc.kill()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Samsung F24T350FH software dimmer / blackout controller."
    )
    parser.add_argument(
        "--debug", action="store_true", help="Enable verbose debug logging."
    )
    parser.add_argument(
        "--no-fauxmo",
        action="store_true",
        help="Skip starting Fauxmo (useful if you run it separately).",
    )
    args = parser.parse_args()

    _disable_quickedit()
    _setup_logging(debug=args.debug)
    log = logging.getLogger(__name__)

    log.info("=" * 60)
    log.info("  Brillo Controller – Samsung F24T350FH")
    log.info("  Hotkey: Ctrl+Alt+B  (cycle Off → Dim → Blackout)")
    log.info("  Panic:  Ctrl+Alt+ESC or ESC in overlay  (→ Off)")
    log.info("  Ctrl+C in this terminal to quit.")
    log.info("=" * 60)

    # 1. Overlay engine (starts tkinter daemon thread)
    engine = OverlayEngine()

    # 2. HTTP control server (localhost:52800, daemon thread)
    server = ControlServer(engine)
    server.start()

    # 3. Global hotkey listener (Win32 daemon thread)
    hotkeys = HotkeyListener(engine)
    hotkeys.start()

    # 4. Fauxmo subprocess
    fauxmo_proc: subprocess.Popen | None = None
    if not args.no_fauxmo:
        # Small delay so the control server is listening before Fauxmo connects
        time.sleep(0.5)
        fauxmo_proc = _start_fauxmo(debug=args.debug)

    # Graceful shutdown on SIGINT (Ctrl+C) or SIGTERM
    def _shutdown(signum: int, frame: object) -> None:
        log.info("Shutdown signal received – cleaning up…")
        _stop_fauxmo(fauxmo_proc)
        hotkeys.stop()
        engine.shutdown()
        server.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    log.info("Controller running. Waiting for commands...")

    # Keep main thread alive (daemon threads will die with it otherwise)
    fauxmo_restarts = 0
    fauxmo_restart_delay = 5.0  # seconds between restart attempts
    try:
        while True:
            time.sleep(1)
            # Monitor Fauxmo health (no readline - output goes direct to terminal)
            if fauxmo_proc is not None:
                rc = fauxmo_proc.poll()
                if rc is not None:
                    # Fauxmo exited
                    fauxmo_restarts += 1
                    if fauxmo_restarts > _MAX_FAUXMO_RESTARTS:
                        log.error(
                            "Fauxmo exited with code %d and has crashed %d times. "
                            "Giving up - run 'fauxmo --config fauxmo.json -v' manually "
                            "to see the error.",
                            rc, fauxmo_restarts,
                        )
                        fauxmo_proc = None  # stop monitoring
                    else:
                        log.warning(
                            "Fauxmo exited with code %d (attempt %d/%d). "
                            "Restarting in %.0fs...",
                            rc, fauxmo_restarts, _MAX_FAUXMO_RESTARTS,
                            fauxmo_restart_delay,
                        )
                        time.sleep(fauxmo_restart_delay)
                        fauxmo_restart_delay = min(fauxmo_restart_delay * 2, 60)
                        fauxmo_proc = _start_fauxmo(debug=args.debug)
    except KeyboardInterrupt:
        _shutdown(signal.SIGINT, None)


if __name__ == "__main__":
    main()
