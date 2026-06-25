from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from autoreview.agent.config_editor import apply_config_patch_to_targets


class ConfigEditorTest(unittest.TestCase):
    def test_submission_patch_writes_shared_submission_when_not_locally_overridden(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = base / "oppo_submission.json"
            shared = base / "shared_submission.json"
            config.write_text(
                json.dumps(
                    {
                        "shared_submission_path": "shared_submission.json",
                        "submission": {"last_rejection_reason": "old rejection"},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            shared.write_text(
                json.dumps(
                    {
                        "submission": {
                            "pkg_name": "com.example.old",
                            "icon_url": "https://example.com/icon.png",
                            "pic_url": [
                                "https://example.com/s1.png",
                                "https://example.com/s2.png",
                            ],
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            apply_config_patch_to_targets(
                config,
                {
                    "submission.pkg_name": "com.example.new",
                    "submission.icon_url.path": "D:\\materials\\icon.png",
                    "submission.pic_url.0.path": "D:\\materials\\s1.png",
                    "submission.pic_url.1.path": "D:\\materials\\s2.png",
                    "submission.last_rejection_reason": "",
                },
            )

            main_raw = json.loads(config.read_text(encoding="utf-8"))
            shared_raw = json.loads(shared.read_text(encoding="utf-8"))

            self.assertEqual(main_raw["submission"]["last_rejection_reason"], "")
            self.assertNotIn("pkg_name", main_raw["submission"])
            self.assertEqual(shared_raw["submission"]["pkg_name"], "com.example.new")
            self.assertEqual(shared_raw["submission"]["icon_url"]["path"], "D:\\materials\\icon.png")
            self.assertEqual(shared_raw["submission"]["pic_url"][0]["path"], "D:\\materials\\s1.png")
            self.assertEqual(shared_raw["submission"]["pic_url"][1]["path"], "D:\\materials\\s2.png")


if __name__ == "__main__":
    unittest.main()
