"""Configuration loading for OPPO app-store submissions."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

from .errors import OppoConfigError


def _as_path(value: str | Path, base_dir: Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = base_dir / path
    return path


def resolve_path_like(value: Any, base_dir: Path) -> Any:
    """Resolve explicit local file references without changing business strings."""

    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if "path" in value and isinstance(value["path"], str):
            resolved = dict(value)
            resolved["path"] = _as_path(value["path"], base_dir)
            return resolved
        return {key: resolve_path_like(item, base_dir) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_path_like(item, base_dir) for item in value]
    return value


def merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_shared_submission(raw: dict[str, Any], config_path: Path) -> dict[str, Any]:
    shared_path_value = raw.get("shared_submission_path") or raw.get("submission_config_path")
    if not shared_path_value:
        return {}
    shared_path = _as_path(str(shared_path_value), config_path.parent).resolve()
    try:
        shared_raw = json.loads(shared_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OppoConfigError(f"Shared submission config not found: {shared_path}") from exc
    except json.JSONDecodeError as exc:
        raise OppoConfigError(f"Shared submission config is not valid JSON: {exc}") from exc
    if not isinstance(shared_raw, dict):
        raise OppoConfigError("Shared submission config must be a JSON object")
    submission = shared_raw.get("submission", shared_raw)
    if not isinstance(submission, dict):
        raise OppoConfigError("Shared submission config must provide a submission object")
    return submission


@dataclass(frozen=True)
class OppoApiSettings:
    base_url: str = "https://oop-openapi-cn.heytapmobi.com"
    token_path: str = "/developer/v1/token"
    pre_upload_path: str = "/resource/v1/upload/get-upload-url"
    release_path: str = "/resource/v1/app/upd"
    update_material_path: str = "/resource/v1/app/updm"
    task_state_path: str = "/resource/v1/app/task-state"
    detail_path: str = "/resource/v1/app/info"
    timeout_seconds: int = 30
    submit_timeout_seconds: int = 15

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "OppoApiSettings":
        if not data:
            return cls()
        known = {field.name for field in cls.__dataclass_fields__.values()}
        return cls(**{key: value for key, value in data.items() if key in known})


@dataclass(frozen=True)
class PollingSettings:
    task_interval_seconds: int = 30
    task_timeout_seconds: int = 1800
    review_interval_seconds: int = 600
    review_timeout_seconds: int = 86400

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "PollingSettings":
        if not data:
            return cls()
        known = {field.name for field in cls.__dataclass_fields__.values()}
        return cls(**{key: value for key, value in data.items() if key in known})


@dataclass(frozen=True)
class OppoSubmissionConfig:
    client_id: str
    client_secret: str
    submission: dict[str, Any]
    api: OppoApiSettings = field(default_factory=OppoApiSettings)
    polling: PollingSettings = field(default_factory=PollingSettings)
    config_path: Path | None = None

    @property
    def base_dir(self) -> Path:
        if self.config_path:
            return self.config_path.parent
        return Path.cwd()

    @classmethod
    def from_file(cls, path: str | Path) -> "OppoSubmissionConfig":
        config_path = Path(path).resolve()
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise OppoConfigError(f"Config file not found: {config_path}") from exc
        except json.JSONDecodeError as exc:
            raise OppoConfigError(f"Config file is not valid JSON: {exc}") from exc

        credentials = raw.get("credentials") or {}
        client_id = credentials.get("client_id") or raw.get("client_id")
        client_secret = credentials.get("client_secret") or raw.get("client_secret")
        if not client_id or not client_secret:
            raise OppoConfigError(
                "Config must provide credentials.client_id and credentials.client_secret"
            )

        raw_submission = raw.get("submission") or {}
        if not isinstance(raw_submission, dict):
            raise OppoConfigError("Config must provide a submission object")
        shared_submission = load_shared_submission(raw, config_path)
        submission = merge_dicts(shared_submission, raw_submission)
        if not submission:
            raise OppoConfigError("Config must provide submission or shared_submission_path")

        return cls(
            client_id=str(client_id),
            client_secret=str(client_secret),
            submission=submission,
            api=OppoApiSettings.from_dict(raw.get("oppo")),
            polling=PollingSettings.from_dict(raw.get("polling")),
            config_path=config_path,
        )

    def resolved_submission(self) -> dict[str, Any]:
        return resolve_path_like(self.submission, self.base_dir)
