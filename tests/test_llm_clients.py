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

    def test_openclaw_prompt_argument_is_compacted_to_single_line(self):
        completed = subprocess.CompletedProcess(
            args=["openclaw", "agent", "--message", "hello"],
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
            client.choose_tool("第一行\n第二行\t第三行", {"session": {}}, [])

        sent_prompt = run.call_args.args[0][3]
        self.assertIn("第一行", sent_prompt)
        self.assertIn("第二行", sent_prompt)
        self.assertIn("第三行", sent_prompt)
        self.assertNotIn("\n", sent_prompt)
        self.assertNotIn("\t", sent_prompt)

    def test_interpret_prompt_contains_layered_autoreview_rules(self):
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
                "openclaw": {"command": "openclaw", "args": ["agent", "--message", "{prompt}"]},
            }
        )
        client = OpenClawLlmClient(config)

        with patch("autoreview.agent.llm.subprocess.run", return_value=completed) as run:
            client.interpret("帮我看一下你的配置，记忆怎么处理的？", {"session": {}})

        sent_prompt = run.call_args.args[0][3]
        self.assertIn("工作方式", sent_prompt)
        self.assertIn("配置边界", sent_prompt)
        self.assertIn("记忆归属", sent_prompt)
        self.assertIn("config/packaging.json", sent_prompt)
        self.assertIn("不要假装存在外部 OpenClaw skill 文件", sent_prompt)

    def test_tool_prompt_treats_config_memory_skill_questions_as_no_tool(self):
        completed = subprocess.CompletedProcess(
            args=["openclaw", "agent", "--message", "hello"],
            returncode=0,
            stdout='{"tool":"none","confidence":0.9}',
            stderr="",
        )
        config = LlmConfig.from_mapping(
            {
                "enabled": True,
                "provider": "openclaw",
                "openclaw": {"command": "openclaw", "args": ["agent", "--message", "{prompt}"]},
            }
        )
        client = OpenClawLlmClient(config)

        with patch("autoreview.agent.llm.subprocess.run", return_value=completed) as run:
            client.choose_tool("各工具调用是怎么判断的？skill是怎么写的。", {"session": {}}, [])

        sent_prompt = run.call_args.args[0][3]
        self.assertIn("调度原则", sent_prompt)
        self.assertIn("问记忆机制", sent_prompt)
        self.assertIn("不要选择 view_submission_config", sent_prompt)
        self.assertIn("记忆写入边界", sent_prompt)


if __name__ == "__main__":
    unittest.main()
