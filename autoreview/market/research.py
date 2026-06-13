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
from urllib.parse import quote, quote_plus, urlencode, urljoin
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
    store_statuses: list[JsonDict] = field(default_factory=list)

    def to_dict(self) -> JsonDict:
        return {
            "query": self.query,
            "apps": [app.to_dict() for app in self.apps],
            "errors": list(self.errors),
            "store_statuses": list(self.store_statuses),
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
            OppoAppMarketProvider(timeout_seconds=timeout_seconds),
            XiaomiAppStoreProvider(timeout_seconds=timeout_seconds),
            VivoAppStoreProvider(timeout_seconds=timeout_seconds),
            HuaweiAppGalleryProvider(timeout_seconds=timeout_seconds),
            HonorAppMarketProvider(timeout_seconds=timeout_seconds),
        ]

    def search_competitors(
        self,
        query: str,
        *,
        limit: int = 10,
        stores: set[str] | None = None,
    ) -> AppMarketSearchResult:
        clean_query = _normalize_search_query(query)
        if not clean_query:
            return AppMarketSearchResult(query="", apps=[], errors=["empty query"])

        apps: list[AppMarketListing] = []
        errors: list[str] = []
        store_statuses: list[JsonDict] = []
        per_store_limit = max(limit, 1)
        wanted = {store.lower() for store in stores} if stores else None
        for provider in self.providers:
            if wanted and provider.name.lower() not in wanted:
                continue
            try:
                provider_apps = provider.search(clean_query, limit=per_store_limit)
                apps.extend(provider_apps)
                store_statuses.append(_provider_status(provider, provider_apps))
            except Exception as exc:  # pragma: no cover - defensive around public sites
                message = _short_error(exc)
                errors.append(f"{provider.name}: {message}")
                store_statuses.append(
                    {
                        "store": provider.name,
                        "status": "failed",
                        "result_count": 0,
                        "message": message,
                    }
                )
        apps.sort(key=_listing_sort_key)
        return AppMarketSearchResult(query=clean_query, apps=apps[:limit], errors=errors, store_statuses=store_statuses)


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


