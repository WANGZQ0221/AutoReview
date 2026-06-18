"""Lightweight JSON state store for review conversations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import re
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

    @property
    def sessions_dir(self) -> Path:
        return self.path.parent / "sessions"

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
        patch = dict(patch)
        patch.pop("conversation_history", None)
        session.update(patch)
        session["updated_at"] = int(time.time())
        self.save(state)
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> JsonDict:
        session = dict(self.load().setdefault("sessions", {}).get(session_id, {}))
        turns = self.get_conversation_history(session_id, limit=20)
        if turns:
            session["conversation_history"] = turns
        return session

    def append_conversation_turn(self, session_id: str, turn: JsonDict, *, keep_recent: int = 20) -> list[JsonDict]:
        entry = dict(turn)
        entry.setdefault("ts", int(time.time()))
        self._session_dir(session_id).mkdir(parents=True, exist_ok=True)
        with self._turns_path(session_id).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.update_session(session_id, {"last_turn_at": entry["ts"]})
        return self.get_conversation_history(session_id, limit=keep_recent)

    def get_conversation_history(self, session_id: str, *, limit: int = 20) -> list[JsonDict]:
        path = self._turns_path(session_id)
        turns: list[JsonDict] = []
        if path.exists():
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                lines = []
            for line in lines[-max(limit * 2, limit) :]:
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    turns.append(item)
            return turns[-limit:]
        legacy = self.load().setdefault("sessions", {}).get(session_id, {}).get("conversation_history") or []
        if isinstance(legacy, list):
            return [item for item in legacy[-limit:] if isinstance(item, dict)]
        return []

    def append_trace_event(self, session_id: str, event: JsonDict) -> None:
        entry = dict(event)
        entry.setdefault("ts", int(time.time()))
        self._session_dir(session_id).mkdir(parents=True, exist_ok=True)
        with self._trace_path(session_id).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.update_session(session_id, {"last_trace_at": entry["ts"]})

    def clear_session(self, session_id: str) -> None:
        state = self.load()
        sessions = state.setdefault("sessions", {})
        sessions.pop(session_id, None)
        self.save(state)
        session_dir = self._session_dir(session_id)
        if session_dir.exists():
            for child in session_dir.iterdir():
                if child.is_file():
                    child.unlink()
            try:
                session_dir.rmdir()
            except OSError:
                pass

    def clear_all(self) -> None:
        self.save(default_state())
        if self.sessions_dir.exists():
            for session_dir in self.sessions_dir.iterdir():
                if not session_dir.is_dir():
                    continue
                for child in session_dir.iterdir():
                    if child.is_file():
                        child.unlink()
                try:
                    session_dir.rmdir()
                except OSError:
                    pass

    def _session_dir(self, session_id: str) -> Path:
        return self.sessions_dir / _safe_session_id(session_id)

    def _turns_path(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "turns.jsonl"

    def _trace_path(self, session_id: str) -> Path:
        return self._session_dir(session_id) / f"trace-{_date_stamp()}.jsonl"


def _safe_session_id(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "default")).strip("._")
    return safe or "default"


def _date_stamp(ts: int | None = None) -> str:
    return datetime.fromtimestamp(ts or int(time.time())).strftime("%Y-%m-%d")
