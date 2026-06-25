from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from autoreview.agent import ReviewAgent
from autoreview.agent.state import JsonStateStore
from autoreview.feishu.config import FeishuConfig


def main() -> int:
    _force_utf8_stdio()
    parser = argparse.ArgumentParser(description="OpenClaw bridge for AutoReview core tools.")
    parser.add_argument("--config", default="config/oppo_submission.json")
    parser.add_argument("--session-id", default="")
    parser.add_argument("--sender-id", default="")
    parser.add_argument("--text", default="")
    parser.add_argument("--stdin", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    text = sys.stdin.read() if args.stdin else args.text
    text = str(text or "").strip()
    if not text:
        return _emit({"ok": False, "error": "text is required"}, as_json=args.json)

    config_path = Path(args.config).resolve()
    session_id = str(args.session_id or "openclaw-autoreview").strip()
    sender_id = str(args.sender_id or "openclaw").strip()

    try:
        config = FeishuConfig.from_file(config_path)
        agent = ReviewAgent(
            JsonStateStore(config.state_path),
            oppo_config_path=config.config_path,
            market_data_config_path=config.market_data_config_path,
            packaging_config_path=config.packaging_config_path,
            llm_client=None,
        )
        response = agent.handle_message(session_id=session_id, text=text, sender_id=sender_id)
    except Exception as exc:
        return _emit({"ok": False, "error": str(exc)}, as_json=args.json)

    payload: dict[str, Any] = {
        "ok": True,
        "response": response.text,
        "data": response.data,
    }
    return _emit(payload, as_json=args.json)


def _emit(payload: dict[str, Any], *, as_json: bool) -> int:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False))
    elif payload.get("ok"):
        print(str(payload.get("response") or ""))
    else:
        print(str(payload.get("error") or "unknown error"), file=sys.stderr)
    return 0 if payload.get("ok") else 1


def _force_utf8_stdio() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
