"""Alert de-duplication state, persisted as JSON so restarts don't re-send everything.

Rule: an active alert is sent if it was never sent, or if `cooldown` has passed since it was
last sent. When a condition stops being active its record is dropped, so the next time the
price crosses the threshold the alert fires again immediately.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Dict, Iterable, List

from .rules import Alert


class AlertState:
    def __init__(self, path: Path):
        self.path = path
        self._sent: Dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            # Corrupt or unreadable state is not fatal: start fresh (worst case: one repeat alert).
            return
        if isinstance(data, dict):
            self._sent = {k: float(v) for k, v in data.items()
                          if isinstance(k, str) and isinstance(v, (int, float)) and not isinstance(v, bool)}

    def due(self, alerts: Iterable[Alert], now_ts: float, cooldown_seconds: float) -> List[Alert]:
        return [a for a in alerts if now_ts - self._sent.get(a.key, float("-inf")) >= cooldown_seconds]

    def mark_sent(self, key: str, now_ts: float) -> None:
        self._sent[key] = now_ts

    def keep_only(self, active_keys: Iterable[str]) -> None:
        active = set(active_keys)
        self._sent = {k: v for k, v in self._sent.items() if k in active}

    def save(self) -> None:
        """Atomic write: temp file in the same directory, then replace."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".state-", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._sent, fh, ensure_ascii=False, indent=0, sort_keys=True)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
