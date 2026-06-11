"""Bind Feishu-uploaded files to OPPO submission config fields."""

from __future__ import annotations

import re
import os
from pathlib import Path
import shutil
from typing import Any

from .config_editor import apply_config_patch_to_file


JsonDict = dict[str, Any]

MATERIAL_ALIASES = {
    "apk": "apk",
    "安装包": "apk",
    "包": "apk",
    "图标": "icon",
    "icon": "icon",
    "截图": "screenshot",
    "应用截图": "screenshot",
    "版权": "copyright",
    "版权证明": "copyright",
    "软著": "copyright",
    "icp": "icp",
    "icp证明": "icp",
    "备案": "icp",
    "备案证明": "icp",
    "特殊类证书": "icp",
}

MATERIAL_EXTENSIONS = {
    "apk": ".apk",
    "icon": ".png",
    "screenshot": ".png",
    "copyright": ".pdf",
    "icp": ".png",
}


class MaterialBindError(ValueError):
    pass


def bind_uploaded_material(
    *,
    config_path: str | Path,
    upload: JsonDict,
    label: str,
) -> JsonDict:
    if not upload:
        raise MaterialBindError("还没有可绑定的最近上传文件。请先在飞书发送文件或图片。")
    source = Path(str(upload.get("path") or ""))
    if not source.exists() or not source.is_file():
        raise MaterialBindError(f"最近上传文件不存在：{source}")

    material_type, index = parse_material_label(label)
    config_path = Path(config_path)
    target = _target_path(config_path, source, material_type, index)
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() != target.resolve():
        shutil.copy2(source, target)

    patch = _material_patch(config_path, target, material_type, index)
    config_result = apply_config_patch_to_file(config_path, patch)
    return {
        "material_type": material_type,
        "index": index,
        "source_path": str(source),
        "target_path": str(target),
        "config_patch": patch,
        "config_update": config_result,
    }


def parse_material_label(label: str) -> tuple[str, int | None]:
    clean = re.sub(r"\s+", "", (label or "").strip().lower())
    clean = clean.replace("：", "").replace(":", "")
    if not clean:
        raise MaterialBindError("请指定材料类型，例如“绑定材料：APK”。")
    index_match = re.search(r"(\d+)$", clean)
    index = int(index_match.group(1)) - 1 if index_match else None
    base = clean[: index_match.start()] if index_match else clean
    if base == "截图":
        return "screenshot", max(index or 0, 0)
    material_type = MATERIAL_ALIASES.get(base)
    if not material_type:
        raise MaterialBindError(f"不支持的材料类型：{label}")
    if material_type == "screenshot":
        return material_type, max(index or 0, 0)
    return material_type, None


def _target_path(config_path: Path, source: Path, material_type: str, index: int | None) -> Path:
    project_dir = config_path.parent.parent
    suffix = source.suffix.lower() or MATERIAL_EXTENSIONS[material_type]
    if material_type == "apk":
        return project_dir / "release" / f"app-release{suffix}"
    if material_type == "icon":
        return project_dir / "assets" / f"icon{suffix}"
    if material_type == "screenshot":
        screenshot_index = (index or 0) + 1
        return project_dir / "assets" / f"screenshot-{screenshot_index}{suffix}"
    if material_type == "copyright":
        return project_dir / "assets" / f"copyright{suffix}"
    if material_type == "icp":
        return project_dir / "assets" / f"icp-proof{suffix}"
    raise MaterialBindError(f"不支持的材料类型：{material_type}")


def _material_patch(
    config_path: Path,
    target: Path,
    material_type: str,
    index: int | None,
) -> JsonDict:
    rel_path = _relative_config_path(config_path, target)
    if material_type == "apk":
        return {"submission.apk_url.path": rel_path}
    if material_type == "icon":
        return {"submission.icon_url.path": rel_path}
    if material_type == "screenshot":
        return {f"submission.pic_url.{index or 0}.path": rel_path}
    if material_type == "copyright":
        return {"submission.copyright_url.path": rel_path}
    if material_type == "icp":
        return {"submission.special_url.0.path": rel_path}
    raise MaterialBindError(f"不支持的材料类型：{material_type}")


def _relative_config_path(config_path: Path, target: Path) -> str:
    return Path(os.path.relpath(target, config_path.parent)).as_posix()