class PublicHtmlAppStoreProvider:
    """Best-effort parser for public app-store search pages.

    Domestic Android stores frequently change their public page markup and often
    do not expose exact install counts. This provider records public links and
    visible metrics when they can be parsed, and otherwise leaves metrics empty.
    """

    name = "public_html_store"
    display_name = "Public HTML Store"
    base_url = ""
    search_url_templates: tuple[str, ...] = ()
    detail_url_patterns: tuple[str, ...] = ()

    def __init__(self, *, timeout_seconds: int = 20):
        self.timeout_seconds = timeout_seconds

    def search(self, query: str, *, limit: int = 10) -> list[AppMarketListing]:
        self.last_status = "no_match"
        self.last_status_message = "未解析到匹配结果"
        errors: list[str] = []
        for template in self.search_url_templates:
            url = self._format_url(template, query)
            try:
                text = _get_text(url, self.timeout_seconds)
            except Exception as exc:
                errors.append(str(exc))
                continue
            apps = self._parse_search_page(text, query=query, search_url=url, limit=limit)
            if apps:
                self.last_status = "ok"
                self.last_status_message = ""
                return apps
        if errors and all(_is_public_page_not_found(error) for error in errors):
            self.last_status = "skipped"
            self.last_status_message = "公开搜索入口不可用，已跳过"
            return []
        if errors:
            raise RuntimeError("; ".join(errors[:2]))
        return []

    def _format_url(self, template: str, query: str) -> str:
        return template.format(query=quote(query), query_plus=quote_plus(query))

    def _parse_search_page(
        self,
        text: str,
        *,
        query: str,
        search_url: str,
        limit: int,
    ) -> list[AppMarketListing]:
        apps = self._parse_json_ld_apps(text, search_url=search_url)
        apps.extend(self._parse_anchor_apps(text, search_url=search_url))
        unique_apps = _dedupe_listings(apps)
        filtered = [app for app in unique_apps if _listing_matches_query(app, query)]
        selected = filtered
        return [
            _with_rank(app, index)
            for index, app in enumerate(selected[:limit], start=1)
        ]

    def _parse_json_ld_apps(self, text: str, *, search_url: str) -> list[AppMarketListing]:
        apps: list[AppMarketListing] = []
        for script in re.findall(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            text,
            flags=re.S | re.I,
        ):
            try:
                data = json.loads(html.unescape(script).strip())
            except json.JSONDecodeError:
                continue
            for item in _iter_json_objects(data):
                type_value = item.get("@type")
                if isinstance(type_value, list):
                    type_match = any(str(value).lower() == "softwareapplication" for value in type_value)
                else:
                    type_match = str(type_value or "").lower() == "softwareapplication"
                if not type_match:
                    continue
                name = str(item.get("name") or "").strip()
                url = str(item.get("url") or "").strip()
                if not name:
                    continue
                absolute_url = urljoin(search_url, url) if url else search_url
                aggregate = item.get("aggregateRating") if isinstance(item.get("aggregateRating"), dict) else {}
                offers = item.get("offers") if isinstance(item.get("offers"), dict) else {}
                downloads_text = (
                    str(item.get("downloadCount") or item.get("numDownloads") or item.get("downloads") or "")
                    or _first_download_text(json.dumps(item, ensure_ascii=False))
                )
                apps.append(
                    AppMarketListing(
                        store=self.name,
                        app_id=_app_id_from_url(absolute_url) or name,
                        name=name,
                        developer=str(item.get("author") or item.get("creator") or item.get("publisher") or ""),
                        category=str(item.get("applicationCategory") or item.get("genre") or ""),
                        url=absolute_url,
                        rating=_optional_float(aggregate.get("ratingValue")),
                        rating_count=_optional_int(aggregate.get("ratingCount") or aggregate.get("reviewCount")),
                        downloads_text=downloads_text,
                        downloads=_parse_download_count(downloads_text),
                        raw_metrics={
                            "download_metric": "public_page_best_effort",
                            "price": offers.get("price"),
                        },
                    )
                )
        return apps

    def _parse_anchor_apps(self, text: str, *, search_url: str) -> list[AppMarketListing]:
        apps: list[AppMarketListing] = []
        for href, label in re.findall(
            r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
            text,
            flags=re.S | re.I,
        ):
            clean_label = _clean_html_text(label)
            if not clean_label or len(clean_label) > 80:
                continue
            absolute_url = urljoin(search_url, html.unescape(href))
            if not self._looks_like_detail_url(absolute_url):
                continue
            raw_href = html.unescape(href)
            around = _text_around(text, href, radius=600) or _text_around(text, raw_href, radius=600)
            downloads_text = _first_download_text(around)
            apps.append(
                AppMarketListing(
                    store=self.name,
                    app_id=_app_id_from_url(absolute_url) or clean_label,
                    name=clean_label,
                    url=absolute_url,
                    rating=_optional_float(_first_rating_text(around)),
                    downloads=_parse_download_count(downloads_text),
                    downloads_text=downloads_text,
                    raw_metrics={"download_metric": "public_page_best_effort"},
                )
            )
        return apps

    def _looks_like_detail_url(self, url: str) -> bool:
        lowered = url.lower()
        if self.detail_url_patterns:
            return any(re.search(pattern, lowered) for pattern in self.detail_url_patterns)
        return any(part in lowered for part in ("/app/", "/appinfo", "/details", "/detail"))


