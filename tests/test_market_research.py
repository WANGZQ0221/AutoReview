import unittest
from unittest.mock import patch

from autoreview.market.research import (
    AppMarketSearcher,
    HuaweiAppGalleryProvider,
    OppoAppMarketProvider,
    PublicHtmlAppStoreProvider,
    XiaomiAppStoreProvider,
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

    def test_oppo_provider_parses_public_json_listing(self):
        provider = OppoAppMarketProvider()
        text = """
        {
          "errno": 0,
          "data": {
            "cards": [
              {
                "apps": [
                  {
                    "appId": 4169,
                    "verId": 22926538,
                    "appName": "微信",
                    "pkgName": "com.tencent.mm",
                    "url": "https://istore.oppomobile.com/download/v1/22926538",
                    "dlCount": 2147483647,
                    "dlDesc": "173.4 亿次",
                    "grade": 2.93813,
                    "gradeCount": 509095,
                    "sizeDesc": "238 MB"
                  }
                ]
              }
            ]
          }
        }
        """

        apps = provider._parse_search_page(
            text,
            query="微信",
            search_url="https://app.cdo.oppomobile.com/home/store/index.json?start=0&size=20",
            limit=5,
        )

        self.assertEqual(len(apps), 1)
        self.assertEqual(apps[0].store, "oppo_app_market")
        self.assertEqual(apps[0].name, "微信")
        self.assertEqual(apps[0].package_name, "com.tencent.mm")
        self.assertEqual(apps[0].downloads_text, "173.4 亿次")
        self.assertEqual(apps[0].downloads, 17340000000)
        self.assertEqual(apps[0].rating_count, 509095)
        self.assertIn("softmarket://market_appdetail", apps[0].url)
        self.assertEqual(
            apps[0].raw_metrics["download_url"],
            "https://istore.oppomobile.com/download/v1/22926538",
        )

    def test_oppo_provider_parses_current_public_html_listing(self):
        provider = OppoAppMarketProvider()
        text = """
        <li pkg="com.tencent.mm" card="0" version="22926538">
          <div class="info">
            <h3>微信</h3>
            <p class="size-wrap">
              <span class="star-wrap"><span grade="2.93813" class="star"></span></span>
              <span class="size">238 MB</span>
            </p>
            <p class="describe">173.4 亿次</p>
          </div>
        </li>
        """

        apps = provider._parse_search_page(
            text,
            query="微信",
            search_url="https://app.cdo.oppomobile.com/home/store?module=2",
            limit=5,
        )

        self.assertEqual(len(apps), 1)
        self.assertEqual(apps[0].package_name, "com.tencent.mm")
        self.assertEqual(apps[0].rating, 2.93813)
        self.assertEqual(apps[0].downloads, 17340000000)

    def test_xiaomi_provider_parses_public_suggestions(self):
        provider = XiaomiAppStoreProvider()
        text = '{"suggestion":["微信","微信读书","微信输入法"]}'

        apps = provider._parse_search_page(
            text,
            query="微信",
            search_url="https://app.mi.com/suggestionApi?keywords=微信",
            limit=5,
        )

        self.assertEqual(len(apps), 3)
        self.assertEqual(apps[0].store, "xiaomi_app_store")
        self.assertEqual(apps[0].name, "微信")
        self.assertEqual(apps[0].raw_metrics["download_metric"], "public_xiaomi_search_suggestion")

    def test_xiaomi_provider_parses_public_detail_page(self):
        provider = XiaomiAppStoreProvider()
        text = """
        <title>微信-小米应用商店</title>
        <div class="intro-titles">
          <h3 style="margin-top: 18px">微信</h3>
          <p class="special-font action"><b>分类：</b>聊天社交<span>|</span></p>
          <div class="star1-empty"><div class="star1-hover star1-8"></div></div>
          <span class="app-intro-comment">( 130926次评分 )</span>
        </div>
        <div><div>软件大小</div><div>249.05M</div></div>
        <div><div>appID</div><div>1122</div></div>
        <div><div>版本号</div><div>8.0.74</div></div>
        <div><div>开发者</div><div>广州腾讯科技有限公司</div></div>
        <div><div>更新时间</div><div>2026-06-10</div></div>
        <div><div>包名</div><div><div class="line-break">com.tencent.mm</div></div></div>
        """

        apps = provider._parse_search_page(
            text,
            query="微信",
            search_url="https://app.mi.com/details?id=com.tencent.mm",
            limit=5,
        )

        self.assertEqual(len(apps), 1)
        self.assertEqual(apps[0].app_id, "1122")
        self.assertEqual(apps[0].package_name, "com.tencent.mm")
        self.assertEqual(apps[0].developer, "广州腾讯科技有限公司")
        self.assertEqual(apps[0].category, "聊天社交")
        self.assertEqual(apps[0].rating, 4.0)
        self.assertEqual(apps[0].rating_count, 130926)
        self.assertEqual(apps[0].raw_metrics["download_metric"], "public_xiaomi_detail_page")

    def test_xiaomi_provider_parses_embedded_public_app_lists(self):
        provider = XiaomiAppStoreProvider()
        text = """
        <script>
        let searchList = [{"appId":1122,"displayName":"微信","packageName":"com.tencent.mm",
          "publisherName":"广州腾讯科技有限公司","ratingScore":8.0,"ratingTotalCount":130926,
          "level2CategoryName":"聊天社交","apkSize":255027200}];
        </script>
        """

        apps = provider._parse_search_page(
            text,
            query="微信",
            search_url="https://app.mi.com/search?keywords=微信&token=public",
            limit=5,
        )

        self.assertEqual(len(apps), 1)
        self.assertEqual(apps[0].name, "微信")
        self.assertEqual(apps[0].package_name, "com.tencent.mm")
        self.assertEqual(apps[0].rating, 4.0)
        self.assertEqual(apps[0].rating_count, 130926)

    def test_huawei_provider_parses_appgallery_json_listing(self):
        provider = HuaweiAppGalleryProvider()
        text = """
        {
          "appList": [
            {
              "detailId": "C100123",
              "name": "微信",
              "package": "com.tencent.mm",
              "developer": "腾讯科技（深圳）有限公司",
              "tagName": "社交通讯",
              "score": "4.5",
              "scoreCount": "12345",
              "downCountDesc": "100亿次安装"
            }
          ],
          "list": ["微信读书"]
        }
        """

        apps = provider._parse_search_page(
            text,
            query="微信",
            search_url="https://wap1.hispace.hicloud.com/uowap/index?keyword=微信",
            limit=5,
        )

        self.assertEqual(len(apps), 1)
        self.assertEqual(apps[0].store, "huawei_appgallery")
        self.assertEqual(apps[0].app_id, "C100123")
        self.assertEqual(apps[0].package_name, "com.tencent.mm")
        self.assertEqual(apps[0].developer, "腾讯科技（深圳）有限公司")
        self.assertEqual(apps[0].downloads, 10000000000)
        self.assertEqual(apps[0].raw_metrics["download_metric"], "public_huawei_appgallery_json")

    def test_huawei_provider_marks_spa_shell_as_skipped(self):
        provider = HuaweiAppGalleryProvider()
        shell = """
        <html><head><script src="/static/agweb/env.js"></script></head>
        <body><div id="app"></div><script src="appgallery.js"></script></body></html>
        """

        with patch("autoreview.market.research._get_text", return_value=shell):
            apps = provider.search("微信", limit=5)

        self.assertEqual(apps, [])
        self.assertEqual(provider.last_status, "skipped")
        self.assertIn("SPA", provider.last_status_message)


if __name__ == "__main__":
    unittest.main()
