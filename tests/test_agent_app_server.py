from pathlib import Path
import tempfile
import unittest

from autoreview.agent_app.server import AgentApp
from autoreview.feishu.config import FeishuConfig


class AgentAppServerTest(unittest.TestCase):
    def test_message_endpoint_adapter_returns_agent_response(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = AgentApp(
                FeishuConfig(
                    app_id="",
                    app_secret="",
                    config_path=Path(temp_dir) / "oppo_submission.json",
                    state_path=Path(temp_dir) / "state.json",
                )
            )

            result = app.handle_message(
                {
                    "session_id": "agent-session-1",
                    "sender_id": "user-1",
                    "text": "帮助",
                }
            )

            self.assertTrue(result["ok"])
            self.assertIn("OPPO", result["response"])
            self.assertEqual(result["data"]["intent"], "help")

    def test_analyze_rejection_adapter_updates_session(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            app = AgentApp(
                FeishuConfig(
                    app_id="",
                    app_secret="",
                    config_path=Path(temp_dir) / "oppo_submission.json",
                    state_path=state_path,
                )
            )

            result = app.analyze_rejection(
                {
                    "session_id": "agent-session-1",
                    "text": "APK相似度0.92，请勿重复提交，请补充ICP备案网站",
                }
            )

            self.assertTrue(result["ok"])
            self.assertIn("不建议原包直接重提", result["response"])
            self.assertEqual(result["data"]["similarity_score"], 0.92)

    def test_tools_describes_http_tools(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = AgentApp(
                FeishuConfig(
                    app_id="",
                    app_secret="",
                    config_path=Path(temp_dir) / "oppo_submission.json",
                    state_path=Path(temp_dir) / "state.json",
                )
            )

            result = app.tools()

            self.assertTrue(result["ok"])
            names = {tool["name"] for tool in result["tools"]}
            self.assertIn("autoreview_message", names)
            self.assertIn("analyze_oppo_rejection", names)


if __name__ == "__main__":
    unittest.main()
