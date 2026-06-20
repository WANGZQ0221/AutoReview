"""HTTP service used by Feishu intelligent-agent applications.

The Feishu intelligent-agent app should stay a thin product entry. This server
keeps the AutoReview workflow logic in our own codebase.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from autoreview.agent import ReviewAgent
from autoreview.agent.llm import build_llm_client
from autoreview.agent.state import JsonStateStore
from autoreview.feishu.config import FeishuConfig


JsonDict = dict[str, Any]


class AgentApp:
    def __init__(self, config: FeishuConfig):
        self.config = config
        llm_client = build_llm_client(config.llm)
        self.agent = ReviewAgent(
            JsonStateStore(config.state_path),
            oppo_config_path=config.config_path,
            market_data_config_path=config.market_data_config_path,
            packaging_config_path=config.packaging_config_path,
            llm_client=llm_client if llm_client.enabled else None,
        )

    def handle_message(self, payload: JsonDict) -> JsonDict:
        text = str(payload.get("text") or payload.get("query") or "").strip()
        session_id = str(payload.get("session_id") or payload.get("user_id") or "agent-app")
        sender_id = str(payload.get("sender_id") or payload.get("user_id") or "")
        response = self.agent.handle_message(session_id=session_id, text=text, sender_id=sender_id)
        return {
            "ok": True,
            "response": response.text,
            "data": response.data,
        }

    def analyze_rejection(self, payload: JsonDict) -> JsonDict:
        text = str(payload.get("text") or payload.get("reason") or "").strip()
        session_id = str(payload.get("session_id") or payload.get("user_id") or "agent-app")
        sender_id = str(payload.get("sender_id") or payload.get("user_id") or "")
        if not text:
            return {"ok": False, "error": "text or reason is required"}
        response = self.agent.analyze_rejection_text(
            session_id=session_id,
            reason=text,
            sender_id=sender_id,
            source="agent_app",
        )
        return {
            "ok": True,
            "response": response.text,
            "data": response.data,
        }

    def tools(self) -> JsonDict:
        return {
            "ok": True,
            "tools": [
                {
                    "name": "autoreview_message",
                    "method": "POST",
                    "path": "/api/agent/message",
                    "description": "Send a natural-language AutoReview command and get a text response.",
                    "input": {
                        "session_id": "stable user/chat/session id",
                        "text": "user command, such as 查询审核状态 or 提交检查",
                    },
                },
                {
                    "name": "analyze_oppo_rejection",
                    "method": "POST",
                    "path": "/api/agent/analyze-rejection",
                    "description": "Analyze OPPO rejection text and return remediation advice.",
                    "input": {
                        "session_id": "stable user/chat/session id",
                        "text": "OPPO rejection reason text",
                    },
                },
            ],
        }


def make_handler(app: AgentApp):
    class AgentAppHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/health":
                self._write_json(200, {"ok": True, "service": "autoreview-agent-app"})
                return
            if path == "/api/agent/tools":
                self._write_json(200, app.tools())
                return
            self._write_json(404, {"ok": False, "error": "not found"})

        def do_POST(self):
            path = urlparse(self.path).path
            if not self._authorized():
                self._write_json(401, {"ok": False, "error": "unauthorized"})
                return
            try:
                payload = self._read_json()
                if path == "/api/agent/message":
                    response = app.handle_message(payload)
                    self._write_json(200, response)
                    return
                if path == "/api/agent/analyze-rejection":
                    response = app.analyze_rejection(payload)
                    self._write_json(200 if response.get("ok") else 400, response)
                    return
                self._write_json(404, {"ok": False, "error": "not found"})
            except Exception as exc:
                self._write_json(500, {"ok": False, "error": str(exc)})

        def log_message(self, format, *args):
            return

        def _authorized(self) -> bool:
            api_key = app.config.agent_app_api_key
            if not api_key:
                return True
            authorization = self.headers.get("Authorization", "")
            return authorization == f"Bearer {api_key}"

        def _read_json(self) -> JsonDict:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                return {}
            raw_body = self.rfile.read(length).decode("utf-8")
            payload = json.loads(raw_body)
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object")
            return payload

        def _write_json(self, status: int, payload: JsonDict) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    return AgentAppHandler


def run_agent_app_server(config_path: str | Path, host: str = "0.0.0.0", port: int = 8090) -> None:
    config = FeishuConfig.from_file(config_path)
    app = AgentApp(config)
    server = ThreadingHTTPServer((host, port), make_handler(app))
    print(f"AutoReview agent-app server listening on http://{host}:{port}")
    server.serve_forever()