class OppoAppMarketProvider(PublicHtmlAppStoreProvider):
    name = "oppo_app_market"
    display_name = "OPPO 软件商店"
    base_url = "https://app.cdo.oppomobile.com"
    search_url_templates = (
        "https://app.cdo.oppomobile.com/home/store/index.json?start=0&size={limit}",
        "https://app.cdo.oppomobile.com/home/store/required.json?start=0&size={limit}",
        "https://app.cdo.oppomobile.com/home/store?module=2",
        "https://m.store.oppomobile.com/",
        "https://www.heytapmobi.com/cn/search?keyword={query_plus}",
        "https://www.heytapmobi.com/cn/search?q={query_plus}",
        "https://www.heytapmobi.com/m/store/search?keyword={query_plus}",
    )
    detail_url_patterns = (
        r"app\.cdo\.oppomobile\.com",
        r"store\.oppomobile\.com",
        r"istore\.oppomobile\.com/download",
        r"softmarket://market_appdetail",
        r"heytapmobi\.com/.*/app",
        r"heytapmobi\.com/.*/detail",
    )

    def _format_url(self, template: str, query: str) -> str:
        return template.format(
            query=quote(query),
            query_plus=quote_plus(query),
            limit=20,
        )

    def _parse_search_page(
        self,
        text: str,
        *,
        query: str,
        search_url: str,
        limit: int,
    ) -> list[AppMarketListing]:
        apps = self._parse_oppo_json_apps(text, search_url=search_url)
        apps.extend(self._parse_oppo_html_apps(text, search_url=search_url))
        if apps:
            unique_apps = _dedupe_listings(apps)
            filtered = [app for app in unique_apps if _listing_matches_query(app, query)]
            return [_with_rank(app, index) for index, app in enumerate(filtered[:limit], start=1)]
        return super()._parse_search_page(text, query=query, search_url=search_url, limit=limit)

    def _parse_oppo_json_apps(self, text: str, *, search_url: str) -> list[AppMarketListing]:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return []

        apps: list[AppMarketListing] = []
        for item in _iter_json_objects(data):
            if not isinstance(item, dict):
                continue
            name = str(item.get("appName") or item.get("name") or "").strip()
            package_name = str(item.get("pkgName") or item.get("packageName") or item.get("pkg") or "").strip()
            app_id = str(item.get("appId") or item.get("verId") or package_name or "").strip()
            if not name or not (app_id or package_name):
                continue
            downloads_text = str(item.get("dlDesc") or item.get("downloadDesc") or item.get("downloads") or "")
            download_url = str(item.get("url") or "").strip()
            detail_url = _oppo_detail_url(package_name or app_id)
            apps.append(
                AppMarketListing(
                    store=self.name,
                    app_id=app_id,
                    name=name,
                    package_name=package_name,
                    category=str(item.get("catName") or item.get("category") or ""),
                    url=detail_url or download_url or search_url,
                    rating=_optional_float(item.get("grade") or item.get("rating")),
                    rating_count=_optional_int(item.get("gradeCount") or item.get("point") or item.get("ratingCount")),
                    downloads=_parse_download_count(downloads_text) or _optional_int(item.get("dlCount")),
                    downloads_text=downloads_text,
                    raw_metrics={
                        "download_metric": "public_oppo_download_desc",
                        "download_url": download_url,
                        "version_id": item.get("verId"),
                        "size": item.get("sizeDesc"),
                    },
                )
            )
        return apps

    def _parse_oppo_html_apps(self, text: str, *, search_url: str) -> list[AppMarketListing]:
        apps: list[AppMarketListing] = []
        for item in re.findall(r"<li\b[^>]*\bpkg=[\"'][^\"']+[\"'][^>]*>.*?</li>", text, flags=re.S | re.I):
            pkg = _first_regex(item, r"\bpkg=[\"']([^\"']+)[\"']")
            name = _clean_html_text(_first_regex(item, r"<h3[^>]*>(.*?)</h3>"))
            if not pkg or not name:
                continue
            downloads_text = _clean_html_text(_first_regex(item, r"<p[^>]*class=[\"']describe[\"'][^>]*>(.*?)</p>"))
            apps.append(
                AppMarketListing(
                    store=self.name,
                    app_id=pkg,
                    name=name,
                    package_name=pkg,
                    url=_oppo_detail_url(pkg) or search_url,
                    rating=_optional_float(_first_regex(item, r"\bgrade=[\"']([^\"']+)[\"']")),
                    downloads=_parse_download_count(downloads_text),
                    downloads_text=downloads_text,
                    raw_metrics={"download_metric": "public_oppo_download_desc"},
                )
            )
        return apps


