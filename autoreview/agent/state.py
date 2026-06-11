"""Lightweight JSON state store for review conversations."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import time
from typing import Any


JsonDict = dict[str, Any]


def default_state() -> JsonDict:
    return {
        "apps": {},
        "sessions": {},
        "updated_at": int(time.time()),
    }


@dataclass
class JsonStateStore:
    path: Path

    def load(self) -> JsonDict:
        if not self.path.exists():
            return default_state()
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return default_state()

    def save(self, state: JsonDict) -> None:
        state["updated_at"] = int(time.time())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def update_session(self, session_id: str, patch: JsonDict) -> JsonDict:
        state = self.load()
        sessions = state.setdefault("sessions", {})
        session = sessions.setdefault(session_id, {})
        session.update(patch)
        session["updated_at"] = int(time.time())
        self.save(state)
        return session

    def get_session(self, session_id: str) -> JsonDict:
        return self.load().setdefault("sessions", {}).get(session_id, {})

    def clear_session(self, session_id: str) -> None:
        state = self.load()
        sessions = state.setdefault("sessions", {})
        sessions.pop(session_id, None)
        self.save(state)

    def clear_all(self) -> None:
        self.save(default_state())
