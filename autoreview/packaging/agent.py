"""Chat-facing helpers for APK packaging jobs."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Callable

from .packlist import (
    require_single_package_channel,
    resolve_packlist_app_name,
    resolve_packlist_app_name_entries,
    scan_packlist_snapshot,
)
from .runner import (
    PackageError,
    load_package_jobs,
    make_package_job,
    run_package_job,
)


JsonDict = dict[str, Any]
LogFn = Callable[[str], None]


@dataclass(frozen=True)
class PackagingSettings:
    project_dir: Path | None = None
    script_path: Path | None = None
    batch_file: Path | None = None
    packlist_scan_file: Path | None = None
    node_command: str = "node"
    skip_start: bool = True


class PackagingAgent:
    def __init__(self, config_path: str | Path | None = None, *, logger: LogFn | None = None):
        self.config_path = Path(config_path).resolve() if config_path else None
        self.logger = logger or (lambda message: None)
        self.settings = self._load_settings()

    def package_one(
        self,
        *,
        app_name: str = "",
        pkg_name: str = "",
        channels: list[str] | None = None,
        dry_run: bool = False,
    ) -> JsonDict:
        project_dir = self._require_project_dir()
        script_path = self._require_script_path()
        resolved_entry = None
        resolved_channels = [item for item in channels or [] if item]
        if app_name and not pkg_name:
            matches = resolve_packlist_app_name(project_dir, app_name)
            if not matches:
                raise PackageError(f"未找到应用名对应的渠道：{app_name}")
            if len(matches) > 1:
                channels_text = "、".join(sorted({entry.channel for entry in matches}))
                packages_text = "、".join(sorted({entry.pkg_name for entry in matches}))
                raise PackageError(
                    f"应用名 {app_name} 匹配到多个渠道：{channels_text}；请补充包名进一步确认，"
                    f"对应包名有：{packages_text}"
                )
            resolved_entry = matches[0]
            resolved_channels = [resolved_entry.channel]
        if pkg_name:
            resolved_entry = require_single_package_channel(project_dir, pkg_name)
            if resolved_channels and resolved_entry.channel not in resolved_channels:
                raise PackageError(
                    f"渠道和包名不匹配：{pkg_name} 对应 {resolved_entry.channel}"
                )
            resolved_channels = [resolved_entry.channel]
        if not resolved_channels:
            raise PackageError("缺少包名或渠道。可以说“打包 com.example.app”或“打包渠道 xm1067”。")
        job = make_package_job(
            project_dir=project_dir,
            channels=resolved_channels,
            script_path=script_path,
            node_command=self.settings.node_command,
            skip_start=self.settings.skip_start,
        )
        result = run_package_job(job, dry_run=dry_run, logger=self.logger)
        if resolved_entry:
            result["resolved_package"] = resolved_entry.to_dict()
        result["latest_apks"] = [str(path) for path in find_latest_apks(project_dir)]
        return result

    def package_batch(self, *, dry_run: bool = False, continue_on_error: bool = True) -> list[JsonDict]:
        batch_file = self._require_batch_file()
        default_script = self.settings.script_path or Path("package.js")
        jobs = load_package_jobs(batch_file, default_script)
        results: list[JsonDict] = []
        for index, job in enumerate(jobs, start=1):
            try:
                self.logger(f"Running package job {index}/{len(jobs)}: {job.name}")
                result = run_package_job(job, dry_run=dry_run, logger=self.logger)
                result["latest_apks"] = [str(path) for path in find_latest_apks(job.project_dir)]
                results.append({"ok": True, **result})
            except PackageError as exc:
                entry = {"ok": False, "name": job.name, "error": str(exc)}
                results.append(entry)
                if not continue_on_error:
                    raise
        return results

    def _load_settings(self) -> PackagingSettings:
        if not self.config_path or not self.config_path.exists():
            return PackagingSettings()
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return PackagingSettings()
        if not isinstance(raw, dict):
            return PackagingSettings()
        packaging = raw.get("packaging") or raw.get("package") or {}
        if not isinstance(packaging, dict):
            return PackagingSettings()
        base_dir = self.config_path.parent
        return PackagingSettings(
            project_dir=_optional_path(packaging.get("project_dir"), base_dir),
            script_path=_optional_path(packaging.get("script") or packaging.get("script_path"), base_dir),
            batch_file=_optional_path(packaging.get("batch_file"), base_dir),
            packlist_scan_file=_optional_path(
                packaging.get("packlist_scan_file") or packaging.get("packlist_snapshot"),
                base_dir,
            ),
            node_command=str(packaging.get("node_command") or "node"),
            skip_start=bool(packaging.get("skip_start", True)),
        )

    def _require_project_dir(self) -> Path:
        if not self.settings.project_dir:
            raise PackageError("未配置 packaging.project_dir。")
        return self.settings.project_dir

    def _require_script_path(self) -> Path:
        if not self.settings.script_path:
            raise PackageError("未配置 packaging.script。")
        return self.settings.script_path

    def _require_batch_file(self) -> Path:
        if not self.settings.batch_file:
            raise PackageError("未配置 packaging.batch_file。")
        return self.settings.batch_file


def parse_package_request(text: str) -> JsonDict:
    clean = str(text or "").strip()
    lowered = clean.lower()
    dry_run = any(term in lowered for term in ("dry-run", "dry run", "试跑", "预演", "只验证", "不真打"))
    batch = any(term in clean for term in ("批量打包", "打包全部", "批量构建"))
    pkg_match = re.search(r"([A-Za-z][\w]*(?:\.[A-Za-z][\w]*){2,})", clean)
    app_name = _extract_app_name(clean)
    channel_match = re.search(r"(?:渠道|channel)\s*[:：]?\s*([A-Za-z0-9_-]+)", clean, flags=re.IGNORECASE)
    channels: list[str] = []
    if channel_match:
        channels = [channel_match.group(1)]
    elif not pkg_match and not app_name:
        payload = _extract_payload(clean)
        if payload and not payload.lower().endswith((".json", ".xls")):
            channels = [
                item.strip()
                for item in re.split(r"[,，\s]+", payload)
                if item.strip() and not item.strip().startswith("--")
            ]
    return {
        "batch": batch,
        "dry_run": dry_run,
        "app_name": app_name,
        "pkg_name": pkg_match.group(1) if pkg_match else "",
        "channels": channels,
    }


def format_package_result(result: JsonDict, *, dry_run: bool = False) -> str:
    title = "打包预演" if dry_run else "打包完成"
    lines = [title]
    resolved = result.get("resolved_package") or {}
    lines.append("")
    lines.append("应用信息：")
    if resolved:
        lines.append(f"- 应用：{resolved.get('app_name') or '未知'}")
        lines.append(f"- 包名：{resolved.get('pkg_name') or '未知'}")
        lines.append(f"- 渠道：{resolved.get('channel') or '未知'}")
        lines.append(f"- 版本：{resolved.get('version_code') or '未知'} / {resolved.get('version_name') or '未知'}")
    else:
        lines.append("- 渠道：" + "、".join(str(item) for item in result.get("channels") or []))

    lines.append("")
    lines.append("打包信息：")
    lines.append(f"- 项目：{result.get('project_dir')}")
    if result.get("packconfig"):
        lines.append(f"- packconfig：{result['packconfig']}")
    if result.get("backup_path"):
        lines.append(f"- 备份：{result['backup_path']}")
    latest = result.get("latest_apks") or []
    if latest and not dry_run:
        lines.append("")
        lines.append("输出文件：")
        lines.append("- 最新 APK：" + str(latest[0]))
    if dry_run:
        lines.append("")
        lines.append("说明：这是预演，没有真正打包。")
    return "\n".join(lines)


def format_batch_package_result(results: list[JsonDict], *, dry_run: bool = False) -> str:
    success = [item for item in results if item.get("ok", True)]
    failed = [item for item in results if not item.get("ok", True)]
    title = "批量打包预演" if dry_run else "批量打包完成"
    lines = [title, "", "汇总："]
    lines.append(f"- 成功：{len(success)}")
    lines.append(f"- 失败：{len(failed)}")
    lines.append("")
    lines.append("任务结果：")
    for item in results[:8]:
        if item.get("ok", True):
            channels = "、".join(str(value) for value in item.get("channels") or [])
            lines.append(f"- {item.get('name')}：{channels}")
        else:
            lines.append(f"- {item.get('name')}：失败，{item.get('error')}")
    if dry_run:
        lines.append("")
        lines.append("说明：这是预演，没有真正打包。")
    return "\n".join(lines)


def find_latest_apks(project_dir: str | Path, *, limit: int = 5) -> list[Path]:
    root = Path(project_dir)
    candidates = list(root.rglob("*.apk")) if root.exists() else []
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[:limit]


def _optional_path(value: Any, base_dir: Path) -> Path | None:
    if value in (None, ""):
        return None
    path = Path(str(value))
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _extract_payload(text: str) -> str:
    parts = re.split(r"[:：]", text, maxsplit=1)
    return parts[1].strip() if len(parts) == 2 else ""


def _extract_app_name(text: str) -> str:
    clean = str(text or "").strip()
    if not clean:
        return ""
    clean = re.sub(r"^(帮我|请|麻烦|能不能|可以|想要|我要|我想|需要)\s*", "", clean)
    clean = re.sub(r"^(打包|查找|查询|定位|看看|看看一下|帮我找|帮我查)\s*", "", clean)
    clean = re.sub(r"\b(dry-run|dry run)\b", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"(试跑|预演|只验证|不真打)", "", clean)
    clean = re.sub(r"(的)?(包|APK|apk|渠道|版本|应用|软件)\s*$", "", clean)
    clean = re.sub(r"[：:，,。！？?\s]+$", "", clean).strip()
    if _looks_like_pkg_name(clean) or _looks_like_channel_name(clean):
        return ""
    if len(clean) < 2:
        return ""
    return clean


def _looks_like_pkg_name(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z][\w]*(?:\.[A-Za-z][\w]*){2,}", value or ""))


def _looks_like_channel_name(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{3,}", value or ""))