class XiaomiAppStoreProvider(PublicHtmlAppStoreProvider):
    name = "xiaomi_app_store"
    display_name = "小米应用商店"
    base_url = "https://app.mi.com"
    search_url_templates = (
        "https://app.mi.com/suggestionApi?keywords={query_plus}",
        "https://app.mi.com/details?id={query}",
        "https://app.mi.com/search?keywords={query_plus}",
        "https://app.mi.com/search?word={query_plus}",
    )
    detail_url_patterns = (r"app\.mi\.com/details", r"app\.mi\.com/detail")

    def _parse_search_page(
        self,
        text: str,
        *,
        query: str,
        search_url: str,
        limit: int,
    ) -> list[AppMarketListing]:
        apps = self._parse_xiaomi_suggestions(text, search_url=search_url)
        apps.extend(self._parse_xiaomi_detail_page(text, search_url=search_url))
        apps.extend(self._parse_xiaomi_embedded_apps(text, search_url=search_url))
        if apps:
            unique_apps = _dedupe_listings(apps)
            filtered = [app for app in unique_apps if _listing_matches_query(app, query)]
            return [_with_rank(app, index) for index, app in enumerate(filtered[:limit], start=1)]
        return super()._parse_search_page(text, query=query, search_url=search_url, limit=limit)

    def _parse_xiaomi_suggestions(self, text: str, *, search_url: str) -> list[AppMarketListing]:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return []
        suggestions = data.get("suggestion") if isinstance(data, dict) else None
        if not isinstance(suggestions, list):
            return []
        apps: list[AppMarketListing] = []
        for name in suggestions:
            clean_name = str(name or "").strip()
            if not clean_name:
                continue
            apps.append(
                AppMarketListing(
                    store=self.name,
                    app_id=clean_name,
                    name=clean_name,
                    url=f"https://app.mi.com/search?keywords={quote_plus(clean_name)}",
                    raw_metrics={"download_metric": "public_xiaomi_search_suggestion"},
                )
            )
        return apps

    def _parse_xiaomi_detail_page(self, text: str, *, search_url: str) -> list[AppMarketListing]:
        if "app.mi.com/details" not in search_url and "appID" not in text and "包名" not in text:
            return []
        name = _clean_html_text(_first_regex(text, r'<div[^>]+class=["\']intro-titles["\'][^>]*>.*?<h3[^>]*>(.*?)</h3>'))
        if not name:
            name = _clean_html_text(_first_regex(text, r"<title>(.*?)-小米应用商店</title>"))
        package_name = _xiaomi_detail_value(text, "包名")
        app_id = _xiaomi_detail_value(text, "appID") or package_name or name
        if not name or not app_id:
            return []
        category = _clean_html_text(_first_regex(text, r"<b>分类：</b>(.*?)<"))
        rating_count = _optional_int(_first_regex(text, r"app-intro-comment[^>]*>\(\s*([0-9,]+)\s*次评分\s*\)"))
        star_class = _first_regex(text, r"star1-hover\s+star1-([0-9]+)")
        rating = None
        if star_class:
            rating = _optional_float(str(float(star_class) / 2))
        return [
            AppMarketListing(
                store=self.name,
                app_id=app_id,
                name=name,
                developer=_xiaomi_detail_value(text, "开发者"),
                package_name=package_name,
                category=category,
                url=search_url,
                rating=rating,
                rating_count=rating_count,
                raw_metrics={
                    "download_metric": "public_xiaomi_detail_page",
                    "size": _xiaomi_detail_value(text, "软件大小"),
                    "version": _xiaomi_detail_value(text, "版本号"),
                    "updated_at": _xiaomi_detail_value(text, "更新时间"),
                },
            )
        ]

    def _parse_xiaomi_embedded_apps(self, text: str, *, search_url: str) -> list[AppMarketListing]:
        apps: list[AppMarketListing] = []
        for variable in ("searchList", "featuredList", "hotList", "cloudList"):
            for item in _json_array_assignment_items(text, variable):
                name = str(item.get("displayName") or item.get("name") or "").strip()
                package_name = str(item.get("packageName") or item.get("pkgName") or "").strip()
                app_id = str(item.get("appId") or package_name or name).strip()
                if not name or not app_id:
                    continue
                rating = _optional_float(item.get("ratingScore"))
                if rating is not None and rating > 5:
                    rating = rating / 2
                apps.append(
                    AppMarketListing(
                        store=self.name,
                        app_id=app_id,
                        name=name,
                        developer=str(item.get("publisherName") or ""),
                        package_name=package_name,
                        category=str(item.get("level2CategoryName") or item.get("level1CategoryName") or ""),
                        url=f"https://app.mi.com/details?id={quote(package_name or app_id)}",
                        rating=rating,
                        rating_count=_optional_int(item.get("ratingTotalCount")),
                        raw_metrics={
                            "download_metric": "public_xiaomi_embedded_page",
                            "size": item.get("apkSize"),
                        },
                    )
                )
        return apps


