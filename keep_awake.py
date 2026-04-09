#!/usr/bin/env python3
import atexit
import ctypes
import signal
import sys
import threading
import time


ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

STOP_EVENT = threading.Event()


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            try:
                reconfigure(errors="replace")
            except Exception:
                pass


def log(message: str) -> None:
    print(f"[keep-awake] {message}", flush=True)


def set_execution_state(flags: int) -> None:
    result = ctypes.windll.kernel32.SetThreadExecutionState(flags)
    if result == 0:
        raise ctypes.WinError()


def keep_system_awake() -> None:
    set_execution_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)


def clear_execution_state() -> None:
    try:
        set_execution_state(ES_CONTINUOUS)
    except Exception:
        pass


def handle_stop(_signum=None, _frame=None) -> None:
    STOP_EVENT.set()


def main() -> int:
    configure_stdio()

    if sys.platform != "win32":
        print("keep_awake.py only supports Windows.", file=sys.stderr)
        return 1

    atexit.register(clear_execution_state)

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, handle_stop)

    keep_system_awake()
    log("started; Windows idle sleep should stay blocked while this process is alive")

    try:
        while not STOP_EVENT.wait(30):
            keep_system_awake()
    except KeyboardInterrupt:
        STOP_EVENT.set()
    finally:
        clear_execution_state()
        log("stopped")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
