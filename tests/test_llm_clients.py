import subprocess
import unittest
from unittest.mock import patch

from autoreview.agent.llm import LlmConfig, OpenClawLlmClient, build_llm_client


class LlmClientTest(unittest.TestCase):
    def test_openclaw_config_ready_without_api_key(self):
        config = LlmConfig.from_mapping(
            {
                "enabled": True,
                "provider": "openclaw",
                "model": "gpt-5.5",
                "openclaw": {"command": "openclaw", "args": ["run", "--stdin"]},
            }
        )

        self.assertTrue(config.ready)
        self.assertEqual(config.provider, "openclaw")
        self.assertEqual(config.api_key, "")

    def test_build_llm_client_selects_openclaw_provider(self):
        client = build_llm_client({"enabled": True, "provider": "openclaw"})

        self.assertIsInstance(client, OpenClawLlmClient)

    def test_openclaw_interpret_uses_command_stdout_json(self):
        completed = subprocess.CompletedProcess(
            args=["openclaw", "agent", "--message", "hello"],
            returncode=0,
            stdout='{"intent":"chat","confidence":0.9,"reply":"ok"}',
            stderr="",
        )
        config = LlmConfig.from_mapping(
            {
                "enabled": True,
                "provider": "openclaw",
                "model": "gpt-5.5",
                "openclaw": {"command": "openclaw", "args": ["agent", "--message", "{prompt}"]},
            }
        )
        client = OpenClawLlmClient(config)

        with patch("autoreview.agent.llm.subprocess.run", return_value=completed) as run:
            result = client.interpret("你好", {"session": {}})

        self.assertEqual(result["intent"], "chat")
        self.assertIsNone(run.call_args.kwargs["input"])
        self.assertEqual(run.call_args.args[0][:3], ["openclaw", "agent", "--message"])
        self.assertIn("你好", run.call_args.args[0][3])

    def test_openclaw_args_expand_model_placeholder(self):
        completed = subprocess.CompletedProcess(
            args=["openclaw", "agent", "--message", "帮助"],
            returncode=0,
            stdout='{"tool":"none","confidence":0.9}',
            stderr="",
        )
        config = LlmConfig.from_mapping(
            {
                "enabled": True,
                "provider": "openclaw",
                "model": "gpt-5.5",
                "openclaw": {"command": "openclaw", "args": ["agent", "--message", "{prompt}"]},
            }
        )
        client = OpenClawLlmClient(config)

        with patch("autoreview.agent.llm.subprocess.run", return_value=completed) as run:
            result = client.choose_tool("帮助", {"session": {}}, [])

        self.assertEqual(result["tool"], "none")
        self.assertEqual(run.call_args.args[0][:3], ["openclaw", "agent", "--message"])
        self.assertIn("帮助", run.call_args.args[0][3])

    def test_openclaw_json_wrapper_reply_is_unwrapped(self):
        completed = subprocess.CompletedProcess(
            args=["openclaw", "agent", "--json", "--message", "帮助"],
            returncode=0,
            stdout='{"reply":"{\\"tool\\":\\"none\\",\\"confidence\\":0.9}"}',
            stderr="",
        )
        config = LlmConfig.from_mapping(
            {
                "enabled": True,
                "provider": "openclaw",
                "model": "gpt-5.5",
                "openclaw": {"command": "openclaw", "args": ["agent", "--json", "--message", "{prompt}"]},
            }
        )
        client = OpenClawLlmClient(config)

        with patch("autoreview.agent.llm.subprocess.run", return_value=completed):
            result = client.choose_tool("帮助", {"session": {}}, [])

        self.assertEqual(result["tool"], "none")


if __name__ == "__main__":
    unittest.main()
