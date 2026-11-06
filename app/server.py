"""Uvicorn entry point with clean SIGINT and SIGTERM shutdown."""

from __future__ import annotations

import signal
import os
import threading
from contextlib import contextmanager
from typing import Generator

from uvicorn import Config, Server


class GracefulServer(Server):
    @contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        if threading.current_thread() is not threading.main_thread():
            yield
            return

        signals = (signal.SIGINT, signal.SIGTERM)
        original_handlers = {
            signum: signal.signal(signum, self.handle_exit) for signum in signals
        }
        try:
            yield
        finally:
            for signum, handler in original_handlers.items():
                signal.signal(signum, handler)


def main() -> None:
    config = Config(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.environ.get("APP_PORT", "8000")),
        timeout_graceful_shutdown=30,
    )
    GracefulServer(config).run()


if __name__ == "__main__":
    main()
