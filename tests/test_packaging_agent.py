from pathlib import Path
import json
import tempfile
import unittest

from autoreview.packaging.agent import (
    PackagingAgent,
    format_batch_package_result,
    format_package_result,
    parse_package_request,
)


class PackagingAgentTest(unittest.TestCase):
    def test_parse_package_request(self):
        parsed = parse_package_request("打包 com.pelbs.book1067 dry-run")
        self.assertEqual(parsed["pkg_name"], "com.pelbs.book1067")
        self.assertTrue(parsed["dry_run"])

    def test_parse_package_request_app_name(self):
        parsed = parse_package_request("打包 八年级语文下册 dry-run")
        self.assertEqual(parsed["app_name"], "八年级语文下册")
        self.assertEqual(parsed["channels"], [])

    def test_package_agent_uses_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            project_dir = self._make_project(base)
            script_path = base / "package.js"
            script_path.write_text("runStartProcess();\n", encoding="utf-8")
            config_path = base / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "packaging": {
                            "project_dir": str(project_dir),
                            "script": str(script_path),
                            "batch_file": str(base / "package_batch.json"),
                            "node_command": "node",
                            "skip_start": True,
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            agent = PackagingAgent(config_path)
            result = agent.package_one(app_name="四年级英语点读", dry_run=True)

            self.assertEqual(result["channels"], ["xm1016"])
            self.assertIn("resolved_package", result)

    def test_formatters(self):
        single = format_package_result(
            {
                "project_dir": "D:/proj",
                "channels": ["xm1067"],
                "packconfig": "xm1067",
                "latest_apks": ["D:/proj/apk/out.apk"],
            }
        )
        batch = format_batch_package_result(
            [
                {"ok": True, "name": "a", "channels": ["xm1"]},
                {"ok": False, "name": "b", "error": "boom"},
            ]
        )
        self.assertIn("打包完成", single)
        self.assertIn("批量打包完成", batch)

    @staticmethod
    def _make_project(base_dir: Path) -> Path:
        project_dir = base_dir / "android-project"
        (project_dir / "app").mkdir(parents=True)
        (project_dir / "app" / "build.gradle").write_text("android {}", encoding="utf-8")
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
        (project_dir / "jksconfig.txt").write_text("signing=value", encoding="utf-8")
        return project_dir


if __name__ == "__main__":
    unittest.main()
