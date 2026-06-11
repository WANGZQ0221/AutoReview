from pathlib import Path
import json
import tempfile
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


class FakeLlmClient:
    def __init__(self, decision):
        self.decision = decision
        self.calls = []

    def interpret(self, message, session):
        self.calls.append((message, session))
        return dict(self.decision)


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
            self.assertIn("四级单词竞品", response.text)
            self.assertEqual(session["last_market_search"]["query"], "英语四级单词")
            self.assertIn("最近竞品搜索", status)

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

            self.assertIn("已记录", response.text)
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

            self.assertIn("请提供有效", response.text)
            self.assertNotIn("应用商店竞品搜索：。", response.text)

    def test_semantic_market_search_understands_natural_language(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            agent = ReviewAgent(
                JsonStateStore(Path(temp_dir) / "state.json"),
                market_searcher_factory=lambda: FakeMarketSearcher(),
            )

            response = agent.handle_message("chat-1", "帮我看看有哪些类似的背单词软件", "user-1")

            self.assertIn("应用商店竞品搜索", response.text)
            self.assertIn("背单词", response.text)

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

    def test_clear_current_session_state_removes_only_that_session(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = JsonStateStore(Path(temp_dir) / "state.json")
            state.update_session("chat-1", {"app_info": {"app_name": "A"}})
            state.update_session("chat-2", {"app_info": {"app_name": "B"}})
            agent = ReviewAgent(state)

            response = agent.handle_message("chat-1", "清空当前记录")

            self.assertIn("已清空当前会话记录", response.text)
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

            self.assertIn("已清空全部会话记录", response.text)
            self.assertEqual(raw["sessions"], {})

    def test_semantic_status_and_submission_check_requests_use_oppo_agent(self):
        fake_agent = FakeOppoWorkflowAgent()
        agent = ReviewAgent(
            JsonStateStore(Path(tempfile.mkdtemp()) / "state.json"),
            oppo_agent_factory=lambda: fake_agent,
        )

        audit = agent.handle_message("chat-1", "帮我查审核状态 200")
        check = agent.handle_message("chat-1", "帮我检查一下现在能不能提交")

        self.assertEqual(fake_agent.status_version_code, "200")
        self.assertIn("OPPO 审核状态", audit.text)
        self.assertIn("提交检查", check.text)

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
                    "memories": ["默认应用是英语四级单词"],
                    "reply": "我记住了，后续默认按英语四级单词处理。",
                }
            )
            agent = ReviewAgent(JsonStateStore(Path(temp_dir) / "state.json"), llm_client=llm)

            response = agent.handle_message("chat-1", "以后这个应用默认就按四级单词处理", "user-1")
            status = agent.handle_message("chat-1", "状态").text
            session = agent.state_store.get_session("chat-1")

            self.assertIn("我记住了", response.text)
            self.assertEqual(session["agent_memory"], ["默认应用是英语四级单词"])
            self.assertIn("长期记忆：1 条", status)

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

    def test_llm_is_not_called_when_rules_match(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            llm = FakeLlmClient({"intent": "chat", "confidence": 1, "reply": "should not be used"})
            agent = ReviewAgent(JsonStateStore(Path(temp_dir) / "state.json"), llm_client=llm)

            response = agent.handle_message("chat-1", "状态")

            self.assertIn("当前会话", response.text)
            self.assertEqual(llm.calls, [])

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
