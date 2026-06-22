from pathlib import Path
import json
import tempfile
import time
import unittest
from unittest.mock import patch

from autoreview.agent import ReviewAgent
from autoreview.agent.state import JsonStateStore
from autoreview.market import AppMarketListing, AppMarketSearchResult
from autoreview.feishu.events import extract_message_event
from autoreview.feishu.config import FeishuConfig
from autoreview.feishu.image_analysis import ImageAnalysisClient
from autoreview.feishu.server import FeishuWebhookApp, _looks_like_oppo_rejection


class FakeOppoWorkflowAgent:
    def __init__(self):
        self.status_version_code = None

    def status(self, version_code=None):
        self.status_version_code = version_code
        return {
            "pkg_name": "com.example.app",
            "version_code": str(version_code or "100"),
            "task": {"task_state": "2"},
            "app_info": {
                "audit_status_name": "审核不通过",
                "refuse_reason": "资质缺失",
            },
            "review_state": "rejected",
        }

    def validate(self):
        return {
            "valid": False,
            "missing_required_fields": ["apk_url"],
            "missing_files": ["release/app.apk"],
        }


class FakeMarketSearcher:
    def search_competitors(self, query, limit=8):
        return AppMarketSearchResult(
            query=query,
            apps=[
                AppMarketListing(
                    store="google_play",
                    app_id="com.example.words",
                    name="四级单词竞品",
                    developer="Example Studio",
                    category="Education",
                    rating=4.7,
                    rating_count=1234,
                    downloads=1000000,
                    downloads_text="1,000,000+",
                ),
                AppMarketListing(
                    store="apple_app_store",
                    app_id="123456",
                    name="英语单词竞品",
                    developer="Apple Example",
                    category="Education",
                    rating=4.5,
                    rating_count=321,
                ),
            ],
        )


class FakeVariantMarketSearcher:
    def __init__(self):
        self.calls = []

    def search_competitors(self, query, limit=8, stores=None):
        self.calls.append({"query": query, "limit": limit, "stores": stores})
        return AppMarketSearchResult(
            query=query,
            apps=[
                AppMarketListing(
                    store="oppo_app_market",
                    app_id="a",
                    name="抖音极速版",
                    downloads=13760000000,
                    downloads_text="137.6 亿次",
                ),
                AppMarketListing(
                    store="oppo_app_market",
                    app_id="b",
                    name="抖音火山版",
                    downloads=7980000000,
                    downloads_text="79.8 亿次",
                ),
            ],
            store_statuses=[{"store": "oppo_app_market", "status": "ok", "result_count": 2}],
        )


class FakePackagingAgent:
    def __init__(self):
        self.package_one_calls = []
        self.package_batch_calls = []

    def package_one(self, *, app_name="", pkg_name="", channels=None, dry_run=False):
        self.package_one_calls.append(
            {"app_name": app_name, "pkg_name": pkg_name, "channels": channels or [], "dry_run": dry_run}
        )
        return {
            "project_dir": "D:/project",
            "channels": ["xm1067"],
            "packconfig": "xm1067",
            "resolved_package": {
                "app_name": app_name or "八年级语文下册",
                "pkg_name": pkg_name,
                "channel": "xm1067",
                "version_code": "68",
                "version_name": "3.1067.38.2",
            },
            "latest_apks": ["D:/project/apk/out.apk"],
        }

    def package_batch(self, *, dry_run=False, continue_on_error=True):
        self.package_batch_calls.append({"dry_run": dry_run, "continue_on_error": continue_on_error})
        return [{"ok": True, "name": "job-1", "channels": ["xm1067"]}]


class FakeLlmClient:
    def __init__(self, decision=None, *, tool_call=None, tool_summary=""):
        self.decision = decision
        self.tool_call = tool_call
        self.tool_summary = tool_summary
        self.calls = []
        self.tool_calls = []
        self.summary_calls = []

    def interpret(self, message, session):
        self.calls.append((message, session))
        return dict(self.decision or {})

    def choose_tool(self, message, session, tools):
        self.tool_calls.append((message, session, tools))
        return dict(self.tool_call or {"tool": "none", "arguments": {}, "confidence": 0.0})

    def summarize_tool_result(self, message, session, tool_call, tool_result):
        self.summary_calls.append((message, session, tool_call, tool_result))
        return self.tool_summary


class FakeFeishuClient:
    def __init__(self):
        self.resource_calls = []
        self.replies = []

    def get_message_resource(self, message_id, file_key, resource_type="image"):
        self.resource_calls.append((message_id, file_key, resource_type))
        return b"uploaded-bytes", "application/vnd.android.package-archive"

    def reply_text(self, message_id, text):
        self.replies.append((message_id, text))
        return {"code": 0}