class VivoAppStoreProvider(PublicHtmlAppStoreProvider):
    name = "vivo_app_store"
    display_name = "vivo 应用商店"
    base_url = "https://info.appstore.vivo.com.cn"
    search_url_templates = (
        "https://info.appstore.vivo.com.cn/search?keyword={query_plus}",
        "https://info.appstore.vivo.com.cn/search?word={query_plus}",
        "https://appstore.vivo.com.cn/search?keyword={query_plus}",
    )
    detail_url_patterns = (r"appstore\.vivo\.com\.cn/.*/detail", r"info\.appstore\.vivo\.com\.cn/.*/detail")


class HuaweiAppGalleryProvider(PublicHtmlAppStoreProvider):
    name = "huawei_appgallery"
    display_name = "华为 AppGallery"
    base_url = "https://appgallery.huawei.com"
    search_url_templates = (
        "https://wap1.hispace.hicloud.com/uowap/index?method=internal.completeSearchWord&serviceType=20&keyword={query}&zone=CN&locale=zh_CN&maxResults=25&reqPageNum=1&ver=1.1",
        "https://appgallery.huawei.com/search/{query}?locale=zh_CN",
        "https://appgallery.huawei.com/#/search/{query}",
    )
    detail_url_patterns = (r"appgallery\.huawei\.com/.*/app/", r"appgallery\.huawei\.com/app/")

    def search(self, query: str, *, limit: int = 10) -> list[AppMarketListing]:
        self.last_status = "no_match"
        self.last_status_message = "未解析到匹配结果"
        errors: list[str] = []
        saw_spa_shell = False
        for template in self.search_url_templates:
            url = self._format_url(template, query)
            try:
                text = _get_text(url, self.timeout_seconds)
            except Exception as exc:
                errors.append(str(exc))
                continue
            saw_spa_shell = saw_spa_shell or _looks_like_huawei_spa_shell(text)
            apps = self._parse_search_page(text, query=query, search_url=url, limit=limit)
            if apps:
                self.last_status = "ok"
                self.last_status_message = ""
                return apps
        if errors and all(_is_public_page_not_found(error) for error in errors):
            self.last_status = "skipped"
            self.last_status_message = "公开搜索入口不可用，已跳过"
            return []
        if errors:
            raise RuntimeError("; ".join(errors[:2]))
        if saw_spa_shell:
            self.last_status = "skipped"
            self.last_status_message = "公开页面为 SPA 壳，静态请求未包含搜索结果"
        return []

    def _parse_search_page(
        self,
        text: str,
        *,
        query: str,
        search_url: str,
        limit: int,
    ) -> list[AppMarketListing]:
        apps = self._parse_huawei_json_apps(text, search_url=search_url)
        if apps:
            unique_apps = _dedupe_listings(apps)
            filtered = [app for app in unique_apps if _listing_matches_query(app, query)]
            return [_with_rank(app, index) for index, app in enumerate(filtered[:limit], start=1)]
        return super()._parse_search_page(text, query=query, search_url=search_url, limit=limit)

    def _parse_huawei_json_apps(self, text: str, *, search_url: str) -> list[AppMarketListing]:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return []

        apps: list[AppMarketListing] = []
        for item in _iter_json_objects(data):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("appName") or item.get("displayName") or "").strip()
            package_name = str(item.get("package") or item.get("packageName") or item.get("pkgName") or "").strip()
            detail_id = str(item.get("detailId") or item.get("appId") or item.get("appid") or item.get("ID") or "").strip()
            app_id = detail_id or package_name or name
            if not name or not app_id:
                continue
            downloads_text = str(
                item.get("downCountDesc")
                or item.get("downloadCountDesc")
                or item.get("installCount")
                or item.get("downloadsText")
                or ""
            )
            detail_url = f"https://appgallery.huawei.com/app/{quote(detail_id)}" if detail_id else search_url
            apps.append(
                AppMarketListing(
                    store=self.name,
                    app_id=app_id,
                    name=name,
                    developer=str(item.get("developer") or item.get("developerName") or item.get("publisherName") or ""),
                    package_name=package_name,
                    category=str(item.get("tagName") or item.get("categoryName") or item.get("category") or ""),
                    url=detail_url,
                    rating=_optional_float(item.get("score") or item.get("rating") or item.get("stars")),
                    rating_count=_optional_int(item.get("commentCount") or item.get("scoreCount") or item.get("ratingCount")),
                    downloads=_parse_download_count(downloads_text),
                    downloads_text=downloads_text,
                    raw_metrics={
                        "download_metric": "public_huawei_appgallery_json",
                        "intro": item.get("intro") or item.get("memo"),
                    },
                )
            )
        if apps:
            return apps

        suggestions = data.get("list") if isinstance(data, dict) else None
        if not isinstance(suggestions, list):
            return []
        for value in suggestions:
            name = str(value or "").strip()
            if not name:
                continue
            apps.append(
                AppMarketListing(
                    store=self.name,
                    app_id=name,
                    name=name,
                    url=f"https://appgallery.huawei.com/search/{quote(name)}?locale=zh_CN",
                    raw_metrics={"download_metric": "public_huawei_search_suggestion"},
                )
            )
        return apps


