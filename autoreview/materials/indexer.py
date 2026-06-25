"""Index local app-store release materials and suggest submission patches."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import re
from typing import Any, Iterable

from autoreview.packaging.packlist import PacklistEntry, scan_packlist_snapshot


JsonDict = dict[str, Any]

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".zip", ".rar", ".mp4"}
RESOURCE_EXTENSIONS = IMAGE_EXTENSIONS | DOCUMENT_EXTENSIONS

ICON_HINTS = ("icon", "icons", "playstore-icon", "216.png", "android")
SCREENSHOT_HINTS = ("screenshot", "截图")
COPYRIGHT_HINTS = ("版权", "软著")
ICP_HINTS = ("icp", "备案", "教育部备案")
SPECIAL_HINTS = ("免责函", "承诺函", "授权书", "许可证", "营业执照", "资质")
PROMO_HINTS = ("宣传图", "封面")


class MaterialIndexError(ValueError):
    pass


@dataclass(frozen=True)
class MaterialCandidate:
    kind: str
    path: str
    score: int
    reason: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class MaterialSuggestion:
    app: JsonDict | None
    query: str
    root: str
    patch: JsonDict
    candidates: dict[str, list[MaterialCandidate]]
    warnings: list[str]

    def to_dict(self) -> JsonDict:
        return {
            "app": self.app,
            "query": self.query,
            "root": self.root,
            "patch": self.patch,
            "candidates": {
                key: [candidate.to_dict() for candidate in value]
                for key, value in self.candidates.items()
            },
            "warnings": self.warnings,
        }


def suggest_submission_materials(
    *,
    root: str | Path,
    app_name: str = "",
    pkg_name: str = "",
    packlist_snapshot: str | Path | None = None,
    config_path: str | Path | None = None,
    max_screenshots: int = 5,
) -> MaterialSuggestion:
    root_path = Path(root)
    if not root_path.exists() or not root_path.is_dir():
        raise MaterialIndexError(f"materials root not found: {root_path}")

    app = _resolve_app(
        app_name=app_name,
        pkg_name=pkg_name,
        packlist_snapshot=packlist_snapshot,
    )
    query_name = app.app_name if app else app_name.strip()
    if not query_name and pkg_name:
        query_name = pkg_name.strip()
    if not query_name:
        raise MaterialIndexError("app_name or pkg_name is required")

    files = _iter_resource_files(root_path)
    aliases = _material_aliases(query_name)
    if app:
        aliases.update(_material_aliases(app.pkg_name))
        aliases.update(_material_aliases(app.channel))

    res_path = app.res_path if app else ""
    res_files = _iter_resource_files(Path(res_path)) if res_path and Path(res_path).is_dir() else []

    candidates = {
        "icon": _rank_candidates(files, aliases, "icon"),
        "screenshots": _rank_candidates(files, aliases, "screenshots"),
        "copyright": _rank_candidates(files, aliases, "copyright"),
        "icp": _rank_candidates(files, aliases, "icp"),
        "special": _rank_candidates(files, aliases, "special"),
        "promo": _rank_candidates(files, aliases, "promo"),
    }

    if res_files:
        _merge_res_path_candidates(candidates, res_files, aliases, res_path)

    selected = _select_candidates(candidates, max_screenshots=max_screenshots)
    patch = _build_patch(app, selected, config_path=config_path)
    warnings = _warnings_for_selection(selected)
    return MaterialSuggestion(
        app=app.to_dict() if app else None,
        query=query_name,
        root=str(root_path),
        patch=patch,
        candidates=candidates,
        warnings=warnings,
    )


def _resolve_app(
    *,
    app_name: str,
    pkg_name: str,
    packlist_snapshot: str | Path | None,
) -> PacklistEntry | None:
    if not packlist_snapshot:
        return None
    snapshot_path = Path(packlist_snapshot)
    if not snapshot_path.exists():
        if pkg_name:
            raise MaterialIndexError(f"packlist snapshot not found: {snapshot_path}")
        return None
    entries = scan_packlist_snapshot(snapshot_path)
    matches: list[PacklistEntry] = []
    if pkg_name:
        wanted_pkg = pkg_name.strip()
        matches = [entry for entry in entries if entry.pkg_name == wanted_pkg]
    elif app_name:
        wanted_aliases = _material_aliases(app_name)
        exact = [
            entry
            for entry in entries
            if _normalize_text(entry.app_name) in wanted_aliases
        ]
        matches = exact or [
            entry
            for entry in entries
            if any(alias and alias in _normalize_text(entry.app_name) for alias in wanted_aliases)
        ]
    if not matches:
        return None
    matches = sorted(matches, key=lambda entry: (entry.app_name, entry.channel))
    return matches[0]


def _iter_resource_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.name.startswith("._") or path.name.startswith("~$"):
            continue
        if path.suffix.lower() in RESOURCE_EXTENSIONS:
            files.append(path)
    return files


def _rank_candidates(
    files: Iterable[Path],
    aliases: set[str],
    kind: str,
) -> list[MaterialCandidate]:
    ranked: list[MaterialCandidate] = []
    for path in files:
        score, reason = _score_path(path, aliases, kind)
        if score > 0:
            ranked.append(MaterialCandidate(kind=kind, path=str(path), score=score, reason=reason))
    return sorted(ranked, key=lambda item: (-item.score, _path_sort_key(item.path)))[:20]


def _merge_res_path_candidates(
    candidates: dict[str, list[MaterialCandidate]],
    res_files: list[Path],
    aliases: set[str],
    res_path: str,
) -> None:
    res_normalized = _normalize_text(res_path)
    icon_files = [f for f in res_files if f.suffix.lower() in IMAGE_EXTENSIONS]
    if icon_files:
        for f in icon_files:
            name = _normalize_text(f.name)
            if any(h in name for h in ICON_HINTS) or any(h in res_normalized for h in ("icon", "icons")):
                candidate = MaterialCandidate(
                    kind="icon", path=str(f), score=200, reason="packlist res_path icon",
                )
                existing = candidates["icon"]
                if not any(c.path == candidate.path for c in existing):
                    existing.insert(0, candidate)
                break

    material_root = _material_root_from_res_path(res_path)
    sibling_files = [
        f
        for f in material_root.rglob("*")
        if f.is_file() and f.suffix.lower() in RESOURCE_EXTENSIONS
    ]
    for kind, hints in (("screenshots", SCREENSHOT_HINTS), ("copyright", COPYRIGHT_HINTS), ("special", SPECIAL_HINTS)):
        for f in sibling_files:
            text = _normalize_text(str(f))
            if any(_normalize_text(h) in text for h in hints):
                alias_hits = [a for a in aliases if a and a in text]
                if kind == "screenshots":
                    score = 170 if _is_in_named_dir(f, {"screenshot", "screenshots", "截图"}) else 130
                else:
                    score = 150 if alias_hits else 70
                candidate = MaterialCandidate(
                    kind=kind, path=str(f), score=score, reason="packlist res_path material root",
                )
                existing = candidates[kind]
                if not any(c.path == candidate.path for c in existing):
                    existing.append(candidate)
    for key, value in candidates.items():
        candidates[key] = sorted(value, key=lambda item: (-item.score, _path_sort_key(item.path)))[:20]


def _material_root_from_res_path(res_path: str | Path) -> Path:
    path = Path(res_path)
    name = _normalize_text(path.name)
    parent_name = _normalize_text(path.parent.name)
    if name in {"android", "ios"} and parent_name.startswith("icons"):
        return path.parent.parent
    if name.startswith("icons"):
        return path.parent
    if name in {"android", "ios"}:
        return path.parent
    return path.parent


def _is_in_named_dir(path: Path, names: set[str]) -> bool:
    normalized_names = {_normalize_text(name) for name in names}
    return any(_normalize_text(part) in normalized_names for part in path.parts[:-1])


def _score_path(path: Path, aliases: set[str], kind: str) -> tuple[int, str]:
    text = _normalize_text(str(path))
    name = _normalize_text(path.name)
    suffix = path.suffix.lower()
    score = 0
    reasons: list[str] = []
    category_hit = False

    alias_hits = [alias for alias in aliases if alias and alias in text]
    if alias_hits:
        score += 60 + min(len(max(alias_hits, key=len)), 20)
        reasons.append("matched app alias")

    if kind == "icon":
        if suffix not in IMAGE_EXTENSIONS:
            return 0, ""
        if any(_normalize_text(hint) in text for hint in ICON_HINTS):
            category_hit = True
            score += 45
            reasons.append("icon hint")
        if name in {"216.png", "playstoreicon.png", "playstore-icon.png"}:
            category_hit = True
            score += 20
            reasons.append("store icon filename")
    elif kind == "screenshots":
        if suffix not in IMAGE_EXTENSIONS:
            return 0, ""
        if any(_normalize_text(hint) in text for hint in SCREENSHOT_HINTS):
            category_hit = True
            score += 45
            reasons.append("screenshot hint")
        if re.search(r"(1080|720|480|450)", text):
            score += 8
            reasons.append("screen size hint")
    elif kind == "copyright":
        if suffix not in RESOURCE_EXTENSIONS:
            return 0, ""
        if any(_normalize_text(hint) in text for hint in COPYRIGHT_HINTS):
            category_hit = True
            score += 50
            reasons.append("copyright hint")
    elif kind == "icp":
        if suffix not in RESOURCE_EXTENSIONS:
            return 0, ""
        if any(_normalize_text(hint) in text for hint in ICP_HINTS):
            category_hit = True
            score += 50
            reasons.append("备案/ICP hint")
    elif kind == "special":
        if suffix not in RESOURCE_EXTENSIONS:
            return 0, ""
        if any(_normalize_text(hint) in text for hint in SPECIAL_HINTS):
            category_hit = True
            score += 40
            reasons.append("special material hint")
    elif kind == "promo":
        if suffix not in IMAGE_EXTENSIONS:
            return 0, ""
        if any(_normalize_text(hint) in text for hint in PROMO_HINTS):
            category_hit = True
            score += 35
            reasons.append("promo image hint")

    if not category_hit or not alias_hits:
        return 0, ""
    if suffix in {".pdf", ".png", ".jpg", ".jpeg"}:
        score += 3
    return score, ", ".join(reasons)


def _select_candidates(
    candidates: dict[str, list[MaterialCandidate]],
    *,
    max_screenshots: int,
) -> dict[str, list[MaterialCandidate]]:
    return {
        "icon": candidates.get("icon", [])[:1],
        "screenshots": _dedupe_by_name(candidates.get("screenshots", []))[:max_screenshots],
        "copyright": candidates.get("copyright", [])[:1],
        "icp": candidates.get("icp", [])[:1],
        "special": _select_special_candidates(candidates.get("special", [])),
        "promo": candidates.get("promo", [])[:3],
    }


def _build_patch(
    app: PacklistEntry | None,
    selected: dict[str, list[MaterialCandidate]],
    *,
    config_path: str | Path | None,
) -> JsonDict:
    patch: JsonDict = {}
    if app:
        patch["submission.pkg_name"] = app.pkg_name
        patch["submission.version_code"] = app.version_code
        patch["submission.version_name"] = app.version_name
        patch["submission.app_name"] = app.app_name

    if selected["icon"]:
        patch["submission.icon_url.path"] = _patch_path(selected["icon"][0].path, config_path)
    for index, candidate in enumerate(selected["screenshots"]):
        patch[f"submission.pic_url.{index}.path"] = _patch_path(candidate.path, config_path)
    if selected["copyright"]:
        patch["submission.copyright_url.path"] = _patch_path(selected["copyright"][0].path, config_path)
    if selected["icp"]:
        patch["submission.icp_url.path"] = _patch_path(selected["icp"][0].path, config_path)
    for index, candidate in enumerate(selected["special"]):
        patch[f"submission.special_url.{index}.path"] = _patch_path(candidate.path, config_path)
    return patch


def _patch_path(path: str, config_path: str | Path | None) -> str:
    source = Path(path)
    if not config_path:
        return str(source)
    config_dir = Path(config_path).resolve().parent
    try:
        return source.resolve().relative_to(config_dir).as_posix()
    except ValueError:
        return str(source)


def _warnings_for_selection(selected: dict[str, list[MaterialCandidate]]) -> list[str]:
    warnings: list[str] = []
    for key, label in (
        ("icon", "图标"),
        ("screenshots", "截图"),
        ("copyright", "版权/软著材料"),
    ):
        if not selected[key]:
            warnings.append(f"未匹配到{label}")
    if not selected["icp"] and not selected["special"]:
        warnings.append("未匹配到备案/资质/授权类补充材料")
    return warnings


def _dedupe_by_name(candidates: list[MaterialCandidate]) -> list[MaterialCandidate]:
    seen: set[str] = set()
    result: list[MaterialCandidate] = []
    for candidate in candidates:
        key = Path(candidate.path).name.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


def _select_special_candidates(candidates: list[MaterialCandidate]) -> list[MaterialCandidate]:
    deduped = _dedupe_by_name(candidates)
    oppo = [candidate for candidate in deduped if "oppo" in _normalize_text(candidate.path)]
    if oppo:
        return oppo[:1]
    return deduped[:3]


def _material_aliases(value: str) -> set[str]:
    normalized = _normalize_text(value)
    aliases = {normalized} if normalized else set()
    if not normalized:
        return aliases
    aliases.add(normalized.replace("点读软件", "点读"))
    aliases.add(normalized.replace("同步软件", "同步"))
    aliases.add(normalized.replace("辅导", ""))
    aliases.add(re.sub(r"^com\.pelbs\.book", "", normalized))
    grade_match = re.search(r"([一二三四五六七八九0-9]+年级)", normalized)
    semester_match = re.search(r"([上下]册)", normalized)
    for subject in ("语文", "数学", "英语"):
        if grade_match and semester_match and subject in normalized:
            aliases.add(f"{grade_match.group(1)}{subject}{semester_match.group(1)}")
            aliases.add(f"{grade_match.group(1)}{semester_match.group(1)}{subject}")
    return {alias for alias in aliases if alias}


def _normalize_text(value: str) -> str:
    return "".join(str(value or "").strip().lower().split()).replace("-", "")


def _path_sort_key(path: str) -> tuple[str, str]:
    item = Path(path)
    return (str(item.parent), item.name)