class ReviewAgentTest(unittest.TestCase):
    def test_analyze_rejection_message_updates_session(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            agent = ReviewAgent(JsonStateStore(Path(temp_dir) / "state.json"))
            response = agent.handle_message(
                "chat-1",
                "分析驳回：APK相似度0.92，请勿重复提交，请补充ICP备案网站",
                "user-1",
            )

            self.assertIn("不建议原包直接重提", response.text)
            self.assertEqual(response.data["similarity_score"], 0.92)
            status = agent.handle_message("chat-1", "状态").text
            self.assertIn("是否建议同包重提：否", status)

    def test_extract_feishu_message_event_v2(self):
        payload = {
            "schema": "2.0",
            "header": {"event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_1"}},
                "message": {
                    "message_id": "om_1",
                    "chat_id": "oc_1",
                    "content": "{\"text\":\"帮助\"}",
                },
            },
        }

        event = extract_message_event(payload)

        self.assertEqual(event["message_id"], "om_1")
        self.assertEqual(event["chat_id"], "oc_1")
        self.assertEqual(event["sender_id"], "ou_1")
        self.assertEqual(event["text"], "帮助")

    def test_extract_feishu_image_event_v2(self):
        payload = {
            "schema": "2.0",
            "header": {"event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_1"}},
                "message": {
                    "message_id": "om_1",
                    "chat_id": "oc_1",
                    "message_type": "image",
                    "content": "{\"image_key\":\"img_1\"}",
                },
            },
        }

        event = extract_message_event(payload)

        self.assertEqual(event["message_type"], "image")
        self.assertEqual(event["image_key"], "img_1")
        self.assertEqual(event["text"], "")

    def test_extract_feishu_file_event_v2(self):
        payload = {
            "schema": "2.0",
            "header": {"event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_1"}},
                "message": {
                    "message_id": "om_1",
                    "chat_id": "oc_1",
                    "message_type": "file",
                    "content": "{\"file_key\":\"file_1\",\"file_name\":\"app-release.apk\"}",
                },
            },
        }

        event = extract_message_event(payload)

        self.assertEqual(event["message_type"], "file")
        self.assertEqual(event["file_key"], "file_1")
        self.assertEqual(event["file_name"], "app-release.apk")
        self.assertEqual(event["image_key"], "")

    def test_status_includes_last_image_analysis(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            agent = ReviewAgent(JsonStateStore(Path(temp_dir) / "state.json"))
            agent.state_store.update_session(
                "chat-1",
                {
                    "last_image_analysis": {
                        "summary": "- image2：成功，文本：测试题目",
                    }
                },
            )

            status = agent.handle_message("chat-1", "状态").text

            self.assertIn("最近图片识别", status)
            self.assertIn("测试题目", status)

    def test_ocr_multipart_body_contains_image_name_and_file(self):
        body = ImageAnalysisClient._build_multipart_body(
            "boundary",
            b"image-bytes",
            "feishu.jpg",
        )

        self.assertIn(b'name="image_name"', body)
        self.assertIn(b"feishu.jpg", body)
        self.assertIn(b'name="file"; filename="feishu.jpg"', body)
        self.assertIn(b"image-bytes", body)

    def test_analyze_last_image_uses_ocr_text(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            agent = ReviewAgent(JsonStateStore(Path(temp_dir) / "state.json"))
            agent.state_store.update_session(
                "chat-1",
                {
                    "last_image_analysis": {
                        "ocr_text": "APK相似度0.92，请勿重复提交，请补充ICP备案网站",
                    }
                },
            )

            response = agent.handle_message("chat-1", "分析这张图", "user-1")

            self.assertIn("不建议原包直接重提", response.text)
            self.assertEqual(response.data["similarity_score"], 0.92)

    def test_detects_oppo_rejection_text(self):
        self.assertEqual(_looks_like_oppo_rejection("请勿重复提交，APK相似度0.92"), True)
        self.assertEqual(_looks_like_oppo_rejection("启动后日志位置是 logs"), False)

    def test_query_oppo_status_uses_agent(self):
        fake_agent = FakeOppoWorkflowAgent()
        agent = ReviewAgent(
            JsonStateStore(Path(tempfile.mkdtemp()) / "state.json"),
            oppo_agent_factory=lambda: fake_agent,
        )

        response = agent.handle_message("chat-1", "查询审核状态：200")

        self.assertEqual(fake_agent.status_version_code, "200")
        self.assertIn("OPPO 审核状态", response.text)
        self.assertIn("审核不通过", response.text)
        self.assertIn("资质缺失", response.text)

    def test_package_message_runs_packaging_agent(self):
        fake_packaging = FakePackagingAgent()
        agent = ReviewAgent(JsonStateStore(Path(tempfile.mkdtemp()) / "state.json"))
        agent.packaging_agent = fake_packaging

        response = agent.handle_message("chat-1", "打包 com.pelbs.book1067 dry-run")

        self.assertIn("打包预演", response.text)
        self.assertEqual(fake_packaging.package_one_calls[0]["pkg_name"], "com.pelbs.book1067")
        self.assertTrue(fake_packaging.package_one_calls[0]["dry_run"])

    def test_batch_package_message_runs_packaging_agent(self):
        fake_packaging = FakePackagingAgent()
        agent = ReviewAgent(JsonStateStore(Path(tempfile.mkdtemp()) / "state.json"))
        agent.packaging_agent = fake_packaging

        response = agent.handle_message("chat-1", "批量打包 dry-run")

        self.assertIn("批量打包预演", response.text)
        self.assertTrue(fake_packaging.package_batch_calls[0]["dry_run"])

    def test_package_message_accepts_app_name(self):
        fake_packaging = FakePackagingAgent()
        agent = ReviewAgent(JsonStateStore(Path(tempfile.mkdtemp()) / "state.json"))
        agent.packaging_agent = fake_packaging

        response = agent.handle_message("chat-1", "打包 八年级语文下册 dry-run")

        self.assertIn("打包预演", response.text)
        self.assertEqual(fake_packaging.package_one_calls[0]["app_name"], "八年级语文下册")

    def test_llm_package_intent_runs_without_rule_phrase(self):
        fake_packaging = FakePackagingAgent()
        llm = FakeLlmClient(
            {
                "intent": "package_apk",
                "confidence": 0.9,
                "app_name": "八年级语文下册",
                "dry_run": True,
            }
        )
        agent = ReviewAgent(JsonStateStore(Path(tempfile.mkdtemp()) / "state.json"), llm_client=llm)
        agent.packaging_agent = fake_packaging

        response = agent.handle_message("chat-1", "把八年级语文下册弄一个测试包")

        self.assertIn("打包预演", response.text)
        self.assertEqual(fake_packaging.package_one_calls[0]["app_name"], "八年级语文下册")
        self.assertTrue(fake_packaging.package_one_calls[0]["dry_run"])

    def test_llm_batch_package_intent_runs(self):
        fake_packaging = FakePackagingAgent()
        llm = FakeLlmClient({"intent": "batch_package", "confidence": 0.9, "dry_run": True})
        agent = ReviewAgent(JsonStateStore(Path(tempfile.mkdtemp()) / "state.json"), llm_client=llm)
        agent.packaging_agent = fake_packaging

        response = agent.handle_message("chat-1", "今天先把这一批都预演一下")

        self.assertIn("批量打包预演", response.text)
        self.assertTrue(fake_packaging.package_batch_calls[0]["dry_run"])

    def test_package_lookup_by_app_name(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            project_dir = base / "android-project"
            (project_dir / "app").mkdir(parents=True)
            (project_dir / "app" / "build.gradle").write_text("android {}", encoding="utf-8")
            (project_dir / "jksconfig.txt").write_text("signing=value", encoding="utf-8")
            (project_dir / "packlist.xls").write_text(
                "\n".join(
                    [
                        "h0",
                        "h1",
                        "h2",
                        "\t四年级英语点读\txm1016\twx\tcom.pelbs.book1016\t\t\t64\t\t\t3.1016.34.2",
                    ]
                ),
                encoding="utf-8",
            )
            config_path = base / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "packaging": {
                            "project_dir": str(project_dir),
                            "script": str(base / "package.js"),
                        }
                    }
                ),
                encoding="utf-8",
            )
            (base / "package.js").write_text("", encoding="utf-8")
            agent = ReviewAgent(JsonStateStore(base / "state.json"), oppo_config_path=config_path)

            response = agent.handle_message("chat-1", "四年级英语点读对应什么包")

            self.assertIn("com.pelbs.book1016", response.text)
            self.assertIn("xm1016", response.text)

    def test_llm_package_lookup_intent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            project_dir = base / "android-project"
            (project_dir / "app").mkdir(parents=True)
            (project_dir / "app" / "build.gradle").write_text("android {}", encoding="utf-8")
            (project_dir / "jksconfig.txt").write_text("signing=value", encoding="utf-8")
            (project_dir / "packlist.xls").write_text(
                "\n".join(
                    [
                        "h0",
                        "h1",
                        "h2",
                        "\t八年级语文下册\txm1067\twx\tcom.pelbs.book1067\t\t\t68\t\t\t3.1067.38.2",
                    ]
                ),
                encoding="utf-8",
            )
            config_path = base / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "packaging": {
                            "project_dir": str(project_dir),
                            "script": str(base / "package.js"),
                        }
                    }
                ),
                encoding="utf-8",
            )
            (base / "package.js").write_text("", encoding="utf-8")
            llm = FakeLlmClient(
                {
                    "intent": "package_lookup",
                    "confidence": 0.9,
                    "app_name": "八年级语文下册",
                }
            )
            agent = ReviewAgent(
                JsonStateStore(base / "state.json"),
                oppo_config_path=config_path,
                llm_client=llm,
            )

            response = agent.handle_message("chat-1", "这个名字对应哪个安装包")

            self.assertIn("com.pelbs.book1067", response.text)
            self.assertIn("xm1067", response.text)

    def test_package_lookup_falls_back_to_packlist_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            config_path = base / "config.json"
            project_dir = base / "android-project"
            project_dir.mkdir()
            snapshot_path = base / "packlist-scan.json"
            snapshot_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "result": [
                            {
                                "sheet": "CfgGameConfig",
                                "row": 56,
                                "channel": "xm1067",
                                "app_name": "八年级语文下册",
                                "pkg_name": "com.pelbs.book1067",
                                "version_code": "68",
                                "version_name": "3.1067.38.2",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            config_path.write_text(
                json.dumps(
                    {
                        "packaging": {
                            "project_dir": str(project_dir),
                            "script": str(base / "package.js"),
                            "packlist_scan_file": str(snapshot_path),
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (base / "package.js").write_text("", encoding="utf-8")
            agent = ReviewAgent(JsonStateStore(base / "state.json"), oppo_config_path=config_path)

            response = agent.handle_message("chat-1", "八年级下册语文对应什么包")

            self.assertIn("应用名：八年级语文下册", response.text)
            self.assertIn("包名：com.pelbs.book1067", response.text)
            self.assertIn("渠道：xm1067", response.text)
            self.assertIn("版本号：68", response.text)
            self.assertIn("版本名：3.1067.38.2", response.text)

    def test_package_lookup_prefers_separate_packaging_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            config_dir = base / "config"
            config_dir.mkdir()
            main_config = config_dir / "oppo_submission.json"
            packaging_config = config_dir / "packaging.json"
            project_dir = base / "android-project"
            project_dir.mkdir()
            snapshot_path = base / "packlist-scan.json"
            snapshot_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "result": [
                            {
                                "sheet": "CfgGameConfig",
                                "row": 56,
                                "channel": "xm1067",
                                "app_name": "八年级语文下册",
                                "pkg_name": "com.pelbs.book1067",
                                "version_code": "68",
                                "version_name": "3.1067.38.2",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            packaging_config.write_text(
                json.dumps(
                    {
                        "packaging": {
                            "project_dir": str(project_dir),
                            "script": str(base / "package.js"),
                            "packlist_scan_file": str(snapshot_path),
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            main_config.write_text(json.dumps({"submission": {"pkg_name": "com.example.app"}}, ensure_ascii=False), encoding="utf-8")
            (base / "package.js").write_text("", encoding="utf-8")

            agent = ReviewAgent(JsonStateStore(base / "state.json"), oppo_config_path=main_config)
            response = agent.handle_message("chat-1", "八年级下册语文对应什么包")

            self.assertIn("com.pelbs.book1067", response.text)
            self.assertEqual(agent.packaging_agent.config_path, packaging_config.resolve())

    def test_packaging_capability_question_does_not_return_global_help(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            config_dir = base / "config"
            config_dir.mkdir()
            main_config = config_dir / "oppo_submission.json"
            packaging_config = config_dir / "packaging.json"
            project_dir = base / "android-project"
            project_dir.mkdir()
            packaging_config.write_text(
                json.dumps(
                    {
                        "packaging": {
                            "project_dir": str(project_dir),
                            "script": str(base / "package.js"),
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            main_config.write_text("{}", encoding="utf-8")
            (base / "package.js").write_text("", encoding="utf-8")
            agent = ReviewAgent(JsonStateStore(base / "state.json"), oppo_config_path=main_config)

            response = agent.handle_message("chat-1", "能打包那些？")

            self.assertIn("支持打包和查包", response.text)
            self.assertIn("打包 八年级语文下册", response.text)
            self.assertNotIn("我可以协助 OPPO 审核提交流程", response.text)
            self.assertEqual(response.data["capability"], "packaging")

    def test_package_lookup_paginates_subject_results(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            config_path = base / "config.json"
            project_dir = base / "android-project"
            project_dir.mkdir()
            snapshot_path = base / "packlist-scan.json"
            entries = []
            for idx in range(12):
                entries.append(
                    {
                        "sheet": "CfgGameConfig",
                        "row": 50 + idx,
                        "channel": f"xm20{idx:02d}",
                        "app_name": f"{idx + 1}年级语文上册",
                        "pkg_name": f"com.pelbs.book20{idx:02d}",
                        "version_code": "68",
                        "version_name": f"3.20{idx:02d}.38.2",
                    }
                )
            snapshot_path.write_text(json.dumps({"ok": True, "result": entries}, ensure_ascii=False), encoding="utf-8")
            config_path.write_text(
                json.dumps(
                    {
                        "packaging": {
                            "project_dir": str(project_dir),
                            "script": str(base / "package.js"),
                            "packlist_scan_file": str(snapshot_path),
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (base / "package.js").write_text("", encoding="utf-8")
            agent = ReviewAgent(JsonStateStore(base / "state.json"), oppo_config_path=config_path)

            response = agent.handle_message("chat-1", "语文都有那些年级的包？")

            self.assertIn("共 12 个", response.text)
            self.assertIn("发送“还有呢”或“下一页”继续", response.text)
            self.assertIn("应用名：1年级语文上册", response.text)
            self.assertIn("应用名：10年级语文上册", response.text)
            self.assertNotIn("应用名：11年级语文上册", response.text)

    def test_package_lookup_followup_returns_next_page(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            config_path = base / "config.json"
            project_dir = base / "android-project"
            project_dir.mkdir()
            snapshot_path = base / "packlist-scan.json"
            entries = []
            for idx in range(12):
                entries.append(
                    {
                        "sheet": "CfgGameConfig",
                        "row": 80 + idx,
                        "channel": f"xm30{idx:02d}",
                        "app_name": f"{idx + 1}年级语文下册",
                        "pkg_name": f"com.pelbs.book30{idx:02d}",
                        "version_code": "68",
                        "version_name": f"3.30{idx:02d}.38.2",
                    }
                )
            snapshot_path.write_text(json.dumps({"ok": True, "result": entries}, ensure_ascii=False), encoding="utf-8")
            config_path.write_text(
                json.dumps(
                    {
                        "packaging": {
                            "project_dir": str(project_dir),
                            "script": str(base / "package.js"),
                            "packlist_scan_file": str(snapshot_path),
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (base / "package.js").write_text("", encoding="utf-8")
            agent = ReviewAgent(JsonStateStore(base / "state.json"), oppo_config_path=config_path)

            agent.handle_message("chat-1", "语文都有那些年级的包？")
            response = agent.handle_message("chat-1", "还有呢？")

            self.assertIn("应用名：11年级语文下册", response.text)
            self.assertIn("应用名：12年级语文下册", response.text)
            self.assertIn("已全部显示，共 12 个", response.text)
            self.assertNotIn("应用名：1年级语文下册", response.text)

    def test_package_lookup_followup_supports_last_page_and_last_count(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            config_path = base / "config.json"
            project_dir = base / "android-project"
            project_dir.mkdir()
            snapshot_path = base / "packlist-scan.json"
            entries = [
                {
                    "sheet": "CfgGameConfig",
                    "row": 100 + idx,
                    "channel": f"xm50{idx:02d}",
                    "app_name": f"{idx + 1}年级语文上册",
                    "pkg_name": f"com.pelbs.book50{idx:02d}",
                    "version_code": "68",
                    "version_name": f"3.50{idx:02d}.38.2",
                }
                for idx in range(12)
            ]
            snapshot_path.write_text(json.dumps({"ok": True, "result": entries}, ensure_ascii=False), encoding="utf-8")
            config_path.write_text(
                json.dumps(
                    {
                        "packaging": {
                            "project_dir": str(project_dir),
                            "script": str(base / "package.js"),
                            "packlist_scan_file": str(snapshot_path),
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (base / "package.js").write_text("", encoding="utf-8")
            agent = ReviewAgent(JsonStateStore(base / "state.json"), oppo_config_path=config_path)

            agent.handle_message("chat-1", "语文都有那些年级的包？")
            response = agent.handle_message("chat-1", "显示最后3个")

            self.assertIn("应用名：10年级语文上册", response.text)
            self.assertIn("应用名：12年级语文上册", response.text)
            self.assertNotIn("应用名：1年级语文上册", response.text)

    def test_continue_packaging_test_does_not_turn_package_lookup_page(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            config_path = base / "config.json"
            project_dir = base / "android-project"
            project_dir.mkdir()
            snapshot_path = base / "packlist-scan.json"
            snapshot_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "result": [
                            {
                                "sheet": "CfgGameConfig",
                                "row": 1,
                                "channel": "xm11038",
                                "app_name": "三年级语文上册",
                                "pkg_name": "com.pelbs.book11038",
                                "version_code": "68",
                                "version_name": "3.11038.38.2",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            config_path.write_text(
                json.dumps(
                    {
                        "packaging": {
                            "project_dir": str(project_dir),
                            "script": str(base / "package.js"),
                            "packlist_scan_file": str(snapshot_path),
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (base / "package.js").write_text("", encoding="utf-8")
            llm = FakeLlmClient({"intent": "chat", "confidence": 1, "reply": "收到，继续打包测试。"})
            agent = ReviewAgent(JsonStateStore(base / "state.json"), oppo_config_path=config_path, llm_client=llm)
            agent.packaging_agent = FakePackagingAgent()
            agent.handle_message("chat-1", "语文都有那些年级的包？")

            response = agent.handle_message("chat-1", "继续打包测试")

            self.assertNotEqual(response.data.get("intent"), "package_lookup")
            self.assertNotIn("包列表：", response.text)

    def test_packaging_catalog_request_lists_all_packages_with_pagination(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            config_path = base / "config.json"
            project_dir = base / "android-project"
            project_dir.mkdir()
            snapshot_path = base / "packlist-scan.json"
            entries = []
            for idx in range(11):
                entries.append(
                    {
                        "sheet": "CfgGameConfig",
                        "row": 100 + idx,
                        "channel": f"xm40{idx:02d}",
                        "app_name": f"教材{idx + 1}",
                        "pkg_name": f"com.pelbs.book40{idx:02d}",
                        "version_code": "68",
                        "version_name": f"3.40{idx:02d}.38.2",
                    }
                )
            snapshot_path.write_text(json.dumps({"ok": True, "result": entries}, ensure_ascii=False), encoding="utf-8")
            config_path.write_text(
                json.dumps(
                    {
                        "packaging": {
                            "project_dir": str(project_dir),
                            "script": str(base / "package.js"),
                            "packlist_scan_file": str(snapshot_path),
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (base / "package.js").write_text("", encoding="utf-8")
            agent = ReviewAgent(JsonStateStore(base / "state.json"), oppo_config_path=config_path)

            response = agent.handle_message("chat-1", "查一下，都可以打那些包？")

            self.assertIn("查包结果", response.text)
            self.assertIn("- 关键词：全部", response.text)
            self.assertIn("- 总数：11", response.text)
            self.assertIn("应用名：教材1", response.text)
            self.assertIn("发送“还有呢”或“下一页”继续", response.text)

    def test_query_oppo_status_remembers_app_context(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = self._write_minimal_config(Path(temp_dir))
            fake_agent = FakeOppoWorkflowAgent()
            agent = ReviewAgent(
                JsonStateStore(Path(temp_dir) / "state.json"),
                oppo_config_path=config_path,
                oppo_agent_factory=lambda: fake_agent,
            )

            agent.handle_message("chat-1", "查询审核状态：200", "user-1")
            session = agent.state_store.get_session("chat-1")

            self.assertEqual(session["app_info"]["app_name"], "示例应用")
            self.assertEqual(session["app_info"]["pkg_name"], "com.example.app")
            self.assertEqual(session["app_info"]["version_code"], "200")

    def test_remediation_checklist_writes_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            agent = ReviewAgent(JsonStateStore(Path(temp_dir) / "state.json"))
            agent.handle_message(
                "chat-1",
                "分析驳回：APK相似度0.92，请勿重复提交，请补充ICP备案网站",
                "user-1",
            )

            response = agent.handle_message("chat-1", "整改清单", "user-1")
            status = agent.handle_message("chat-1", "状态").text

            self.assertIn("整改清单", response.text)
            self.assertIn("ICP", response.text)
            self.assertIn("整改待办", status)

    def test_submission_check_includes_validation_and_risk(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            agent = ReviewAgent(
                JsonStateStore(Path(temp_dir) / "state.json"),
                oppo_agent_factory=lambda: FakeOppoWorkflowAgent(),
            )
            agent.handle_message(
                "chat-1",
                "分析驳回：APK相似度0.92，请勿重复提交，请补充ICP备案网站",
                "user-1",
            )

            response = agent.handle_message("chat-1", "提交检查")

            self.assertIn("配置文件：不通过", response.text)
            self.assertIn("缺字段：apk_url", response.text)
            self.assertIn("不建议原包直接重提", response.text)

    def test_market_search_records_last_result(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            agent = ReviewAgent(
                JsonStateStore(Path(temp_dir) / "state.json"),
                market_searcher_factory=lambda: FakeMarketSearcher(),
            )

            response = agent.handle_message("chat-1", "搜索竞品：英语四级单词", "user-1")
            status = agent.handle_message("chat-1", "状态").text
            session = agent.state_store.get_session("chat-1")

            self.assertIn("应用商店竞品搜索", response.text)
            self.assertIn("查询结果：", response.text)
            self.assertIn("1. 四级单词竞品", response.text)
            self.assertIn("   - 商店：Google Play", response.text)
            self.assertIn("四级单词竞品", response.text)
            self.assertEqual(session["last_market_search"]["query"], "英语四级单词")
            self.assertIn("最近应用商店查询", status)

    def test_market_search_exact_match_filters_out_variants(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            searcher = FakeVariantMarketSearcher()
            llm = FakeLlmClient(
                tool_call={
                    "tool": "market_search",
                    "confidence": 0.9,
                    "arguments": {
                        "query": "抖音",
                        "exact_match": True,
                        "exclude_terms": ["极速版", "火山版"],
                        "target_stores": ["oppo_app_market"],
                    },
                },
                tool_summary="我只查了抖音本体，OPPO 当前公开结果里没有精确匹配。\n- 已排除：抖音极速版、抖音火山版",
            )
            agent = ReviewAgent(
                JsonStateStore(Path(temp_dir) / "state.json"),
                market_searcher_factory=lambda: searcher,
                llm_client=llm,
            )

            response = agent.handle_message("chat-1", "只要抖音APP，不要极速版和火山版")

            self.assertIn("我只查了抖音本体", response.text)
            self.assertIn("已排除：抖音极速版、抖音火山版", response.text)
            self.assertEqual(searcher.calls[0]["stores"], {"oppo_app_market"})
            self.assertEqual(llm.summary_calls[0][2]["tool"], "market_search")
            self.assertIn("filtered_names", llm.summary_calls[0][3]["data"])

    def test_market_search_one_shot_store_scope(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            searcher = FakeVariantMarketSearcher()
            llm = FakeLlmClient(
                tool_call={
                    "tool": "market_search",
                    "confidence": 0.9,
                    "arguments": {
                        "query": "抖音",
                        "target_stores": ["oppo_app_market"],
                    },
                }
            )
            agent = ReviewAgent(
                JsonStateStore(Path(temp_dir) / "state.json"),
                market_searcher_factory=lambda: searcher,
                llm_client=llm,
            )

            agent.handle_message("chat-1", "你现在搜索一下OPPO应用商店，看下抖音这个APP的下载量是多少")

            self.assertEqual(searcher.calls[0]["stores"], {"oppo_app_market"})

    def test_market_download_query_is_not_auto_recorded(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            searcher = FakeVariantMarketSearcher()
            llm = FakeLlmClient(
                tool_call={
                    "tool": "market_download_snapshot",
                    "confidence": 0.9,
                    "arguments": {
                        "query": "抖音",
                        "target_stores": ["xiaomi_app_store"],
                    },
                }
            )
            agent = ReviewAgent(
                JsonStateStore(Path(temp_dir) / "state.json"),
                market_searcher_factory=lambda: searcher,
                llm_client=llm,
            )

            response = agent.handle_message("chat-1", "帮我查一下小米应用市场，抖音的下载量", "user-1")
            session = agent.state_store.get_session("chat-1")

            self.assertIn("应用商店查询", response.text)
            self.assertIn("- 关键词：抖音", response.text)
            self.assertNotIn("已记录", response.text)
            self.assertEqual(searcher.calls[0]["stores"], {"xiaomi_app_store"})
            self.assertNotIn("market_download_snapshots", session)
            self.assertEqual(response.data["tool_call"]["tool"], "market_search")

    def test_market_followup_reuses_previous_query_for_other_stores(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            searcher = FakeVariantMarketSearcher()
            llm = FakeLlmClient(
                tool_call={
                    "tool": "market_search",
                    "confidence": 0.9,
                    "arguments": {
                        "query": "抖音",
                        "exact_match": True,
                        "exclude_terms": ["极速版", "火山版"],
                        "target_stores": ["oppo_app_market"],
                    },
                }
            )
            agent = ReviewAgent(
                JsonStateStore(Path(temp_dir) / "state.json"),
                market_searcher_factory=lambda: searcher,
                llm_client=llm,
            )

            agent.handle_message("chat-1", "你现在搜索一下OPPO应用商店，看下抖音这个APP的下载量是多少")
            response = agent.handle_message("chat-1", "搜索一些其他应用商店。")
            session = agent.state_store.get_session("chat-1")

            self.assertEqual(searcher.calls[1]["query"], "抖音")
            self.assertNotIn("oppo_app_market", searcher.calls[1]["stores"])
            self.assertTrue(session["last_market_search_request"]["exact_match"])
            self.assertIn("未找到与“抖音”精确匹配", response.text)

    def test_recent_context_question_reports_conversation_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            agent = ReviewAgent(JsonStateStore(Path(temp_dir) / "state.json"))

            agent.handle_message("chat-1", "记录应用：小学四年级英语 / com.pelbs.book43 / 32")
            agent.handle_message("chat-1", "你现在搜索一下OPPO应用商店，看下抖音这个APP的下载量是多少")
            response = agent.handle_message("chat-1", "我之前发给你的信息是什么？")

            self.assertIn("最近对话", response.text)
            self.assertIn("最近你发给我的内容", response.text)
            self.assertIn("抖音", response.text)

    def test_llm_tool_call_executes_packaging_and_summarizes_result(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fake_packaging = FakePackagingAgent()
            llm = FakeLlmClient(
                tool_call={
                    "tool": "package_apk",
                    "confidence": 0.93,
                    "arguments": {"app_name": "八年级语文下册", "dry_run": True},
                },
                tool_summary="已按八年级语文下册做打包预演，渠道是 xm1067。",
            )
            agent = ReviewAgent(JsonStateStore(Path(temp_dir) / "state.json"), llm_client=llm)
            agent.packaging_agent = fake_packaging

            response = agent.handle_message("chat-1", "先给八年级语文下册出一个测试包")

            self.assertIn("打包预演", llm.summary_calls[0][3]["text"])
            self.assertIn("处理结果", response.text)
            self.assertIn("已按八年级语文下册", response.text)
            self.assertEqual(fake_packaging.package_one_calls[0]["app_name"], "八年级语文下册")
            self.assertTrue(fake_packaging.package_one_calls[0]["dry_run"])
            self.assertEqual(response.data["tool_call"]["tool"], "package_apk")

    def test_full_chain_trace_records_llm_tool_and_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            fake_packaging = FakePackagingAgent()
            llm = FakeLlmClient(
                tool_call={
                    "tool": "package_apk",
                    "confidence": 0.93,
                    "arguments": {"app_name": "八年级语文下册", "dry_run": True},
                    "api_key": "should-not-leak",
                },
                tool_summary="已完成打包预演。",
            )
            state = JsonStateStore(base / "state.json")
            agent = ReviewAgent(state, llm_client=llm)
            agent.packaging_agent = fake_packaging

            response = agent.handle_message("chat-1", "先给八年级语文下册出一个测试包", "user-1")

            today = time.strftime("%Y-%m-%d")
            trace_path = base / "sessions" / "chat-1" / f"trace-{today}.jsonl"
            trace = json.loads(trace_path.read_text(encoding="utf-8").splitlines()[-1])
            event_types = [event["type"] for event in trace["events"]]

            self.assertIn("已完成打包预演", response.text)
            self.assertEqual(trace["user_message"], "先给八年级语文下册出一个测试包")
            self.assertIn("llm_tool_choice_request", event_types)
            self.assertIn("llm_tool_choice_response", event_types)
            self.assertIn("tool_execute_request", event_types)
            self.assertIn("tool_execute_response", event_types)
            self.assertIn("llm_tool_summary_request", event_types)
            self.assertIn("llm_tool_summary_response", event_types)
            self.assertIn("final_response", trace)
            self.assertNotIn("should-not-leak", json.dumps(trace, ensure_ascii=False))
            self.assertIn("***REDACTED***", json.dumps(trace, ensure_ascii=False))

    def test_llm_tool_call_can_view_and_stage_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            config_path = self._write_minimal_config(base_dir)
            llm = FakeLlmClient(
                tool_call={
                    "tool": "stage_config_update",
                    "confidence": 0.91,
                    "arguments": {"config_assignment": "submission.version_code=101"},
                },
                tool_summary="版本号修改已暂存，还没有写入配置文件。",
            )
            agent = ReviewAgent(
                JsonStateStore(base_dir / "state.json"),
                oppo_config_path=config_path,
                llm_client=llm,
            )

            response = agent.handle_message("chat-1", "版本号先改成 101，等我确认再保存", "user-1")
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            session = agent.state_store.get_session("chat-1")

            self.assertIn("暂存", response.text)
            self.assertEqual(session["pending_config_patch"]["submission.version_code"], "101")
            self.assertEqual(raw["submission"]["version_code"], "100")
            self.assertEqual(llm.summary_calls[0][2]["tool"], "stage_config_update")

    def test_llm_tool_call_can_bind_material(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            config_path = self._write_minimal_config(base_dir)
            upload_path = base_dir / "copyright.pdf"
            upload_path.write_bytes(b"pdf")
            state = JsonStateStore(base_dir / "state.json")
            state.update_session(
                "chat-1",
                {
                    "last_upload": {
                        "path": str(upload_path),
                        "file_name": "copyright.pdf",
                        "resource_type": "file",
                    }
                },
            )
            llm = FakeLlmClient(
                tool_call={
                    "tool": "bind_material",
                    "confidence": 0.9,
                    "arguments": {"material_label": "版权证明"},
                },
                tool_summary="版权证明已绑定，并完成了一次提交检查。",
            )
            agent = ReviewAgent(state, oppo_config_path=config_path, llm_client=llm)

            response = agent.handle_message("chat-1", "把刚上传的文件作为版权证明", "user-1")

            self.assertIn("版权证明已绑定", response.text)
            self.assertEqual(response.data["tool_call"]["tool"], "bind_material")
            self.assertIn("bind_material", response.data["tool_result"]["data"]["intent"])

    def test_llm_tool_call_can_analyze_last_image(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = JsonStateStore(Path(temp_dir) / "state.json")
            state.update_session(
                "chat-1",
                {
                    "last_image_analysis": {
                        "ocr_text": "APK相似度0.92，请勿重复提交，请补充ICP备案网站",
                    }
                },
            )
            llm = FakeLlmClient(
                tool_call={
                    "tool": "analyze_last_image",
                    "confidence": 0.9,
                    "arguments": {},
                },
                tool_summary="最近图片已分析：不建议原包直接重提，需要补 ICP 证明。",
            )
            agent = ReviewAgent(state, llm_client=llm)

            response = agent.handle_message("chat-1", "分析一下刚才那张截图", "user-1")

            self.assertIn("最近图片已分析", response.text)
            self.assertEqual(response.data["tool_call"]["tool"], "analyze_last_image")
            self.assertIn("apk_similarity_or_template", response.data["tool_result"]["data"]["categories"])

    def test_market_search_formats_store_status_summary(self):
        class StatusMarketSearcher:
            def search_competitors(self, query, limit=8):
                return AppMarketSearchResult(
                    query=query,
                    apps=[
                        AppMarketListing(
                            store="apple_app_store",
                            app_id="1",
                            name="王者荣耀",
                        )
                    ],
                    errors=["google_play: 超时"],
                    store_statuses=[
                        {"store": "apple_app_store", "status": "ok", "result_count": 8},
                        {"store": "xiaomi_app_store", "status": "ok", "result_count": 1},
                        {"store": "huawei_appgallery", "status": "no_match", "result_count": 0, "message": "未解析到匹配结果"},
                        {"store": "oppo_app_market", "status": "skipped", "result_count": 0, "message": "公开搜索入口不可用，已跳过"},
                        {"store": "google_play", "status": "failed", "result_count": 0, "message": "超时"},
                    ],
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            agent = ReviewAgent(
                JsonStateStore(Path(temp_dir) / "state.json"),
                market_searcher_factory=lambda: StatusMarketSearcher(),
            )

            response = agent.handle_message("chat-1", "搜索应用：王者荣耀", "user-1")

            self.assertIn("查询状态：", response.text)
            self.assertIn("- Apple App Store：8 个结果", response.text)
            self.assertIn("- 小米应用商店：1 个结果", response.text)
            self.assertIn("- 华为 AppGallery：未解析到匹配结果", response.text)
            self.assertIn("- OPPO 软件商店：公开搜索入口不可用，已跳过", response.text)
            self.assertIn("- Google Play：超时", response.text)

    def test_competitor_download_snapshot_is_grouped_by_month(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            agent = ReviewAgent(
                JsonStateStore(Path(temp_dir) / "state.json"),
                market_searcher_factory=lambda: FakeMarketSearcher(),
            )

            response = agent.handle_message("chat-1", "记录竞品下载：英语四级单词", "user-1")
            session = agent.state_store.get_session("chat-1")
            snapshots = session["market_download_snapshots"]
            snapshot = next(iter(snapshots.values()))

            self.assertIn("竞品下载数据已记录", response.text)
            self.assertIn("记录结果：", response.text)
            self.assertIn("1. 四级单词竞品", response.text)
            self.assertIn("1,000,000+", response.text)
            self.assertEqual(snapshot["query"], "英语四级单词")
            self.assertEqual(snapshot["apps"][0]["downloads"], 1000000)
            self.assertEqual(snapshot["apps"][1]["downloads"], None)

    def test_market_query_falls_back_to_recorded_app_name(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            agent = ReviewAgent(
                JsonStateStore(Path(temp_dir) / "state.json"),
                market_searcher_factory=lambda: FakeMarketSearcher(),
            )
            agent.handle_message("chat-1", "记录应用：英语四级单词 / com.example.app / 100")

            response = agent.handle_message("chat-1", "搜索竞品", "user-1")

            self.assertIn("英语四级单词", response.text)

    def test_market_search_rejects_punctuation_only_query(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            agent = ReviewAgent(
                JsonStateStore(Path(temp_dir) / "state.json"),
                market_searcher_factory=lambda: FakeMarketSearcher(),
            )

            response = agent.handle_message("chat-1", "搜索竞品：。", "user-1")

            self.assertIn("应用商店查询缺少关键词", response.text)
            self.assertIn("没有识别到有效的应用名或关键词", response.text)
            self.assertNotIn("应用商店竞品搜索：。", response.text)

    def test_llm_chat_decision_wins_over_generic_search_rule(self):
        class RaisingMarketSearcher:
            def search_competitors(self, query, limit=8):
                raise AssertionError("LLM chat decision should not fall through to market search")

        with tempfile.TemporaryDirectory() as temp_dir:
            llm = FakeLlmClient({"intent": "chat", "confidence": 1, "reply": "这句话不需要调用工具。"})
            agent = ReviewAgent(
                JsonStateStore(Path(temp_dir) / "state.json"),
                market_searcher_factory=lambda: RaisingMarketSearcher(),
                llm_client=llm,
            )

            response = agent.handle_message("chat-1", "搜索，王者荣耀", "user-1")

            self.assertEqual(response.data["intent"], "chat")
            self.assertIn("回复", response.text)
            self.assertIn("不需要调用工具", response.text)
            self.assertNotIn("应用商店竞品搜索", response.text)
            self.assertEqual(len(llm.calls), 1)

    def test_default_app_question_reads_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = self._write_minimal_config(Path(temp_dir))
            agent = ReviewAgent(
                JsonStateStore(Path(temp_dir) / "state.json"),
                oppo_config_path=config_path,
            )

            response = agent.handle_message("chat-1", "现在的默认应用是什么？")

            self.assertIn("当前默认应用", response.text)
            self.assertIn("示例应用", response.text)
            self.assertIn("com.example.app", response.text)

    def test_contextual_market_query_uses_default_app(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = self._write_minimal_config(Path(temp_dir))
            agent = ReviewAgent(
                JsonStateStore(Path(temp_dir) / "state.json"),
                oppo_config_path=config_path,
                market_searcher_factory=lambda: FakeMarketSearcher(),
            )

            response = agent.handle_message("chat-1", "找一下这个应用相似的应用。", "user-1")

            self.assertIn("应用商店竞品搜索", response.text)
            self.assertIn("- 关键词：示例应用", response.text)
            self.assertNotIn("- 关键词：这个", response.text)

    def test_semantic_market_search_understands_natural_language(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            agent = ReviewAgent(
                JsonStateStore(Path(temp_dir) / "state.json"),
                market_searcher_factory=lambda: FakeMarketSearcher(),
            )

            response = agent.handle_message("chat-1", "帮我看看有哪些类似的背单词软件", "user-1")

            self.assertIn("应用商店竞品搜索", response.text)
            self.assertIn("背单词", response.text)

    def test_data_platform_research_does_not_run_app_store_search(self):
        class RaisingMarketSearcher:
            def search_competitors(self, query, limit=8):
                raise AssertionError("data platform research should not run app store search")

        with tempfile.TemporaryDirectory() as temp_dir:
            llm = FakeLlmClient(
                {
                    "intent": "market_search",
                    "confidence": 0.9,
                    "query": "第三方数据平台 类似七麦 点点数据",
                }
            )
            agent = ReviewAgent(
                JsonStateStore(Path(temp_dir) / "state.json"),
                market_searcher_factory=lambda: RaisingMarketSearcher(),
                llm_client=llm,
            )

            response = agent.handle_message(
                "chat-1",
                "搜一下第三方数据平台。就是类似于七麦这种，应用商店统计数据的平台。",
                "user-1",
            )

            self.assertEqual(response.data["intent"], "app_store_data_platform_research")
            self.assertIn("应用商店数据/ASO 平台调研", response.text)
            self.assertIn("七麦数据", response.text)
            self.assertNotIn("应用商店竞品搜索", response.text)

    def test_semantic_market_snapshot_uses_session_app_name(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            agent = ReviewAgent(
                JsonStateStore(Path(temp_dir) / "state.json"),
                market_searcher_factory=lambda: FakeMarketSearcher(),
            )
            agent.handle_message("chat-1", "记录应用：英语四级单词 / com.example.app / 100")

            response = agent.handle_message("chat-1", "把竞品下载量记录一下", "user-1")

            self.assertIn("已记录", response.text)
            self.assertIn("英语四级单词", response.text)

    def test_semantic_help_and_status_requests(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            agent = ReviewAgent(JsonStateStore(Path(temp_dir) / "state.json"))

            help_response = agent.handle_message("chat-1", "你能做什么？")
            status_response = agent.handle_message("chat-1", "现在进度怎么样？")

            self.assertIn("我可以协助", help_response.text)
            self.assertIn("当前会话", status_response.text)

    def test_strips_feishu_bot_mentions_before_intent_matching(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            agent = ReviewAgent(JsonStateStore(Path(temp_dir) / "state.json"))

            response = agent.handle_message("chat-1", "帮助@提交助手")

            self.assertEqual(response.data["intent"], "help")
            self.assertIn("我可以协助", response.text)

    def test_capability_questions_do_not_trigger_market_search(self):
        class RaisingMarketSearcher:
            def search_competitors(self, query, limit=8):
                raise AssertionError("capability question should not run market search")

        with tempfile.TemporaryDirectory() as temp_dir:
            agent = ReviewAgent(
                JsonStateStore(Path(temp_dir) / "state.json"),
                market_searcher_factory=lambda: RaisingMarketSearcher(),
            )

            response = agent.handle_message("chat-1", "拥有竞品搜索能力吗？")
            status = agent.handle_message("chat-1", "输出当前记录").text

            self.assertEqual(response.data["intent"], "capability_question")
            self.assertIn("有应用商店查询能力", response.text)
            self.assertNotIn("最近应用商店查询", status)
            self.assertIn("当前会话", status)

    def test_market_store_scope_question_lists_supported_stores(self):
        class RaisingMarketSearcher:
            def search_competitors(self, query, limit=8):
                raise AssertionError("store scope question should not run market search")

        with tempfile.TemporaryDirectory() as temp_dir:
            agent = ReviewAgent(
                JsonStateStore(Path(temp_dir) / "state.json"),
                market_searcher_factory=lambda: RaisingMarketSearcher(),
            )

            response = agent.handle_message("chat-1", "目前可以查询那些厂家的应用商店？@提交助手")

            self.assertEqual(response.data["intent"], "capability_question")
            self.assertEqual(response.data["capability"], "market_store_scope")
            self.assertIn("Apple App Store", response.text)
            self.assertIn("OPPO 软件商店", response.text)
            self.assertIn("荣耀应用市场", response.text)
            self.assertNotIn("应用商店竞品搜索：目前可以", response.text)

    def test_market_store_preference_is_session_only_and_affects_store_list(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            config_path = self._write_minimal_config(base_dir)
            before = config_path.read_text(encoding="utf-8")
            agent = ReviewAgent(
                JsonStateStore(base_dir / "state.json"),
                oppo_config_path=config_path,
            )

            preference = agent.handle_message("chat-1", "默认不查询Google Play @提交助手")
            stores = agent.handle_message("chat-1", "目前可以查询那些厂家的应用商店？@提交助手")
            status = agent.handle_message("chat-1", "状态").text
            after = config_path.read_text(encoding="utf-8")

            self.assertEqual(before, after)
            self.assertEqual(preference.data["intent"], "market_store_preference")
            self.assertIn("不会修改默认配置文件", preference.text)
            self.assertIn("当前会话已按你的偏好排除", stores.text)
            self.assertIn("- Google Play", stores.text)
            self.assertIn("应用商店偏好：不查询 Google Play", status)

    def test_market_store_preference_filters_actual_search(self):
        class CapturingMarketSearcher:
            def __init__(self):
                self.calls = []

            def search_competitors(self, query, limit=8, stores=None):
                self.calls.append({"query": query, "limit": limit, "stores": stores})
                return AppMarketSearchResult(
                    query=query,
                    apps=[
                        AppMarketListing(
                            store="apple_app_store",
                            app_id="123456",
                            name="英语单词竞品",
                        )
                    ],
                    store_statuses=[
                        {"store": "apple_app_store", "status": "ok", "result_count": 1},
                    ],
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            searcher = CapturingMarketSearcher()
            agent = ReviewAgent(
                JsonStateStore(Path(temp_dir) / "state.json"),
                market_searcher_factory=lambda: searcher,
            )

            agent.handle_message("chat-1", "默认不查询Google Play")
            response = agent.handle_message("chat-1", "搜索竞品：英语四级单词")

            self.assertIn("应用商店竞品搜索", response.text)
            self.assertNotIn("google_play", searcher.calls[-1]["stores"])
            self.assertIn("apple_app_store", searcher.calls[-1]["stores"])

    def test_market_searcher_reads_qimai_config_from_separate_market_data_file(self):
        captured = {}

        class CapturingSearcher:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def search_competitors(self, query, limit=8, stores=None):
                return AppMarketSearchResult(
                    query=query,
                    apps=[],
                    store_statuses=[],
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            config_path = self._write_minimal_config(base_dir)
            market_data_path = base_dir / "market_data.json"
            market_data_path.write_text(
                json.dumps(
                    {
                        "qimai": {
                            "enabled": True,
                            "base_url": "https://qimai.example",
                            "search_path": "/apps/search",
                            "api_key": "secret",
                        }
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            agent = ReviewAgent(
                JsonStateStore(base_dir / "state.json"),
                oppo_config_path=config_path,
                market_data_config_path=market_data_path,
            )

            with patch("autoreview.agent.review_agent.AppMarketSearcher", CapturingSearcher):
                agent.handle_message("chat-1", "搜索竞品：英语四级单词")

        self.assertEqual(captured["market_data_config"]["qimai"]["base_url"], "https://qimai.example")

    def test_market_searcher_does_not_read_qimai_config_from_submission_config(self):
        captured = {}

        class CapturingSearcher:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def search_competitors(self, query, limit=8, stores=None):
                return AppMarketSearchResult(query=query, apps=[], store_statuses=[])

        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            config_path = self._write_minimal_config(base_dir)
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            raw["market_data"] = {
                "qimai": {
                    "enabled": True,
                    "base_url": "https://wrong-place.example",
                    "search_path": "/apps/search",
                }
            }
            config_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
            agent = ReviewAgent(
                JsonStateStore(base_dir / "state.json"),
                oppo_config_path=config_path,
            )

            with patch("autoreview.agent.review_agent.AppMarketSearcher", CapturingSearcher):
                agent.handle_message("chat-1", "搜索竞品：英语四级单词")

        self.assertEqual(captured["market_data_config"], {})

    def test_image_capability_questions_use_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = self._write_minimal_config(Path(temp_dir))
            agent = ReviewAgent(
                JsonStateStore(Path(temp_dir) / "state.json"),
                oppo_config_path=config_path,
            )

            ocr = agent.handle_message("chat-1", "接入了ocr能力吗@提交助手")
            image2 = agent.handle_message("chat-1", "接入了image2了吗@提交助手")

            self.assertIn("OCR 已接入", ocr.text)
            self.assertEqual(ocr.data["capability"], "ocr")
            self.assertIn("image2 目前未配置", image2.text)
            self.assertEqual(image2.data["capability"], "image2")

    def test_clear_current_session_state_removes_only_that_session(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = JsonStateStore(Path(temp_dir) / "state.json")
            state.update_session("chat-1", {"app_info": {"app_name": "A"}})
            state.update_session("chat-2", {"app_info": {"app_name": "B"}})
            agent = ReviewAgent(state)

            response = agent.handle_message("chat-1", "清空当前记录")

            self.assertIn("会话记录已清空", response.text)
            self.assertIn("当前会话记录已清空", response.text)
            self.assertEqual(state.get_session("chat-1"), {})
            self.assertEqual(state.get_session("chat-2")["app_info"]["app_name"], "B")

    def test_clear_all_state_removes_all_sessions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = JsonStateStore(Path(temp_dir) / "state.json")
            state.update_session("chat-1", {"app_info": {"app_name": "A"}})
            state.update_session("chat-2", {"app_info": {"app_name": "B"}})
            agent = ReviewAgent(state)

            response = agent.handle_message("chat-1", "清空所有记录")
            raw = state.load()

            self.assertIn("全部会话记录已清空", response.text)
            self.assertEqual(raw["sessions"], {})

    def test_semantic_status_and_submission_check_requests_use_oppo_agent(self):
        fake_agent = FakeOppoWorkflowAgent()
        agent = ReviewAgent(
            JsonStateStore(Path(tempfile.mkdtemp()) / "state.json"),
            oppo_agent_factory=lambda: fake_agent,
        )

        audit = agent.handle_message("chat-1", "帮我查审核状态 200")
        check = agent.handle_message("chat-1", "帮我检查一下现在能不能提交")
        direct_check = agent.handle_message("chat-1", "现在是否可以提交？")

        self.assertEqual(fake_agent.status_version_code, "200")
        self.assertIn("OPPO 审核状态", audit.text)
        self.assertIn("提交检查", check.text)
        self.assertIn("提交检查", direct_check.text)

    def test_semantic_config_view_and_update(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            config_path = self._write_minimal_config(base_dir)
            agent = ReviewAgent(
                JsonStateStore(base_dir / "state.json"),
                oppo_config_path=config_path,
            )

            view = agent.handle_message("chat-1", "帮我看看当前提交配置")
            stage = agent.handle_message("chat-1", "把 submission.version_code=101 暂存一下")

            self.assertIn("当前提交配置", view.text)
            self.assertIn("submission.version_code", stage.text)
            session = agent.state_store.get_session("chat-1")
            self.assertEqual(session["pending_config_patch"]["submission.version_code"], "101")

    def test_semantic_bind_material_request(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            config_path = self._write_minimal_config(base_dir)
            upload_path = base_dir / "upload.apk"
            upload_path.write_bytes(b"apk")
            state = JsonStateStore(base_dir / "state.json")
            state.update_session(
                "chat-1",
                {
                    "last_upload": {
                        "path": str(upload_path),
                        "file_name": "upload.apk",
                        "resource_type": "file",
                    }
                },
            )
            agent = ReviewAgent(state, oppo_config_path=config_path)

            response = agent.handle_message("chat-1", "把刚才上传的 APK 绑定成材料")

            self.assertIn("材料已绑定", response.text)

    def test_semantic_image_analysis_and_remediation_requests(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            agent = ReviewAgent(JsonStateStore(Path(temp_dir) / "state.json"))
            agent.state_store.update_session(
                "chat-1",
                {
                    "last_image_analysis": {
                        "ocr_text": "APK相似度0.92，请勿重复提交，请补充ICP备案网站",
                    }
                },
            )

            analysis = agent.handle_message("chat-1", "帮我分析一下最近这张截图")
            checklist = agent.handle_message("chat-1", "接下来怎么整改？")

            self.assertIn("不建议原包直接重提", analysis.text)
            self.assertIn("整改清单", checklist.text)

    def test_llm_fallback_chat_and_memory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            llm = FakeLlmClient(
                {
                    "intent": "remember",
                    "confidence": 0.9,
                    "memories": [{"category": "default_app", "text": "默认应用是英语四级单词"}],
                    "app_info": {"app_name": "英语四级单词", "pkg_name": "com.example.words"},
                    "preferences": {"tone": "简洁"},
                    "reply": "我记住了，后续默认按英语四级单词处理。",
                }
            )
            agent = ReviewAgent(JsonStateStore(Path(temp_dir) / "state.json"), llm_client=llm)

            response = agent.handle_message("chat-1", "以后这个应用默认就按四级单词处理", "user-1")
            status = agent.handle_message("chat-1", "状态").text
            session = agent.state_store.get_session("chat-1")

            self.assertIn("已记录", response.text)
            self.assertIn("我记住了", response.text)
            self.assertEqual(session["agent_memory"], ["default_app: 默认应用是英语四级单词"])
            self.assertEqual(session["long_term_memory"]["app_info"]["app_name"], "英语四级单词")
            self.assertEqual(session["long_term_memory"]["preferences"]["tone"], "简洁")
            self.assertIn("长期记忆：1 条", status)

    def test_llm_context_is_compact_for_large_status_payload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = JsonStateStore(Path(temp_dir) / "state.json")
            state.update_session(
                "chat-1",
                {
                    "app_info": {"app_name": "示例应用", "pkg_name": "com.example.app", "version_code": "100"},
                    "last_oppo_status": {
                        "pkg_name": "com.example.app",
                        "version_code": "100",
                        "review_state": "rejected",
                        "task": {"task_state": "3", "err_msg": "任务失败"},
                        "app_info": {
                            "app_name": "示例应用",
                            "audit_status_name": "审核不通过",
                            "huge_blob": "X" * 5000,
                        },
                    },
                },
            )
            llm = FakeLlmClient({"intent": "chat", "confidence": 1, "reply": "好的。"})
            agent = ReviewAgent(state, llm_client=llm)

            agent.handle_message("chat-1", "你好", "user-1")

            session_context = llm.calls[0][1]
            compact_status = session_context["session"]["last_oppo_status"]
            self.assertNotIn("huge_blob", json.dumps(compact_status, ensure_ascii=False))
            self.assertEqual(compact_status["audit_status_name"], "审核不通过")

    def test_llm_low_confidence_reply_is_lightly_formatted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            llm = FakeLlmClient(
                {
                    "intent": "unknown",
                    "confidence": 0.2,
                    "reply": "你这句话我还没完全听懂。",
                }
            )
            agent = ReviewAgent(JsonStateStore(Path(temp_dir) / "state.json"), llm_client=llm)

            response = agent.handle_message("chat-1", "嗯就那个", "user-1")

            self.assertIn("还没理解清楚", response.text)
            self.assertIn("你这句话我还没完全听懂", response.text)
            self.assertIn("发送“帮助”查看可用场景", response.text)

    def test_llm_fallback_dispatches_market_search(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            llm = FakeLlmClient(
                {
                    "intent": "market_search",
                    "confidence": 0.92,
                    "query": "英语四级单词",
                }
            )
            agent = ReviewAgent(
                JsonStateStore(Path(temp_dir) / "state.json"),
                market_searcher_factory=lambda: FakeMarketSearcher(),
                llm_client=llm,
            )

            response = agent.handle_message("chat-1", "研究一下这个赛道有哪些产品", "user-1")

            self.assertIn("应用商店竞品搜索", response.text)
            self.assertIn("英语四级单词", response.text)

    def test_llm_fallback_dispatches_config_update_as_stage_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            config_path = self._write_minimal_config(base_dir)
            llm = FakeLlmClient(
                {
                    "intent": "stage_config_update",
                    "confidence": 0.95,
                    "config_assignment": "submission.version_code=101",
                }
            )
            agent = ReviewAgent(
                JsonStateStore(base_dir / "state.json"),
                oppo_config_path=config_path,
                llm_client=llm,
            )

            response = agent.handle_message("chat-1", "下个版本号先改到 101", "user-1")
            raw = json.loads(config_path.read_text(encoding="utf-8"))

            self.assertIn("待保存配置修改", response.text)
            self.assertEqual(raw["submission"]["version_code"], "100")

    def test_llm_is_called_when_rules_match_but_local_command_still_wins(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            llm = FakeLlmClient({"intent": "chat", "confidence": 1, "reply": "should not be used"})
            agent = ReviewAgent(JsonStateStore(Path(temp_dir) / "state.json"), llm_client=llm)

            response = agent.handle_message("chat-1", "状态")

            self.assertIn("当前会话", response.text)
            self.assertEqual(len(llm.calls), 1)

    def test_llm_context_includes_default_config_and_preferences(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            config_path = self._write_minimal_config(base_dir)
            llm = FakeLlmClient({"intent": "chat", "confidence": 1, "reply": "知道了。"})
            agent = ReviewAgent(
                JsonStateStore(base_dir / "state.json"),
                oppo_config_path=config_path,
            )

            agent.handle_message("chat-1", "默认不查询Google Play")
            agent.llm_client = llm
            agent.handle_message("chat-1", "随便聊一句")
            context = llm.calls[-1][1]

            self.assertEqual(context["default_config"]["app_info"]["app_name"], "示例应用")
            self.assertEqual(context["preferences"]["market_stores"]["disabled_stores"], ["google_play"])
            self.assertEqual(context["long_term_memory"]["preferences"]["market_stores"]["disabled_stores"], ["google_play"])
            self.assertGreaterEqual(len(context["recent_conversation"]), 1)
            self.assertIn({"store": "google_play", "label": "Google Play"}, context["supported_market_stores"])

    def test_llm_can_dispatch_market_store_preference(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            llm = FakeLlmClient(
                {
                    "intent": "market_store_preference",
                    "confidence": 0.92,
                    "disable_stores": ["google_play"],
                }
            )
            agent = ReviewAgent(JsonStateStore(Path(temp_dir) / "state.json"), llm_client=llm)

            response = agent.handle_message("chat-1", "后续海外的先别查了，Google Play 跳过")
            status = agent.handle_message("chat-1", "状态").text

            self.assertIn("默认不查询Google Play", response.text)
            self.assertIn("应用商店偏好：不查询 Google Play", status)

    def test_conversation_history_keeps_last_20_turns(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            agent = ReviewAgent(JsonStateStore(Path(temp_dir) / "state.json"))

            for index in range(25):
                agent.handle_message("chat-1", f"记录应用：应用{index} / com.example.{index} / {index}", "user-1")

            session = agent.state_store.get_session("chat-1")
            history = session["conversation_history"]

            self.assertEqual(len(history), 20)
            self.assertIn("应用5", history[0]["user"])
            self.assertIn("应用24", history[-1]["user"])
            self.assertEqual(history[-1]["intent"], "record_app")

    def test_llm_context_uses_recent_20_turns(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            llm = FakeLlmClient({"intent": "chat", "confidence": 1, "reply": "收到。"})
            agent = ReviewAgent(JsonStateStore(Path(temp_dir) / "state.json"))

            for index in range(22):
                agent.handle_message("chat-1", f"记录应用：应用{index} / com.example.{index} / {index}", "user-1")
            agent.llm_client = llm
            agent.handle_message("chat-1", "随便聊一句", "user-1")
            context = llm.calls[-1][1]

            self.assertEqual(len(context["recent_conversation"]), 20)
            self.assertNotIn("应用0", context["recent_conversation"][0]["user"])
            self.assertIn("应用21", context["recent_conversation"][-1]["user"])

    def test_view_submission_config_hides_secrets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = self._write_minimal_config(Path(temp_dir))
            agent = ReviewAgent(
                JsonStateStore(Path(temp_dir) / "state.json"),
                oppo_config_path=config_path,
            )

            response = agent.handle_message("chat-1", "查看提交配置")

            self.assertIn("当前提交配置", response.text)
            self.assertIn("com.example.app", response.text)
            self.assertIn("OPPO 密钥：已配置", response.text)
            self.assertIn("大模型：test-model", response.text)
            self.assertIn("大模型密钥：已配置", response.text)
            self.assertNotIn("client-secret", response.text)
            self.assertNotIn("feishu-secret", response.text)
            self.assertNotIn("llm-secret", response.text)

    def test_view_submission_config_shows_openclaw_llm_auth(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            config_path = self._write_minimal_config(base)
            llm_path = base / "config" / "llm_config.json"
            llm_path.write_text(
                json.dumps(
                    {
                        "enabled": True,
                        "provider": "openclaw",
                        "model": "gpt-5.5",
                        "openclaw": {"command": "openclaw", "args": ["run", "--stdin"]},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            agent = ReviewAgent(JsonStateStore(base / "state.json"), oppo_config_path=config_path)

            response = agent.handle_message("chat-1", "查看提交配置")

            self.assertIn("大模型：gpt-5.5（OpenClaw）", response.text)
            self.assertIn("OpenClaw 本机账号授权", response.text)
            self.assertNotIn("大模型密钥：已配置", response.text)

    def test_config_update_requires_confirmation_and_backs_up(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            config_path = self._write_minimal_config(base_dir)
            agent = ReviewAgent(
                JsonStateStore(base_dir / "state.json"),
                oppo_config_path=config_path,
            )

            stage = agent.handle_message("chat-1", "设置提交配置：submission.version_code=101")
            before = json.loads(config_path.read_text(encoding="utf-8"))
            confirm = agent.handle_message("chat-1", "确认保存配置")
            after = json.loads(config_path.read_text(encoding="utf-8"))

            self.assertIn("待保存配置修改", stage.text)
            self.assertEqual(before["submission"]["version_code"], "100")
            self.assertEqual(after["submission"]["version_code"], "101")
            self.assertIn("配置已保存", confirm.text)
            self.assertIn("提交检查", confirm.text)
            self.assertTrue(any((config_path.parent / "backups").iterdir()))

    def test_config_update_rejects_secret_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = self._write_minimal_config(Path(temp_dir))
            agent = ReviewAgent(
                JsonStateStore(Path(temp_dir) / "state.json"),
                oppo_config_path=config_path,
            )

            response = agent.handle_message("chat-1", "设置提交配置：credentials.client_secret=new-secret")

            self.assertIn("不允许通过飞书修改", response.text)
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(raw["credentials"]["client_secret"], "client-secret")

    def test_packaging_config_update_writes_separate_packaging_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            config_path = self._write_minimal_config(base)
            config_dir = base / "config"
            packaging_path = config_dir / "packaging.json"
            packaging_path.write_text(
                json.dumps(
                    {"packaging": {"script": "D:/old/package.js", "project_dir": "D:/proj"}},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            agent = ReviewAgent(
                JsonStateStore(base / "state.json"),
                oppo_config_path=config_path,
                packaging_config_path=packaging_path,
            )

            stage = agent.handle_message("chat-1", "设置提交配置：packaging.script=D:\\AutoReview\\package.js")
            confirm = agent.handle_message("chat-1", "确认保存配置")
            packaging = json.loads(packaging_path.read_text(encoding="utf-8"))

            self.assertIn("packaging.script", stage.text)
            self.assertEqual(packaging["packaging"]["script"], "D:\\AutoReview\\package.js")
            self.assertIn("配置已保存", confirm.text)

    def test_confirm_packaging_config_update_reloads_packaging_agent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            config_path = self._write_minimal_config(base)
            config_dir = base / "config"
            old_script = base / "old-package.js"
            new_script = base / "new-package.js"
            old_script.write_text("", encoding="utf-8")
            new_script.write_text("", encoding="utf-8")
            packaging_path = config_dir / "packaging.json"
            packaging_path.write_text(
                json.dumps(
                    {"packaging": {"script": str(old_script), "project_dir": str(base / "proj")}},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            agent = ReviewAgent(
                JsonStateStore(base / "state.json"),
                oppo_config_path=config_path,
                packaging_config_path=packaging_path,
            )

            agent.handle_message("chat-1", f"设置提交配置：packaging.script={new_script}")
            agent.handle_message("chat-1", "确认保存配置")

            self.assertEqual(agent.packaging_agent.settings.script_path, new_script.resolve())

    def test_natural_language_packaging_script_update_is_mapped_to_packaging_script(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            config_path = self._write_minimal_config(base)
            config_dir = base / "config"
            packaging_path = config_dir / "packaging.json"
            packaging_path.write_text(
                json.dumps(
                    {"packaging": {"script": "D:/old/package.js", "project_dir": "D:/proj"}},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            agent = ReviewAgent(
                JsonStateStore(base / "state.json"),
                oppo_config_path=config_path,
                packaging_config_path=packaging_path,
            )

            stage = agent.handle_message("chat-1", "路径改成D:\\AutoReview\\package.js，帮我把默认配置改一下。")

            self.assertIn("packaging.script", stage.text)
            session = agent.state_store.get_session("chat-1")
            self.assertEqual(session["pending_config_patch"]["packaging.script"], "D:\\AutoReview\\package.js")

            followup = agent.handle_message("chat-1", "我需要在那个文件里面修改？")
            self.assertIn("packaging.json", followup.text)
            self.assertIn("packaging.script", followup.text)

    def test_view_submission_config_includes_packaging_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            config_path = self._write_minimal_config(base)
            packaging_path = base / "config" / "packaging.json"
            packaging_path.write_text(
                json.dumps(
                    {"packaging": {"script": "D:\\AutoReview\\package.js", "project_dir": "D:\\Proj"}},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            agent = ReviewAgent(
                JsonStateStore(base / "state.json"),
                oppo_config_path=config_path,
                packaging_config_path=packaging_path,
            )

            response = agent.handle_message("chat-1", "查看提交配置")

            self.assertIn("当前打包配置（packaging.json）", response.text)
            self.assertIn("打包脚本：D:\\AutoReview\\package.js", response.text)

    def test_file_search_tool_finds_old_path_residue(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            config_path = self._write_minimal_config(base)
            project_dir = base / "project"
            project_dir.mkdir()
            (project_dir / "build.gradle").write_text(
                "scriptPath = 'D:\\development_sercer\\AutoReview\\package.js'\n",
                encoding="utf-8",
            )
            packaging_path = base / "config" / "packaging.json"
            packaging_path.write_text(
                json.dumps(
                    {"packaging": {"project_dir": str(project_dir), "script": str(base / "package.js")}},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            llm = FakeLlmClient(
                tool_call={
                    "tool": "file_search",
                    "confidence": 0.9,
                    "arguments": {"patterns": ["D:\\development_sercer\\AutoReview\\package.js"]},
                }
            )
            agent = ReviewAgent(
                JsonStateStore(base / "state.json"),
                oppo_config_path=config_path,
                packaging_config_path=packaging_path,
                llm_client=llm,
            )

            response = agent.handle_message("chat-1", "在项目里全文搜索 D:\\development_sercer\\AutoReview\\package.js")

            self.assertIn("全文搜索结果", response.text)
            self.assertIn("build.gradle", response.text)
            self.assertEqual(response.data["tool_call"]["tool"], "file_search")

    def test_batch_config_update_supports_pic_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            config_path = self._write_minimal_config(base_dir)
            agent = ReviewAgent(
                JsonStateStore(base_dir / "state.json"),
                oppo_config_path=config_path,
            )
            payload = (
                "批量设置提交配置："
                '{"submission":{"pic_url":[{"path":"../assets/a.png"},{"path":"../assets/b.png"}]}}'
            )

            stage = agent.handle_message("chat-1", payload)
            agent.handle_message("chat-1", "确认保存配置")
            raw = json.loads(config_path.read_text(encoding="utf-8"))

            self.assertIn("submission.pic_url.0.path", stage.text)
            self.assertEqual(raw["submission"]["pic_url"][0]["path"], "../assets/a.png")
            self.assertEqual(raw["submission"]["pic_url"][1]["path"], "../assets/b.png")

    def test_material_index_request_stages_patch_until_confirmed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            config_path = self._write_minimal_config(base_dir)
            materials = base_dir / "materials"
            app_dir = materials / "语文" / "八年级" / "下册" / "智趣互娱"
            app_dir.mkdir(parents=True)
            (app_dir / "八年级语文下册软著.png").write_bytes(b"copyright")
            (app_dir / "八年级下册语文免责函oppo_01.jpg").write_bytes(b"letter")
            snapshot = base_dir / "packlist-scan.json"
            snapshot.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "result": [
                            {
                                "sheet": "CfgGameConfig",
                                "row": 56,
                                "channel": "xm1067",
                                "app_name": "八年级语文下册",
                                "pkg_name": "com.pelbs.book1067",
                                "version_code": "68",
                                "version_name": "3.1067.38.2",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            packaging_path = base_dir / "config" / "packaging.json"
            packaging_path.write_text(
                json.dumps(
                    {
                        "packaging": {
                            "packlist_scan_file": str(snapshot),
                            "materials_root": str(materials),
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            agent = ReviewAgent(
                JsonStateStore(base_dir / "state.json"),
                oppo_config_path=config_path,
                packaging_config_path=packaging_path,
            )

            stage = agent.handle_message("chat-1", "索引上架资源：com.pelbs.book1067")
            before = json.loads(config_path.read_text(encoding="utf-8"))
            session = agent.state_store.get_session("chat-1")
            confirm = agent.handle_message("chat-1", "确认保存配置")
            after = json.loads(config_path.read_text(encoding="utf-8"))

            self.assertIn("上架资源索引", stage.text)
            self.assertIn("确认保存配置", stage.text)
            self.assertEqual(before["submission"]["pkg_name"], "com.example.app")
            self.assertEqual(session["pending_config_patch"]["submission.pkg_name"], "com.pelbs.book1067")
            self.assertIn("八年级语文下册软著.png", session["pending_config_patch"]["submission.copyright_url.path"])
            self.assertEqual(after["submission"]["pkg_name"], "com.pelbs.book1067")
            self.assertIn("八年级语文下册软著.png", after["submission"]["copyright_url"]["path"])
            self.assertIn("配置已保存", confirm.text)

    def test_bind_last_upload_as_apk_copies_file_and_updates_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            config_path = self._write_minimal_config(base_dir)
            upload_path = base_dir / "upload.apk"
            upload_path.write_bytes(b"apk")
            state = JsonStateStore(base_dir / "state.json")
            state.update_session(
                "chat-1",
                {
                    "last_upload": {
                        "path": str(upload_path),
                        "file_name": "upload.apk",
                        "resource_type": "file",
                    }
                },
            )
            agent = ReviewAgent(state, oppo_config_path=config_path)

            response = agent.handle_message("chat-1", "绑定材料：APK")
            raw = json.loads(config_path.read_text(encoding="utf-8"))

            self.assertIn("材料已绑定", response.text)
            self.assertEqual(raw["submission"]["apk_url"]["path"], "../release/app-release.apk")
            self.assertEqual((base_dir / "release" / "app-release.apk").read_bytes(), b"apk")

    def test_bind_material_requires_upload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            config_path = self._write_minimal_config(base_dir)
            agent = ReviewAgent(JsonStateStore(base_dir / "state.json"), oppo_config_path=config_path)

            response = agent.handle_message("chat-1", "绑定材料：图标")

            self.assertIn("还没有可绑定", response.text)

    def test_file_event_saves_last_upload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            config_path = self._write_minimal_config(base_dir)
            config = FeishuConfig(
                app_id="app-id",
                app_secret="app-secret",
                config_path=config_path,
                state_path=base_dir / "state.json",
            )
            app = FeishuWebhookApp(config)
            fake_client = FakeFeishuClient()
            app.client = fake_client

            response = app.handle_message_event(
                {
                    "message_id": "om_1",
                    "chat_id": "chat-1",
                    "sender_id": "user-1",
                    "message_type": "file",
                    "file_key": "file_1",
                    "file_name": "app-release.apk",
                }
            )
            session = app.agent.state_store.get_session("chat-1")

            self.assertEqual(response["code"], 0)
            self.assertEqual(fake_client.resource_calls[0], ("om_1", "file_1", "file"))
            self.assertTrue(Path(session["last_upload"]["path"]).exists())
            self.assertIn("绑定材料", fake_client.replies[0][1])

    def test_group_text_without_bot_mention_is_ignored(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            config_path = self._write_minimal_config(base_dir)
            app = FeishuWebhookApp(
                FeishuConfig(
                    app_id="cli_bot",
                    app_secret="app-secret",
                    config_path=config_path,
                    state_path=base_dir / "state.json",
                )
            )
            fake_client = FakeFeishuClient()
            app.client = fake_client

            response = app.handle_message_event(
                {
                    "message_id": "om_1",
                    "chat_id": "chat-1",
                    "chat_type": "group",
                    "sender_id": "user-1",
                    "message_type": "text",
                    "text": "帮助",
                }
            )

            self.assertEqual(response["message"], "ignored: group message without bot mention")
            self.assertEqual(fake_client.replies, [])

    def test_group_text_with_bot_mention_replies(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            config_path = self._write_minimal_config(base_dir)
            app = FeishuWebhookApp(
                FeishuConfig(
                    app_id="cli_bot",
                    app_secret="app-secret",
                    config_path=config_path,
                    state_path=base_dir / "state.json",
                )
            )
            fake_client = FakeFeishuClient()
            app.client = fake_client

            response = app.handle_message_event(
                {
                    "message_id": "om_1",
                    "chat_id": "chat-1",
                    "chat_type": "group",
                    "sender_id": "user-1",
                    "message_type": "text",
                    "text": "帮助@提交助手",
                }
            )

            self.assertEqual(response["code"], 0)
            self.assertEqual(len(fake_client.replies), 1)
            self.assertIn("我可以协助", fake_client.replies[0][1])

    def test_feishu_config_ignores_environment_fallbacks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "oppo_submission.json"
            config_path.write_text(
                json.dumps(
                    {
                        "feishu": {
                            "app_id": "app-id",
                            "app_secret": "app-secret",
                        }
                    }
                ),
                encoding="utf-8",
            )

            with patch.dict(
                "os.environ",
                {
                    "AUTOREVIEW_IMAGE2_URL": "http://env/image2",
                    "AUTOREVIEW_OCR_URL": "http://env/ocr",
                    "AUTOREVIEW_OCR_API_KEY": "env-key",
                    "OCR_PROXY_API_KEY": "proxy-key",
                    "AUTOREVIEW_IMAGE_ANALYSIS_TIMEOUT_SECONDS": "9",
                },
            ):
                config = FeishuConfig.from_file(config_path)

            self.assertEqual(config.image2_url, "")
            self.assertEqual(config.ocr_url, "")
            self.assertEqual(config.ocr_api_key, "")
            self.assertEqual(config.image_analysis_timeout_seconds, 120)

    def test_feishu_config_loads_llm_block(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "oppo_submission.json"
            llm_path = Path(temp_dir) / "llm_config.json"
            llm_path.write_text(
                json.dumps(
                    {
                        "enabled": True,
                        "base_url": "https://llm.example/v1",
                        "api_key": "secret",
                        "model": "model-a",
                    }
                ),
                encoding="utf-8",
            )
            config_path.write_text(
                json.dumps(
                    {
                        "feishu": {
                            "app_id": "app-id",
                            "app_secret": "app-secret",
                        },
                        "llm_config_path": "llm_config.json",
                    }
                ),
                encoding="utf-8",
            )

            config = FeishuConfig.from_file(config_path)

            self.assertEqual(config.llm["enabled"], True)
            self.assertEqual(config.llm["model"], "model-a")

    def test_feishu_config_inline_llm_overrides_shared_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "oppo_submission.json"
            llm_path = Path(temp_dir) / "llm_config.json"
            llm_path.write_text(
                json.dumps({"enabled": False, "model": "shared-model"}),
                encoding="utf-8",
            )
            config_path.write_text(
                json.dumps(
                    {
                        "feishu": {
                            "app_id": "app-id",
                            "app_secret": "app-secret",
                        },
                        "llm_config_path": "llm_config.json",
                        "llm": {"enabled": True, "model": "inline-model"},
                    }
                ),
                encoding="utf-8",
            )

            config = FeishuConfig.from_file(config_path)

            self.assertEqual(config.llm["enabled"], True)
            self.assertEqual(config.llm["model"], "inline-model")

    def test_feishu_config_defaults_market_data_path_next_to_main_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "oppo_submission.json"
            config_path.write_text(
                json.dumps(
                    {
                        "feishu": {
                            "app_id": "app-id",
                            "app_secret": "app-secret",
                        }
                    }
                ),
                encoding="utf-8",
            )

            config = FeishuConfig.from_file(config_path)

            self.assertEqual(config.market_data_config_path, Path(temp_dir) / "market_data.json")

    def test_feishu_config_supports_explicit_market_data_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "oppo_submission.json"
            config_path.write_text(
                json.dumps(
                    {
                        "feishu": {
                            "app_id": "app-id",
                            "app_secret": "app-secret",
                        },
                        "market_data_config_path": "config/market_data.custom.json",
                    }
                ),
                encoding="utf-8",
            )

            config = FeishuConfig.from_file(config_path)

            self.assertEqual(config.market_data_config_path, Path(temp_dir) / "config" / "market_data.custom.json")

    def test_feishu_config_defaults_packaging_path_next_to_main_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "oppo_submission.json"
            config_path.write_text(
                json.dumps(
                    {
                        "feishu": {
                            "app_id": "app-id",
                            "app_secret": "app-secret",
                        }
                    }
                ),
                encoding="utf-8",
            )

            config = FeishuConfig.from_file(config_path)

            self.assertEqual(config.packaging_config_path, Path(temp_dir) / "packaging.json")

    def test_feishu_config_supports_explicit_packaging_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "oppo_submission.json"
            config_path.write_text(
                json.dumps(
                    {
                        "feishu": {
                            "app_id": "app-id",
                            "app_secret": "app-secret",
                        },
                        "packaging_config_path": "shared/packaging.json",
                    }
                ),
                encoding="utf-8",
            )

            config = FeishuConfig.from_file(config_path)

            self.assertEqual(config.packaging_config_path, Path(temp_dir) / "shared" / "packaging.json")

    @staticmethod
    def _write_minimal_config(base_dir: Path) -> Path:
        config_dir = base_dir / "config"
        config_dir.mkdir()
        (config_dir / "llm_config.json").write_text(
            json.dumps(
                {
                    "enabled": True,
                    "base_url": "https://llm.example/v1",
                    "api_key": "llm-secret",
                    "model": "test-model",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        config_path = config_dir / "oppo_submission.json"
        config_path.write_text(
            json.dumps(
                {
                    "credentials": {
                        "client_id": "client-id",
                        "client_secret": "client-secret",
                    },
                    "feishu": {
                        "app_id": "feishu-app",
                        "app_secret": "feishu-secret",
                        "image_analysis": {
                            "ocr_url": "http://127.0.0.1:5000/ocr",
                            "ocr_api_key": "ocr-secret",
                        },
                    },
                    "llm_config_path": "llm_config.json",
                    "submission": {
                        "pkg_name": "com.example.app",
                        "version_code": "100",
                        "version_name": "1.0.0",
                        "apk_url": {"path": "../release/app-release.apk", "cpu_code": 0},
                        "app_name": "示例应用",
                        "icon_url": {"path": "../assets/icon.png"},
                        "pic_url": [{"path": "../assets/screenshot-1.png"}],
                        "copyright_url": {"path": "../assets/copyright.pdf"},
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return config_path


if __name__ == "__main__":
    unittest.main()
