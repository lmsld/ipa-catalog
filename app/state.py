"""Atomic YAML-backed catalog state."""

from __future__ import annotations

import os
import tempfile
import threading
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import yaml


class CatalogStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def initialize(self) -> None:
        with self._lock:
            if self.path.exists():
                self.read()
                return

            self.write({"apps": []})

    def read(self) -> dict[str, Any]:
        with self._lock:
            if not self.path.exists():
                return {"apps": []}
            try:
                with self.path.open("r", encoding="utf-8") as file:
                    state = yaml.safe_load(file)
            except (OSError, yaml.YAMLError) as exc:
                raise RuntimeError(f"Could not read catalog state at {self.path}: {exc}") from exc
            if state is None:
                return {"apps": []}
            if not isinstance(state, dict) or not isinstance(state.get("apps", []), list):
                raise RuntimeError(f"Catalog state at {self.path} must contain an apps list.")
            state.setdefault("apps", [])
            return state

    def write(self, state: dict[str, Any]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path: str | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=self.path.parent,
                    prefix=f".{self.path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as file:
                    temporary_path = file.name
                    yaml.safe_dump(
                        state,
                        file,
                        allow_unicode=True,
                        default_flow_style=False,
                        sort_keys=False,
                    )
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary_path, self.path)
            except (OSError, yaml.YAMLError) as exc:
                if temporary_path and os.path.exists(temporary_path):
                    os.unlink(temporary_path)
                raise RuntimeError(f"Could not write catalog state at {self.path}: {exc}") from exc

    def edit(self) -> AbstractContextManager[dict[str, Any]]:
        return _CatalogEdit(self)


class _CatalogEdit:
    def __init__(self, store: CatalogStore) -> None:
        self.store = store
        self.state: dict[str, Any] | None = None

    def __enter__(self) -> dict[str, Any]:
        self.store._lock.acquire()
        try:
            self.state = self.store.read()
            return self.state
        except Exception:
            self.store._lock.release()
            raise

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            if exc_type is None and self.state is not None:
                self.store.write(self.state)
        finally:
            self.store._lock.release()
        return False
