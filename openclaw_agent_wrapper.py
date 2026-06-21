from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def main() -> int:
    _force_utf8_stdio()
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", default="main")
    parser.add_argument("--profile", default="autoreview")
    parser.add_argument("--timeout", default="120")
    args = parser.parse_args()

    prompt = sys.stdin.read()
    if not prompt:
        print("empty prompt", file=sys.stderr)
        return 2
    prompt = re.sub(r"\s+", " ", prompt).strip()
    if len(prompt) > 12000:
        prompt = prompt[:12000] + " [内容过长，已截断]"

    node = _resolve_node()
    openclaw_mjs = _resolve_openclaw_mjs()
    session_key = f"agent:{args.agent}:autoreview-{os.urandom(8).hex()}"
    command = [
        node,
        str(openclaw_mjs),
        "--profile",
        args.profile,
        "agent",
        "--agent",
        args.agent,
        "--session-key",
        session_key,
        "--message",
        prompt,
        "--json",
        "--timeout",
        str(args.timeout),
    ]
    completed = subprocess.run(
        command,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        print(detail, file=sys.stderr)
        return completed.returncode
    output = (completed.stdout or "").strip()
    print(_extract_assistant_text(output), end="")
    return 0


def _resolve_node() -> str:
    appdata = Path(os.environ.get("APPDATA", ""))
    candidates = [
        appdata / "npm" / "node.exe",
        Path("C:/Program Files/nodejs/node.exe"),
        Path("G:/Program Files/nodejs/node.exe"),
        Path("node.exe"),
    ]
    for candidate in candidates:
        if candidate.name == "node.exe" and candidate.exists():
            return str(candidate)
    return "node.exe"


def _resolve_openclaw_mjs() -> Path:
    appdata = Path(os.environ.get("APPDATA", ""))
    candidate = appdata / "npm" / "node_modules" / "openclaw" / "openclaw.mjs"
    if candidate.exists():
        return candidate
    return Path("openclaw.mjs")


def _extract_assistant_text(output: str) -> str:
    if not output:
        return ""
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        return output
    if not isinstance(parsed, dict):
        return output

    result = parsed.get("result")
    if isinstance(result, dict):
        for key in ("finalAssistantVisibleText", "finalAssistantRawText"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        payloads = result.get("payloads")
        if isinstance(payloads, list) and payloads:
            first = payloads[0]
            if isinstance(first, dict):
                value = first.get("text")
                if isinstance(value, str) and value.strip():
                    return value.strip()
    for key in ("reply", "content", "text", "message", "output"):
        value = parsed.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return output


if __name__ == "__main__":
    raise SystemExit(main())


def _force_utf8_stdio() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
