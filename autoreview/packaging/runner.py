"""Run the legacy package.js APK packaging flow in a controlled way."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any, Callable


JsonDict = dict[str, Any]
LogFn = Callable[[str], None]

REQUIRED_PROJECT_FILES = (
    "packlist.xls",
    "jksconfig.txt",
    "app/build.gradle",
)


@dataclass(frozen=True)
class PackageJob:
    name: str
    project_dir: Path
    channels: list[str]
    script_path: Path
    node_command: str = "node"
    copy_script: bool = True
    skip_start: bool = True


class PackageError(RuntimeError):
    pass


def run_package_job(job: PackageJob, *, dry_run: bool = False, logger: LogFn | None = None) -> JsonDict:
    logger = logger or (lambda message: None)
    _validate_job(job)
    packconfig_path = job.project_dir / "packconfig.txt"
    backup_path = _backup_file(packconfig_path)
    script_to_run = _prepare_script(job)
    packconfig_text = " ".join(job.channels)
    if dry_run:
        return {
            "name": job.name,
            "project_dir": str(job.project_dir),
            "channels": job.channels,
            "script": str(script_to_run),
            "packconfig": packconfig_text,
            "backup_path": str(backup_path) if backup_path else "",
            "dry_run": True,
        }

    packconfig_path.write_text(packconfig_text, encoding="utf-8")
    logger(f"Packaging {job.name}: {packconfig_text}")
    start_time = time.monotonic()
    process = subprocess.Popen(
        [job.node_command, str(script_to_run)],
        cwd=str(job.project_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert process.stdout is not None
    for line in process.stdout:
        logger(line.rstrip())
    code = process.wait()
    elapsed = time.monotonic() - start_time
    elapsed_str = _format_elapsed(elapsed)
    if code != 0:
        raise PackageError(f"package.js failed for {job.name}, exit code {code} (耗时 {elapsed_str})")
    return {
        "name": job.name,
        "project_dir": str(job.project_dir),
        "channels": job.channels,
        "script": str(script_to_run),
        "packconfig": packconfig_text,
        "backup_path": str(backup_path) if backup_path else "",
        "exit_code": code,
        "elapsed_seconds": round(elapsed, 1),
        "elapsed_text": elapsed_str,
    }


def load_package_jobs(batch_file: str | Path, default_script_path: str | Path) -> list[PackageJob]:
    batch_path = Path(batch_file).resolve()
    try:
        payload = json.loads(batch_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PackageError(f"Package batch file not found: {batch_path}") from exc
    except json.JSONDecodeError as exc:
        raise PackageError(f"Package batch file is not valid JSON: {exc}") from exc

    defaults: JsonDict = {}
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        defaults = payload.get("defaults") or {}
        items = payload.get("items") or []
    else:
        raise PackageError("Package batch file must be a JSON array or object")
    if not isinstance(items, list) or not items:
        raise PackageError("Package batch file must provide non-empty items")

    jobs: list[PackageJob] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise PackageError(f"Package batch item {index} must be an object")
        merged = dict(defaults)
        merged.update(item)
        channels = _parse_channels(merged.get("channels"))
        project_dir = _resolve_path(str(merged.get("project_dir") or "."), batch_path.parent)
        script_path = _resolve_path(
            str(merged.get("script") or default_script_path),
            batch_path.parent,
        )
        jobs.append(
            PackageJob(
                name=str(merged.get("name") or project_dir.name),
                project_dir=project_dir,
                channels=channels,
                script_path=script_path,
                node_command=str(merged.get("node_command") or "node"),
                copy_script=bool(merged.get("copy_script", True)),
                skip_start=bool(merged.get("skip_start", True)),
            )
        )
    return jobs


def make_package_job(
    *,
    project_dir: str | Path,
    channels: list[str],
    script_path: str | Path,
    node_command: str = "node",
    copy_script: bool = True,
    skip_start: bool = True,
    name: str | None = None,
) -> PackageJob:
    project_path = Path(project_dir).resolve()
    return PackageJob(
        name=name or project_path.name,
        project_dir=project_path,
        channels=channels,
        script_path=Path(script_path).resolve(),
        node_command=node_command,
        copy_script=copy_script,
        skip_start=skip_start,
    )


def _validate_job(job: PackageJob) -> None:
    if not job.channels:
        raise PackageError("At least one channel/flavor is required")
    if not job.project_dir.exists() or not job.project_dir.is_dir():
        raise PackageError(f"Android project dir not found: {job.project_dir}")
    if not job.script_path.exists() or not job.script_path.is_file():
        raise PackageError(f"package.js not found: {job.script_path}")
    missing = [
        rel_path
        for rel_path in REQUIRED_PROJECT_FILES
        if not (job.project_dir / rel_path).exists()
    ]
    if missing:
        raise PackageError(
            f"Packaging project is missing required files: {', '.join(missing)}"
        )


def _prepare_script(job: PackageJob) -> Path:
    if not job.copy_script and not job.skip_start:
        return job.script_path
    target = job.project_dir / "autoreview_package.js"
    script_text = job.script_path.read_text(encoding="utf-8")
    if job.skip_start:
        script_text = script_text.replace(
            "runStartProcess();",
            "console.info(\"===========跳过 start.bat 自动执行==================\");",
        )
    target.write_text(script_text, encoding="utf-8")
    return target


def _backup_file(path: Path) -> Path | None:
    if not path.exists():
        return None
    backup_dir = path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"{path.name}.{timestamp}.bak"
    shutil.copy2(path, backup_path)
    return backup_path


def _format_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}秒"
    minutes = int(seconds // 60)
    remaining = seconds - minutes * 60
    return f"{minutes}分{remaining:.1f}秒"


def _parse_channels(value: Any) -> list[str]:
    if isinstance(value, str):
        channels = [item.strip() for item in value.replace(",", " ").split() if item.strip()]
    elif isinstance(value, list):
        channels = [str(item).strip() for item in value if str(item).strip()]
    else:
        channels = []
    if not channels:
        raise PackageError("channels must be a non-empty string or array")
    return channels


def _resolve_path(value: str, base_dir: Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()
