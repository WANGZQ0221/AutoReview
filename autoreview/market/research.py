"""Search public app markets and build competitor download snapshots.

Most app stores do not expose exact download counts through public pages. This
module records the best public metric available per store and leaves exact
downloads empty when the store does not publish them.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import html
import json
import re
from typing import Any, Protocol
from urllib.parse import quote_plus, urlencode
from urllib.request import Request, urlopen


JsonDict = dict[str, Any]


@dataclass(frozen=True)
class AppMarketListing:
    store: str
    app_id: str
    name: str
    developer: str = ""
    package_name: str = ""
    category: str = ""
    url: str = ""
    rating: float | None = None
    rating_count: int | None = None
    downloads: int | None = None
    downloads_text: str = ""
    rank: int | None = None
    raw_metrics: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class AppMarketSearchResult:
    query: str
    apps: list[AppMarketListing]
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> JsonDict:
        return {
            "query": self.query,
            "apps": [app.to_dict() for app in self.apps],
            "errors": list(self.errors),
        }


class AppStoreProvider(Protocol):
    name: str

    def search(self, query: str, *, limit: int = 10) -> list[AppMarketListing]:
        ...


class AppMarketSearcher:
    """Fan out competitor search across supported public stores."""

    def __init__(
        self,
        providers: list[AppStoreProvider] | None = None,
        *,
        timeout_seconds: int = 20,
    ):
        self.providers = providers or [
            AppleAppStoreProvider(timeout_seconds=timeout_seconds),
            GooglePlayProvider(timeout_seconds=timeout_seconds),
        ]

    def search_competitors(
        self,
        query: str,
        *,
        limit: int = 10,
        stores: set[str] | None = None,
    ) -> AppMarketSearchResult:
        clean_query = (query or "").strip()
        if not clean_query:
            return AppMarketSearchResult(query="", apps=[], errors=["empty query"])

        apps: list[AppMarketListing] = []
        errors: list[str] = []
        per_store_limit = max(limit, 1)
        wanted = {store.lower() for store in stores} if stores else None
        for provider in self.providers:
            if wanted and provider.name.lower() not in wanted:
                continue
            try:
                apps.extend(provider.search(clean_query, limit=per_store_limit))
            except Exception as exc:  # pragma: no cover - defensive around public sites
                errors.append(f"{provider.name}: {exc}")
        apps.sort(key=_listing_sort_key)
        return AppMarketSearchResult(query=clean_query, apps=apps[:limit], errors=errors)


class AppleAppStoreProvider:
    name = "apple_app_store"

    def __init__(self, *, country: str = "cn", timeout_seconds: int = 20):
        self.country = country
        self.timeout_seconds = timeout_seconds

    def search(self, query: str, *, limit: int = 10) -> list[AppMarketListing]:
        params = urlencode(
            {
                "term": query,
                "country": self.country,
                "media": "software",
                "entity": "software",
                "limit": str(limit),
            }
        )
        payload = _get_json(f"https://itunes.apple.com/search?{params}", self.timeout_seconds)
        results = payload.get("results") if isinstance(payload, dict) else []
        apps: list[AppMarketListing] = []
        for index, item in enumerate(results or [], start=1):
            if not isinstance(item, dict):
                continue
            app_id = str(item.get("trackId") or item.get("bundleId") or "")
            name = str(item.get("trackName") or "").strip()
            if not app_id or not name:
                continue
            apps.append(
                AppMarketListing(
                    store=self.name,
                    app_id=app_id,
                    name=name,
                    developer=str(item.get("sellerName") or ""),
                    package_name=str(item.get("bundleId") or ""),
                    category=str(item.get("primaryGenreName") or ""),
                    url=str(item.get("trackViewUrl") or ""),
                    rating=_optional_float(item.get("averageUserRating")),
                    rating_count=_optional_int(item.get("userRatingCount")),
                    downloads=None,
                    downloads_text="",
                    rank=index,
                    raw_metrics={
                        "download_metric": "not_public",
                        "country": self.country,
                    },
                )
            )
        return apps


class GooglePlayProvider:
    name = "google_play"

    def __init__(self, *, country: str = "US", language: str = "en", timeout_seconds: int = 20):
        self.country = country
        self.language = language
        self.timeout_seconds = timeout_seconds

    def search(self, query: str, *, limit: int = 10) -> list[AppMarketListing]:
        url = (
            "https://play.google.com/store/search?"
            + urlencode({"q": query, "c": "apps", "gl": self.country, "hl": self.language})
        )
        text = _get_text(url, self.timeout_seconds)
        package_ids = _unique(
            html.unescape(match)
            for match in re.findall(r"/store/apps/details\?id=([^\"&]+)", text)
        )
        apps: list[AppMarketListing] = []
        for index, package_name in enumerate(package_ids[:limit], start=1):
            details = self._fetch_details(package_name)
            apps.append(
                AppMarketListing(
                    store=self.name,
                    app_id=package_name,
                    package_name=package_name,
                    name=details.get("name") or package_name,
                    developer=details.get("developer") or "",
                    category=details.get("category") or "",
                    url=f"https://play.google.com/store/apps/details?id={quote_plus(package_name)}",
                    rating=_optional_float(details.get("rating")),
                    rating_count=_optional_int(details.get("rating_count")),
                    downloads=_parse_download_count(details.get("downloads_text") or ""),
                    downloads_text=details.get("downloads_text") or "",
                    rank=index,
                    raw_metrics={"download_metric": "public_installs_text"},
                )
            )
        return apps

    def _fetch_details(self, package_name: str) -> JsonDict:
        url = (
            f"https://play.google.com/store/apps/details?id={quote_plus(package_name)}&"
            + urlencode({"gl": self.country, "hl": self.language})
        )
        text = _get_text(url, self.timeout_seconds)
        return {
            "name": _first_meta(text, "og:title").replace(" - Apps on Google Play", ""),
            "developer": _first_regex(text, r'"developerName":"([^"]+)"'),
            "category": _first_regex(text, r'"applicationCategory":"([^"]+)"'),
            "rating": _first_regex(text, r'"ratingValue":"?([0-9.]+)"?'),
            "rating_count": _first_regex(text, r'"ratingCount":"?([0-9,]+)"?'),
            "downloads_text": _first_download_text(text),
        }


def build_monthly_snapshot(
    query: str,
    result: AppMarketSearchResult | JsonDict,
    *,
    now: datetime | None = None,
) -> JsonDict:
    current = now or datetime.now()
    month = current.strftime("%Y-%m")
    if isinstance(result, AppMarketSearchResult):
        result_dict = result.to_dict()
    else:
        result_dict = dict(result)
    apps = []
    for app in result_dict.get("apps") or []:
        if isinstance(app, AppMarketListing):
            app = app.to_dict()
        apps.append(
            {
                "store": app.get("store", ""),
                "app_id": app.get("app_id", ""),
                "name": app.get("name", ""),
                "developer": app.get("developer", ""),
                "package_name": app.get("package_name", ""),
                "category": app.get("category", ""),
                "url": app.get("url", ""),
                "downloads": app.get("downloads"),
                "downloads_text": app.get("downloads_text", ""),
                "download_metric": (app.get("raw_metrics") or {}).get("download_metric", ""),
                "rating": app.get("rating"),
                "rating_count": app.get("rating_count"),
            }
        )
    return {
        "month": month,
        "recorded_at": current.isoformat(timespec="seconds"),
        "query": (query or result_dict.get("query") or "").strip(),
        "apps": apps,
        "errors": list(result_dict.get("errors") or []),
    }


def _get_json(url: str, timeout_seconds: int) -> JsonDict:
    return json.loads(_get_text(url, timeout_seconds))


def _get_text(url: str, timeout_seconds: int) -> str:
    request = Request(
        url,
        headers={
            "Accept": "application/json,text/html,*/*",
            "User-Agent": "AutoReview/1.0 (+https://example.invalid/autoreview)",
        },
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        return response.read().decode("utf-8", errors="replace")


def _listing_sort_key(app: AppMarketListing) -> tuple[int, int, int]:
    rating_count = app.rating_count if app.rating_count is not None else -1
    rank = app.rank if app.rank is not None else 9999
    return (rank, -rating_count, 0 if app.downloads is None else -app.downloads)


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value).replace(",", "").strip())
    except ValueError:
        return None


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def _unique(values: Any) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        clean = str(value or "").strip()
        if clean and clean not in seen:
            seen.add(clean)
            result.append(clean)
    return result


def _first_regex(text: str, pattern: str) -> str:
    match = re.search(pattern, text)
    return html.unescape(match.group(1)) if match else ""


def _first_meta(text: str, property_name: str) -> str:
    escaped = re.escape(property_name)
    patterns = [
        rf'<meta[^>]+property=["\']{escaped}["\'][^>]+content=["\']([^"\']+)["\']',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']{escaped}["\']',
    ]
    for pattern in patterns:
        value = _first_regex(text, pattern)
        if value:
            return value
    return ""


def _first_download_text(text: str) -> str:
    patterns = [
        r'"numDownloads":"([^"]+)"',
        r'"installs":"([^"]+)"',
        r'\["([0-9,.]+\+?)","Downloads"\]',
        r'([0-9,.]+\+)\s+Downloads',
    ]
    for pattern in patterns:
        value = _first_regex(text, pattern)
        if value:
            return value
    return ""


def _parse_download_count(value: str) -> int | None:
    text = (value or "").strip()
    if not text:
        return None
    multiplier = 1
    upper = text.upper()
    if "B" in upper:
        multiplier = 1_000_000_000
    elif "M" in upper:
        multiplier = 1_000_000
    elif "K" in upper:
        multiplier = 1_000
    match = re.search(r"([0-9]+(?:[,.][0-9]+)*)", upper)
    if not match:
        return None
    number_text = match.group(1)
    if multiplier == 1:
        return _optional_int(number_text)
    try:
        return int(float(number_text.replace(",", ".")) * multiplier)
    except ValueError:
        return None