class HonorAppMarketProvider(PublicHtmlAppStoreProvider):
    name = "honor_app_market"
    display_name = "荣耀应用市场"
    base_url = "https://www.honor.com"
    search_url_templates = (
        "https://www.honor.com/cn/search/?keyword={query_plus}",
        "https://www.honor.com/cn/app-market/search/?keyword={query_plus}",
    )
    detail_url_patterns = (r"honor\.com/.*/app", r"honor\.com/.*/app-market")


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
        "store_statuses": list(result_dict.get("store_statuses") or []),
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


def _is_public_page_not_found(error: str) -> bool:
    return "HTTP Error 404" in str(error)


def _listing_sort_key(app: AppMarketListing) -> tuple[int, int, int]:
    rating_count = app.rating_count if app.rating_count is not None else -1
    rank = app.rank if app.rank is not None else 9999
    return (rank, -rating_count, 0 if app.downloads is None else -app.downloads)


def _provider_status(provider: AppStoreProvider, apps: list[AppMarketListing]) -> JsonDict:
    status = str(getattr(provider, "last_status", "") or "")
    message = str(getattr(provider, "last_status_message", "") or "")
    if apps:
        status = "ok"
        message = ""
    elif not status:
        status = "no_match"
        message = "未解析到匹配结果"
    return {
        "store": provider.name,
        "status": status,
        "result_count": len(apps),
        "message": message,
    }


def _short_error(exc: Any) -> str:
    text = str(exc or "").strip()
    lowered = text.lower()
    if "timed out" in lowered or "timeout" in lowered:
        return "超时"
    if _is_public_page_not_found(text):
        return "公开搜索入口不可用，已跳过"
    return text


