"""Batch submission helpers for OPPO workflows."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import json
from pathlib import Path
from typing import Any

from .config import OppoSubmissionConfig
from .errors import OppoConfigError


JsonDict = dict[str, Any]


@dataclass(frozen=True)
class BatchJob:
    name: str
    config_path: Path
    overrides: JsonDict
    path_base: Path


def apply_submission_overrides(
    config: OppoSubmissionConfig,
    overrides: JsonDict,
    *,
    path_base: Path,
) -> OppoSubmissionConfig:
    if not overrides:
        return config

    submission = deepcopy(config.submission)
    nested_submission = overrides.get("submission")
    if nested_submission is not None:
        if not isinstance(nested_submission, dict):
            raise OppoConfigError("batch submission override 'submission' must be an object")
        _deep_update(submission, nested_submission)

    if overrides.get("apk") or overrides.get("apk_path"):
        apk_path = _resolve_override_path(str(overrides.get("apk") or overrides.get("apk_path")), path_base)
        apk_ref = submission.get("apk_url")
        if isinstance(apk_ref, list) and apk_ref:
            first = dict(apk_ref[0]) if isinstance(apk_ref[0], dict) else {}
            first["path"] = str(apk_path)
            if "cpu_code" not in first:
                first["cpu_code"] = int(overrides.get("cpu_code", 0))
            apk_ref[0] = first
        elif isinstance(apk_ref, dict):
            updated = dict(apk_ref)
            updated["path"] = str(apk_path)
            if "cpu_code" not in updated:
                updated["cpu_code"] = int(overrides.get("cpu_code", 0))
            submission["apk_url"] = updated
        else:
            submission["apk_url"] = {"path": str(apk_path), "cpu_code": int(overrides.get("cpu_code", 0))}

    for source, target in (
        ("pkg_name", "pkg_name"),
        ("version_code", "version_code"),
        ("version_name", "version_name"),
        ("app_name", "app_name"),
    ):
        if overrides.get(source) is not None:
            submission[target] = str(overrides[source])

    return replace(config, submission=submission)


def load_batch_jobs(batch_file: str | Path, default_config_path: str | Path) -> list[BatchJob]:
    batch_path = Path(batch_file).resolve()
    try:
        payload = json.loads(batch_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OppoConfigError(f"Batch file not found: {batch_path}") from exc
    except json.JSONDecodeError as exc:
        raise OppoConfigError(f"Batch file is not valid JSON: {exc}") from exc

    defaults: JsonDict = {}
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        defaults = payload.get("defaults") or {}
        items = payload.get("items") or payload.get("submissions") or []
    else:
        raise OppoConfigError("Batch file must be a JSON array or object")

    if not isinstance(items, list) or not items:
        raise OppoConfigError("Batch file must provide non-empty items")

    jobs: list[BatchJob] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise OppoConfigError(f"Batch item {index} must be an object")
        merged = dict(defaults)
        merged.update(item)
        config_path = _resolve_override_path(str(merged.get("config") or default_config_path), batch_path.parent)
        name = str(merged.get("name") or merged.get("app_name") or config_path.stem)
        overrides = {
            key: value
            for key, value in merged.items()
            if key not in {"name", "config"}
        }
        jobs.append(
            BatchJob(
                name=name,
                config_path=config_path,
                overrides=overrides,
                path_base=batch_path.parent,
            )
        )
    return jobs


def _deep_update(target: JsonDict, patch: JsonDict) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = deepcopy(value)


def _resolve_override_path(value: str, base_dir: Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()
