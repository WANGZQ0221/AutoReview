"""Safe JSON config editing helpers for chat-driven updates."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import shutil
from typing import Any


JsonDict = dict[str, Any]

INT_CONFIG_PATHS = {
    "submission.apk_url.cpu_code",
    "feishu.image_analysis.timeout_seconds",
}

SIMPLE_SUBMISSION_FIELDS = {
    "pkg_name",
    "version_code",
    "version_name",
    "last_rejection_reason",
    "app_name",
    "second_category_id",
    "third_category_id",
    "summary",
    "detail_desc",
    "update_desc",
    "privacy_source_url",
    "icon_url",
    "online_type",
    "test_desc",
    "copyright_url",
    "business_username",
    "business_email",
    "business_mobile",
    "age_level",
    "adaptive_equipment",
}

FILE_REF_FIELDS = {
    "apk_url": {"path", "url", "md5", "cpu_code"},
    "icon_url": {"path"},
    "copyright_url": {"path"},
}

LIST_FILE_REF_FIELDS = {
    "pic_url",
    "special_url",
}

FEISHU_IMAGE_ANALYSIS_FIELDS = {
    "image2_url",
    "ocr_url",
    "timeout_seconds",
}

PACKAGING_FIELDS = {
    "project_dir",
    "script",
    "script_path",
    "batch_file",
    "packlist_scan_file",
    "packlist_snapshot",
    "node_command",
    "skip_start",
}


class ConfigEditError(ValueError):
    pass


def build_assignment_patch(payload: str) -> JsonDict:
    if "=" not in payload:
        raise ConfigEditError("请使用“设置提交配置：字段=值”的格式。")
    path, value = payload.split("=", 1)
    path = path.strip()
    if not path:
        raise ConfigEditError("配置字段不能为空。")
    value = _coerce_assignment_value(path, value.strip())
    patch = {path: value}
    _validate_flat_patch(patch)
    return patch


def build_json_patch(payload: str) -> JsonDict:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ConfigEditError(f"批量配置不是有效 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise ConfigEditError("批量配置必须是 JSON 对象。")
    patch = dict(_iter_flat_items(data))
    _validate_flat_patch(patch)
    return patch


def apply_config_patch_to_file(config_path: str | Path, patch: JsonDict) -> JsonDict:
    path = Path(config_path)
    raw = _read_config(path)
    _validate_flat_patch(patch)
    backup_path = _backup_config(path)
    for dotted_path, value in patch.items():
        _set_dotted_path(raw, dotted_path, value)
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "config_path": str(path),
        "backup_path": str(backup_path),
        "patch": patch,
    }


def apply_config_patch_to_targets(
    main_config_path: str | Path,
    patch: JsonDict,
    *,
    packaging_config_path: str | Path | None = None,
) -> JsonDict:
    _validate_flat_patch(patch)
    main_patch: JsonDict = {}
    packaging_patch: JsonDict = {}
    for dotted_path, value in patch.items():
        if dotted_path.startswith("packaging."):
            packaging_patch[dotted_path] = value
        else:
            main_patch[dotted_path] = value

    results: list[JsonDict] = []
    if main_patch:
        results.append(apply_config_patch_to_file(main_config_path, main_patch))
    if packaging_patch:
        target = Path(packaging_config_path) if packaging_config_path else Path(main_config_path).parent / "packaging.json"
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('{\n  "packaging": {}\n}\n', encoding="utf-8")
        results.append(apply_config_patch_to_file(target, packaging_patch))

    return {
        "results": results,
        "patch": patch,
    }


def format_config_summary(config_path: str | Path) -> str:
    path = Path(config_path)
    raw = _read_config(path)
    submission = raw.get("submission") or {}
    feishu = raw.get("feishu") or {}
    image = feishu.get("image_analysis") or {}
    llm = _load_summary_llm_config(raw, feishu, path)
    pic_paths = _list_pic_paths(submission.get("pic_url"))
    lines = [
        "当前提交配置",
        "",
        "应用信息：",
        f"- 应用：{submission.get('app_name') or '未配置'}",
        f"- 包名：{submission.get('pkg_name') or '未配置'}",
        f"- 版本：{submission.get('version_code') or '未配置'} / {submission.get('version_name') or '未配置'}",
        "",
        "材料配置：",
        f"- APK：{_ref_text(submission.get('apk_url'))}",
        f"- 图标：{_ref_text(submission.get('icon_url'))}",
        f"- 截图：{'、'.join(pic_paths) if pic_paths else '未配置'}",
        f"- 版权证明：{_ref_text(submission.get('copyright_url'))}",
        f"- 隐私政策：{submission.get('privacy_source_url') or '未配置'}",
        "",
        "能力配置：",
        f"- OCR：{image.get('ocr_url') or '未配置'}",
        f"- image2：{image.get('image2_url') or '未启用'}",
        f"- 大模型：{llm.get('model') or '未启用'}",
    ]
    secret_lines = []
    if raw.get("credentials"):
        secret_lines.append("- OPPO 密钥：已配置（不在飞书展示）")
    if feishu.get("app_secret") or image.get("ocr_api_key"):
        secret_lines.append("- 飞书/OCR 密钥：已配置（不在飞书展示）")
    if llm.get("api_key"):
        secret_lines.append("- 大模型密钥：已配置（不在飞书展示）")
    if secret_lines:
        lines.append("")
        lines.append("密钥状态：")
        lines.extend(secret_lines)
    return "\n".join(lines)


def _load_summary_llm_config(raw: JsonDict, feishu: JsonDict, config_path: Path) -> JsonDict:
    inline = raw.get("llm") or feishu.get("llm") or {}
    llm_path_value = raw.get("llm_config_path") or feishu.get("llm_config_path")
    if not llm_path_value:
        return dict(inline)
    llm_path = Path(str(llm_path_value))
    if not llm_path.is_absolute():
        llm_path = config_path.parent / llm_path
    try:
        loaded = json.loads(llm_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(inline)
    if not isinstance(loaded, dict):
        return dict(inline)
    merged = dict(loaded)
    merged.update(inline)
    return merged


def format_patch_summary(patch: JsonDict) -> str:
    if not patch:
        return "没有配置修改。"
    lines = ["待保存配置修改", "", "修改项："]
    for path, value in patch.items():
        lines.append(f"- {path} = {_display_value(value)}")
    lines.append("")
    lines.append("下一步：")
    lines.append("- 发送“确认保存配置”写入文件。")
    lines.append("- 发送“取消保存配置”放弃修改。")
    return "\n".join(lines)


def _read_config(path: Path) -> JsonDict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigEditError(f"配置文件不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigEditError(f"配置文件不是有效 JSON：{exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigEditError("配置文件根节点必须是 JSON 对象。")
    return raw


def _backup_config(path: Path) -> Path:
    backup_dir = path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"{path.stem}.{timestamp}{path.suffix}"
    shutil.copy2(path, backup_path)
    return backup_path


def _validate_flat_patch(patch: JsonDict) -> None:
    if not patch:
        raise ConfigEditError("没有可保存的配置项。")
    forbidden = [path for path in patch if not _is_allowed_path(path)]
    if forbidden:
        raise ConfigEditError(
            "不允许通过飞书修改这些字段："
            + "、".join(forbidden)
            + "。密钥类字段请继续手动改配置文件。"
        )


def _is_allowed_path(path: str) -> bool:
    parts = path.split(".")
    if len(parts) < 2:
        return False
    if parts[0] == "submission":
        return _is_allowed_submission_path(parts[1:])
    if parts[:2] == ["feishu", "image_analysis"] and len(parts) == 3:
        return parts[2] in FEISHU_IMAGE_ANALYSIS_FIELDS
    if parts[0] == "packaging" and len(parts) == 2:
        return parts[1] in PACKAGING_FIELDS
    return False


def _is_allowed_submission_path(parts: list[str]) -> bool:
    if len(parts) == 1:
        return parts[0] in SIMPLE_SUBMISSION_FIELDS
    if len(parts) == 2:
        return parts[0] in FILE_REF_FIELDS and parts[1] in FILE_REF_FIELDS[parts[0]]
    if len(parts) == 3 and parts[0] in LIST_FILE_REF_FIELDS:
        return parts[1].isdigit() and 0 <= int(parts[1]) <= 9 and parts[2] == "path"
    return False


def _iter_flat_items(value: Any, prefix: str = ""):
    if isinstance(value, dict):
        if not value:
            yield prefix, value
        for key, item in value.items():
            dotted = f"{prefix}.{key}" if prefix else str(key)
            yield from _iter_flat_items(item, dotted)
    elif isinstance(value, list):
        if not value:
            yield prefix, value
        for index, item in enumerate(value):
            dotted = f"{prefix}.{index}" if prefix else str(index)
            yield from _iter_flat_items(item, dotted)
    else:
        yield prefix, value


def _coerce_assignment_value(path: str, value: str) -> Any:
    if path in INT_CONFIG_PATHS:
        try:
            return int(value)
        except ValueError as exc:
            raise ConfigEditError(f"{path} 必须是整数。") from exc
    return value


def _set_dotted_path(target: Any, path: str, value: Any) -> None:
    parts = path.split(".")
    cursor = target
    for index, part in enumerate(parts[:-1]):
        next_part = parts[index + 1]
        if part.isdigit():
            list_index = int(part)
            if not isinstance(cursor, list):
                raise ConfigEditError(f"字段路径无法写入列表：{path}")
            while len(cursor) <= list_index:
                cursor.append({} if not next_part.isdigit() else [])
            cursor = cursor[list_index]
            continue
        if not isinstance(cursor, dict):
            raise ConfigEditError(f"字段路径无法写入对象：{path}")
        if part not in cursor or cursor[part] is None:
            cursor[part] = [] if next_part.isdigit() else {}
        cursor = cursor[part]

    leaf = parts[-1]
    if leaf.isdigit():
        if not isinstance(cursor, list):
            raise ConfigEditError(f"字段路径无法写入列表：{path}")
        list_index = int(leaf)
        while len(cursor) <= list_index:
            cursor.append(None)
        cursor[list_index] = value
    else:
        if not isinstance(cursor, dict):
            raise ConfigEditError(f"字段路径无法写入对象：{path}")
        cursor[leaf] = value


def _ref_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("path") or value.get("url") or "已配置")
    if isinstance(value, list):
        return "、".join(_ref_text(item) for item in value if item) or "未配置"
    return str(value or "未配置")


def _list_pic_paths(value: Any) -> list[str]:
    if not isinstance(value, list):
        return [_ref_text(value)] if value else []
    return [_ref_text(item) for item in value if item]


def _display_value(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
    return text if len(text) <= 120 else text[:119] + "..."