def _normalize_search_query(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"[\s：:，,。！？?、；;“”\"'`~!@#$%^&*()\[\]{}<>|\\/]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not re.search(r"[\w\u4e00-\u9fff]", text):
        return ""
    return text


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


def _dedupe_listings(apps: list[AppMarketListing]) -> list[AppMarketListing]:
    seen: set[tuple[str, str]] = set()
    result: list[AppMarketListing] = []
    for app in apps:
        key = (app.store, (app.package_name or app.app_id or app.url or app.name).lower())
        if key in seen:
            continue
        seen.add(key)
        result.append(app)
    return result


def _with_rank(app: AppMarketListing, rank: int) -> AppMarketListing:
    return AppMarketListing(
        store=app.store,
        app_id=app.app_id,
        name=app.name,
        developer=app.developer,
        package_name=app.package_name,
        category=app.category,
        url=app.url,
        rating=app.rating,
        rating_count=app.rating_count,
        downloads=app.downloads,
        downloads_text=app.downloads_text,
        rank=rank,
        raw_metrics=app.raw_metrics,
    )


def _listing_matches_query(app: AppMarketListing, query: str) -> bool:
    compact_query = re.sub(r"\s+", "", query or "").lower()
    if not compact_query:
        return True
    haystack = re.sub(
        r"\s+",
        "",
        f"{app.name}{app.developer}{app.category}{app.package_name}",
    ).lower()
    if compact_query in haystack:
        return True
    chinese_chars = [char for char in compact_query if "\u4e00" <= char <= "\u9fff"]
    if not chinese_chars:
        return False
    chinese_query = "".join(chinese_chars)
    if len(chinese_query) <= 2:
        return all(char in haystack for char in chinese_query)
    bigrams = [chinese_query[index : index + 2] for index in range(len(chinese_query) - 1)]
    matched = sum(1 for bigram in bigrams if bigram in haystack)
    required = 2 if len(bigrams) >= 3 else 1
    return matched >= required


def _iter_json_objects(value: Any):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _iter_json_objects(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_json_objects(item)


def _clean_html_text(value: str) -> str:
    text = re.sub(r"<script\b.*?</script>", "", value or "", flags=re.S | re.I)
    text = re.sub(r"<style\b.*?</style>", "", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _text_around(text: str, needle: str, *, radius: int = 400) -> str:
    index = text.find(needle)
    if index < 0:
        return ""
    start = max(index - radius, 0)
    end = min(index + len(needle) + radius, len(text))
    return _clean_html_text(text[start:end])


def _first_rating_text(text: str) -> str:
    patterns = [
        r"(?:评分|rating|score)[：:\s]*([0-9.]+)",
        r"([0-9.]+)\s*(?:分|星)",
    ]
    for pattern in patterns:
        value = _first_regex(text, pattern)
        if value:
            return value
    return ""


def _app_id_from_url(url: str) -> str:
    decoded = html.unescape(url or "")
    patterns = [
        r"[?&]id=([^&#]+)",
        r"[?&]appId=([^&#]+)",
        r"[?&]pkg=([^&#]+)",
        r"[?&]packageName=([^&#]+)",
        r"/app/([^/?#]+)",
        r"/details/([^/?#]+)",
        r"/detail/([^/?#]+)",
    ]
    for pattern in patterns:
        value = _first_regex(decoded, pattern)
        if value:
            return value
    return ""


def _xiaomi_detail_value(text: str, label: str) -> str:
    pattern = (
        r"<div[^>]*>\s*"
        + re.escape(label)
        + r"\s*</div>\s*<div[^>]*>\s*(.*?)\s*</div>"
    )
    return _clean_html_text(_first_regex(text, pattern))


def _json_array_assignment_items(text: str, variable: str) -> list[JsonDict]:
    match = re.search(r"\b(?:let|var|const)\s+" + re.escape(variable) + r"\s*=\s*(\[.*?\]);", text, flags=re.S)
    if not match:
        return []
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []
    return [item for item in data if isinstance(item, dict)]


def _looks_like_huawei_spa_shell(text: str) -> bool:
    lowered = (text or "").lower()
    return (
        '<div id="app"></div>' in lowered
        and "static/agweb" in lowered
    )


def _oppo_detail_url(package_name: str) -> str:
    package_name = str(package_name or "").strip()
    if not package_name:
        return ""
    params = urlencode(
        {
            "gb": "1",
            "params": (
                "enter_id=15"
                f"&out_package_name={package_name}"
                "&out_start_download=false"
                "&enter_params=out_operator"
            ),
        }
    )
    return f"softmarket://market_appdetail?pn=com.oppo.market&{params}"


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
        r'"downloadCount":"?([^",}]+)"?',
        r'"downloads":"?([^",}]+)"?',
        r"(下载[：:\s]*[0-9,.]+\s*(?:万|亿)?)",
        r"([0-9,.]+\s*(?:万|亿)?\s*(?:次)?下载)",
        r"([0-9,.]+\s*(?:万|亿))",
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
    if "亿" in upper:
        multiplier = 100_000_000
    elif "万" in upper:
        multiplier = 10_000
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
