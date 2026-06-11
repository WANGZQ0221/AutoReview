"""Long-connection Feishu event receiver."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from typing import Any

from .config import FeishuConfig
from .server import FeishuWebhookApp


JsonDict = dict[str, Any]


def run_long_connection(config_path: str | Path, log_level: str = "INFO") -> None:
    try:
        import lark_oapi as lark
    except ImportError as exc:
        raise RuntimeError(
            "Missing Feishu SDK. Install it with: python -m pip install -U lark-oapi"
        ) from exc

    config = FeishuConfig.from_file(config_path)
    app = FeishuWebhookApp(config)
    executor = ThreadPoolExecutor(max_workers=4)

    def process_payload(payload: JsonDict) -> None:
        response = app.handle_payload(payload, verify_token=False)
        if response.get("code") not in (0, None):
            print(json.dumps(response, ensure_ascii=False))

    def on_p2_im_message_receive_v1(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
        payload = json.loads(lark.JSON.marshal(data))
        executor.submit(process_payload, payload)

    event_handler = (
        lark.EventDispatcherHandler.builder(config.encrypt_key, config.verification_token)
        .register_p2_im_message_receive_v1(on_p2_im_message_receive_v1)
        .build()
    )
    client = lark.ws.Client(
        config.app_id,
        config.app_secret,
        event_handler=event_handler,
        log_level=_to_lark_log_level(lark, log_level),
    )
    print("Feishu long-connection client starting. Keep this process running.")
    client.start()


def _to_lark_log_level(lark: Any, log_level: str) -> Any:
    normalized = log_level.upper()
    return getattr(lark.LogLevel, normalized, lark.LogLevel.INFO)
