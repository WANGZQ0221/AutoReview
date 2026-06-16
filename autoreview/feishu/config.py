"""Configuration for Feishu webhook integration."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class FeishuConfig:
    app_id: str
    app_secret: str
    config_path: Path | None = None
    market_data_config_path: Path | None = None
    verification_token: str = ""
    encrypt_key: str = ""
    state_path: Path = Path("data/review_agent_state.json")
    image2_url: str = ""
    ocr_url: str = ""
    ocr_api_key: str = ""
    image_analysis_timeout_seconds: int = 120
    llm: dict[str, Any] | None = None
    agent_app_api_key: str = ""

    @classmethod
    def from_file(cls, path: str | Path) -> "FeishuConfig":
        config_path = Path(path).resolve()
        raw: dict[str, Any] = json.loads(config_path.read_text(encoding="utf-8"))
        feishu = raw.get("feishu") or raw
        state_path = feishu.get("state_path") or "data/review_agent_state.json"
        resolved_state_path = Path(state_path)
        if not resolved_state_path.is_absolute():
            resolved_state_path = config_path.parent / resolved_state_path
        image_analysis = feishu.get("image_analysis") or {}
        agent_app = raw.get("agent_app") or feishu.get("agent_app") or {}
        llm = _load_llm_config(raw, feishu, config_path)
        market_data_config_path = _resolve_optional_path(
            raw.get("market_data_config_path") or feishu.get("market_data_config_path") or "market_data.json",
            config_path.parent,
        )
        return cls(
            app_id=str(feishu.get("app_id", "")),
            app_secret=str(feishu.get("app_secret", "")),
            config_path=config_path,
            market_data_config_path=market_data_config_path,
            verification_token=str(feishu.get("verification_token", "")),
            encrypt_key=str(feishu.get("encrypt_key", "")),
            state_path=resolved_state_path,
            image2_url=str(
                image_analysis.get("image2_url")
                or feishu.get("image2_url")
                or ""
            ),
            ocr_url=str(
                image_analysis.get("ocr_url")
                or feishu.get("ocr_url")
                or ""
            ),
            ocr_api_key=str(
                image_analysis.get("ocr_api_key")
                or feishu.get("ocr_api_key")
                or ""
            ),
            image_analysis_timeout_seconds=int(
                image_analysis.get("timeout_seconds")
                or feishu.get("image_analysis_timeout_seconds")
                or 120
            ),
            llm=llm,
            agent_app_api_key=str(agent_app.get("api_key") or feishu.get("agent_app_api_key") or ""),
        )


def _resolve_optional_path(value: Any, base_dir: Path) -> Path | None:
    if value in (None, ""):
        return None
    path = Path(str(value))
    if not path.is_absolute():
        path = base_dir / path
    return path


def _load_llm_config(raw: dict[str, Any], feishu: dict[str, Any], config_path: Path) -> dict[str, Any]:
    llm_path_value = raw.get("llm_config_path") or feishu.get("llm_config_path")
    inline = raw.get("llm") or feishu.get("llm") or {}
    if not llm_path_value:
        return dict(inline)
    llm_path = Path(str(llm_path_value))
    if not llm_path.is_absolute():
        llm_path = config_path.parent / llm_path
    try:
        loaded = json.loads(llm_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return dict(inline)
    if not isinstance(loaded, dict):
        return dict(inline)
    merged = dict(loaded)
    merged.update(inline)
    return merged
