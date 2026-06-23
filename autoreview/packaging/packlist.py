"""Read package/channel entries from the legacy packlist file."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import csv
import json
from pathlib import Path
import re
import subprocess
from typing import Any

from .runner import PackageError


@dataclass(frozen=True)
class PacklistEntry:
    sheet: str
    row: int
    channel: str
    app_name: str
    pkg_name: str
    version_code: str
    version_name: str
    res_path: str = ""

    def to_dict(self) -> dict[str, str | int]:
        return asdict(self)


def scan_packlist(project_dir: str | Path) -> list[PacklistEntry]:
    project_path = Path(project_dir).resolve()
    packlist_path = project_path / "packlist.xls"
    if not packlist_path.exists():
        raise PackageError(f"packlist.xls not found: {packlist_path}")
    rows = _read_packlist_rows(packlist_path)
    return _rows_to_entries(rows)


def scan_packlist_snapshot(snapshot_path: str | Path) -> list[PacklistEntry]:
    path = Path(snapshot_path).resolve()
    if not path.exists():
        raise PackageError(f"packlist snapshot not found: {path}")
    try:
        raw = json.loads(_read_text_with_common_encodings(path))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackageError(f"unable to read packlist snapshot: {path}") from exc
    payload = raw.get("result") if isinstance(raw, dict) else raw
    if not isinstance(payload, list):
        raise PackageError("packlist snapshot must be a list or {result: list}")
    return [_entry_from_mapping(item) for item in payload if isinstance(item, dict)]


def resolve_packlist_package(project_dir: str | Path, pkg_name: str) -> list[PacklistEntry]:
    wanted = pkg_name.strip()
    if not wanted:
        raise PackageError("pkg_name must not be empty")
    return [entry for entry in scan_packlist(project_dir) if entry.pkg_name == wanted]


def resolve_packlist_app_name(project_dir: str | Path, app_name: str) -> list[PacklistEntry]:
    return resolve_packlist_app_name_entries(scan_packlist(project_dir), app_name)


def resolve_packlist_app_name_entries(entries: list[PacklistEntry], app_name: str) -> list[PacklistEntry]:
    wanted = _normalize_text(app_name)
    wanted_aliases = _packlist_name_aliases(app_name)
    if not wanted:
        raise PackageError("app_name must not be empty")
    exact = [entry for entry in entries if _normalize_text(entry.app_name) == wanted]
    if exact:
        return exact
    alias_exact = [
        entry
        for entry in entries
        if _normalize_text(entry.app_name) in wanted_aliases
    ]
    if alias_exact:
        return alias_exact
    return [
        entry
        for entry in entries
        if any(alias in _normalize_text(entry.app_name) for alias in wanted_aliases)
    ]


def require_single_package_channel(project_dir: str | Path, pkg_name: str) -> PacklistEntry:
    matches = resolve_packlist_package(project_dir, pkg_name)
    if not matches:
        raise PackageError(f"No packlist channel found for package: {pkg_name}")
    channels = sorted({entry.channel for entry in matches})
    if len(channels) > 1:
        raise PackageError(
            f"Multiple packlist channels found for package {pkg_name}: {', '.join(channels)}"
        )
    return matches[0]


def packlist_entries_to_dicts(entries: list[PacklistEntry]) -> list[dict[str, Any]]:
    return [entry.to_dict() for entry in entries]


def _entry_from_mapping(item: dict[str, Any]) -> PacklistEntry:
    return PacklistEntry(
        sheet=str(item.get("sheet") or ""),
        row=int(item.get("row") or 0),
        channel=str(item.get("channel") or ""),
        app_name=str(item.get("app_name") or ""),
        pkg_name=str(item.get("pkg_name") or ""),
        version_code=str(item.get("version_code") or ""),
        version_name=str(item.get("version_name") or ""),
        res_path=str(item.get("res_path") or ""),
    )


def _read_packlist_rows(packlist_path: Path) -> list[dict[str, Any]]:
    try:
        return _read_with_node_xlsx(packlist_path)
    except PackageError:
        return _read_text_packlist(packlist_path)


def _read_text_with_common_encodings(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-16", "utf-16-le"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8")


def _read_with_node_xlsx(packlist_path: Path) -> list[dict[str, Any]]:
    script = (
        "const xlsx=require('node-xlsx');"
        "const sheets=xlsx.parse(process.argv[1]);"
        "const out=[];"
        "for (const sheet of sheets) {"
        "  for (let i=0; i<(sheet.data||[]).length; i++) {"
        "    out.push({sheet:sheet.name,row:i+1,cells:sheet.data[i]||[]});"
        "  }"
        "}"
        "console.log(JSON.stringify(out));"
    )
    try:
        process = subprocess.run(
            ["node", "-e", script, str(packlist_path)],
            cwd=str(packlist_path.parent),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PackageError(f"node-xlsx unavailable for reading packlist: {exc}") from exc
    if process.returncode != 0:
        raise PackageError(process.stderr.strip() or "node-xlsx failed to read packlist")
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise PackageError(f"node-xlsx returned invalid JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise PackageError("node-xlsx returned unexpected packlist payload")
    return payload


def _read_text_packlist(packlist_path: Path) -> list[dict[str, Any]]:
    try:
        text = packlist_path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PackageError(
            "Unable to read packlist.xls. Install node and node-xlsx in the Android project "
            "or provide a readable text fixture."
        ) from exc
    dialect = csv.excel_tab if "\t" in text else csv.excel
    rows: list[dict[str, Any]] = []
    for index, cells in enumerate(csv.reader(text.splitlines(), dialect=dialect), start=1):
        rows.append({"sheet": packlist_path.stem, "row": index, "cells": cells})
    return rows


def _rows_to_entries(rows: list[dict[str, Any]]) -> list[PacklistEntry]:
    entries: list[PacklistEntry] = []
    for item in rows:
        row_number = int(item.get("row") or 0)
        if row_number <= 3:
            continue
        cells = list(item.get("cells") or [])
        channel = _cell(cells, 2)
        pkg_name = _cell(cells, 4)
        if not channel or not pkg_name:
            continue
        entries.append(
            PacklistEntry(
                sheet=str(item.get("sheet") or ""),
                row=row_number,
                channel=channel,
                app_name=_cell(cells, 1),
                pkg_name=pkg_name,
                version_code=_cell(cells, 7),
                version_name=_cell(cells, 10),
                res_path=_cell(cells, 6),
            )
        )
    return entries


def _cell(cells: list[Any], index: int) -> str:
    if index >= len(cells):
        return ""
    value = cells[index]
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _normalize_text(value: str) -> str:
    return "".join(str(value or "").strip().lower().split())


def _packlist_name_aliases(value: str) -> set[str]:
    normalized = _normalize_text(value)
    aliases = {normalized} if normalized else set()
    grade_match = re.search(r"([一二三四五六七八九0-9]+年级)", normalized)
    semester_match = re.search(r"([上下]册)", normalized)
    for subject in ("语文", "数学", "英语", "物理", "化学", "生物", "历史", "地理", "政治", "科学"):
        if grade_match and semester_match and subject in normalized:
            aliases.add(f"{grade_match.group(1)}{subject}{semester_match.group(1)}")
            aliases.add(f"{grade_match.group(1)}{semester_match.group(1)}{subject}")
    return aliases
