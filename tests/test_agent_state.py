from pathlib import Path
import json
import tempfile
import unittest

from autoreview.agent.state import JsonStateStore


class JsonStateStoreTest(unittest.TestCase):
    def test_conversation_turns_are_stored_as_jsonl_outside_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = JsonStateStore(Path(temp_dir) / "state.json")

            store.update_session("chat-1", {"app_info": {"app_name": "小学四年级英语"}})
            store.append_conversation_turn(
                "chat-1",
                {
                    "ts": 1,
                    "sender_id": "user-1",
                    "user": "搜索 OPPO 应用商店，查抖音下载量",
                    "assistant": "未找到精确匹配",
                    "intent": "market_search",
                },
            )

            state = json.loads((Path(temp_dir) / "state.json").read_text(encoding="utf-8"))
            session = store.get_session("chat-1")
            turns_path = Path(temp_dir) / "sessions" / "chat-1" / "turns.jsonl"

            self.assertTrue(turns_path.exists())
            self.assertNotIn("conversation_history", state["sessions"]["chat-1"])
            self.assertIn("抖音", session["conversation_history"][0]["user"])
            self.assertEqual(session["app_info"]["app_name"], "小学四年级英语")

    def test_get_conversation_history_keeps_recent_turns(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = JsonStateStore(Path(temp_dir) / "state.json")

            for index in range(25):
                store.append_conversation_turn(
                    "chat-1",
                    {"ts": index, "user": f"消息{index}", "assistant": "收到", "intent": "chat"},
                )

            history = store.get_conversation_history("chat-1", limit=20)

            self.assertEqual(len(history), 20)
            self.assertEqual(history[0]["user"], "消息5")
            self.assertEqual(history[-1]["user"], "消息24")


if __name__ == "__main__":
    unittest.main()
