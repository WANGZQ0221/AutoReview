import unittest
from unittest.mock import patch

from autoreview.market.research import (
    AppMarketSearcher,
    OppoAppMarketProvider,
    PublicHtmlAppStoreProvider,
    _listing_matches_query,
    _parse_download_count,
)
from autoreview.market import AppMarketListing


class FakeDomesticProvider(PublicHtmlAppStoreProvider):
    name = "fake_domestic"
    search_url_templates = ("https://store.example/search?q={query_plus}",)
    detail_url_patterns = (r"store\.example/app",)


class TimeoutProvider(PublicHtmlAppStoreProvider):
    name = "timeout_store"
    search_url_templates = ("https://timeout.example/search?q={query_plus}",)

    def search(self, query, *, limit=10):
        raise TimeoutError("timed out")


class MarketResearchTest(unittest.TestCase):
    def test_default_searcher_includes_domestic_android_stores(self):
        searcher = AppMarketSearcher()
        names = {provider.name for provider in searcher.providers}

        self.assertIn("oppo_app_market", names)
        self.assertIn("xiaomi_app_store", names)
        self.assertIn("vivo_app_store", names)
        self.assertIn("huawei_appgallery", names)
        self.assertIn("honor_app_market", names)

    def test_searcher_rejects_punctuation_only_query(self):
        searcher = AppMarketSearcher(providers=[FakeDomesticProvider()])

        result = searcher.search_competitors("。")

        self.assertEqual(result.query, "")
        self.assertEqual(result.apps, [])
        self.assertIn("empty query", result.errors)

    def test_searcher_does_not_report_public_404_as_user_error(self):
        searcher = AppMarketSearcher(providers=[FakeDomesticProvider()])

        with patch("autoreview.market.research._get_text", side_effect=RuntimeError("HTTP Error 404: Not Found")):
            result = searcher.search_competitors("王者荣耀")

        self.assertEqual(result.apps, [])
        self.assertEqual(result.errors, [])
        self.assertEqual(result.store_statuses[0]["status"], "skipped")

    def test_searcher_records_failed_store_status(self):
        searcher = AppMarketSearcher(providers=[TimeoutProvider()])

        result = searcher.search_competitors("王者荣耀")

        self.assertEqual(result.store_statuses[0]["store"], "timeout_store")
        self.assertEqual(result.store_statuses[0]["status"], "failed")
        self.assertEqual(result.store_statuses[0]["message"], "超时")

    def test_public_html_provider_parses_json_ld_application(self):
        provider = FakeDomesticProvider()
        html = """
        <script type="application/ld+json">
        {
          "@type": "SoftwareApplication",
          "name": "英语四级单词",
          "url": "/app/com.example.words",
          "author": "Example Studio",
          "applicationCategory": "教育",
          "aggregateRating": {"ratingValue": "4.8", "ratingCount": "1234"},
          "downloadCount": "123万次下载"
        }
        </script>
        """

        apps = provider._parse_search_page(
            html,
            query="英语四级",
            search_url="https://store.example/search?q=英语四级",
            limit=5,
        )

        self.assertEqual(len(apps), 1)
        self.assertEqual(apps[0].store, "fake_domestic")
        self.assertEqual(apps[0].name, "英语四级单词")
        self.assertEqual(apps[0].developer, "Example Studio")
        self.assertEqual(apps[0].rating, 4.8)
        self.assertEqual(apps[0].rating_count, 1234)
        self.assertEqual(apps[0].downloads, 1230000)

    def test_public_html_provider_parses_detail_links(self):
        provider = FakeDomesticProvider()
        html = """
        <div class="app">
          <a href="/app/com.example.words">英语四级单词</a>
          <span>评分 4.6</span>
          <span>下载 56万</span>
        </div>
        """

        apps = provider._parse_search_page(
            html,
            query="四级单词",
            search_url="https://store.example/search?q=四级单词",
            limit=5,
        )

        self.assertEqual(len(apps), 1)
        self.assertEqual(apps[0].app_id, "com.example.words")
        self.assertEqual(apps[0].downloads, 560000)

    def test_public_html_provider_does_not_return_unmatched_generic_results(self):
        provider = FakeDomesticProvider()
        html = """
        <a href="/app/com.example.mall">荣耀商城APP</a>
        <a href="/app/com.example.jobs">青团社兼职</a>
        """

        apps = provider._parse_search_page(
            html,
            query="英语四级",
            search_url="https://store.example/search?q=英语四级",
            limit=5,
        )

        self.assertEqual(apps, [])

    def test_chinese_query_matching_requires_stronger_phrase_match(self):
        self.assertEqual(
            _listing_matches_query(AppMarketListing("honor_app_market", "1", "荣耀商城APP"), "王者荣耀"),
            False,
        )
        self.assertEqual(
            _listing_matches_query(AppMarketListing("xiaomi_app_store", "2", "王者荣耀-S43赛季陌上相逢"), "王者荣耀"),
            True,
        )

    def test_chinese_download_count_parser(self):
        self.assertEqual(_parse_download_count("123万次下载"), 1230000)
        self.assertEqual(_parse_download_count("1.5亿下载"), 150000000)

    def test_oppo_provider_has_search_templates(self):
        provider = OppoAppMarketProvider()

        self.assertEqual(provider.name, "oppo_app_market")
        self.assertTrue(provider.search_url_templates)


if __name__ == "__main__":
    unittest.main()
