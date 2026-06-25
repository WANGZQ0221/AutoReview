from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from autoreview.materials.indexer import MaterialIndexError, suggest_submission_materials


class MaterialIndexerTest(unittest.TestCase):
    def test_suggest_submission_materials_by_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            materials = root / "materials"
            app_dir = materials / "英语" / "八年级英语下册"
            (app_dir / "android").mkdir(parents=True)
            (app_dir / "截图" / "1080").mkdir(parents=True)
            (app_dir / "上架材料").mkdir(parents=True)
            (app_dir / "android" / "playstore-icon.png").write_bytes(b"icon")
            (app_dir / "截图" / "1080" / "screenshot-1.png").write_bytes(b"s1")
            (app_dir / "截图" / "1080" / "screenshot-2.png").write_bytes(b"s2")
            (app_dir / "上架材料" / "八年级英语下册软著.pdf").write_bytes(b"copyright")
            (app_dir / "上架材料" / "八年级英语下册ICP备案.png").write_bytes(b"icp")
            (app_dir / "上架材料" / "八年级英语下册免责函小米.png").write_bytes(b"xiaomi")
            (app_dir / "上架材料" / "八年级英语下册免责函oppo.png").write_bytes(b"oppo")

            snapshot = root / "packlist-scan.json"
            snapshot.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "result": [
                            {
                                "sheet": "CfgGameConfig",
                                "row": 1,
                                "channel": "xm1067",
                                "app_name": "八年级英语下册",
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

            config = root / "config" / "oppo_submission.json"
            config.parent.mkdir()
            suggestion = suggest_submission_materials(
                root=materials,
                pkg_name="com.pelbs.book1067",
                packlist_snapshot=snapshot,
                config_path=config,
            )

            self.assertEqual(suggestion.app["app_name"], "八年级英语下册")
            self.assertEqual(suggestion.patch["submission.pkg_name"], "com.pelbs.book1067")
            self.assertEqual(suggestion.patch["submission.version_code"], "68")
            self.assertIn("playstore-icon.png", suggestion.patch["submission.icon_url.path"])
            self.assertIn("screenshot-1.png", suggestion.patch["submission.pic_url.0.path"])
            self.assertIn("软著.pdf", suggestion.patch["submission.copyright_url.path"])
            self.assertIn("ICP备案.png", suggestion.patch["submission.icp_url.path"])
            self.assertIn("免责函oppo.png", suggestion.patch["submission.special_url.0.path"])
            self.assertNotIn("submission.special_url.1.path", suggestion.patch)
            self.assertFalse(suggestion.warnings)

    def test_packlist_res_path_icons_android_uses_material_root_siblings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            materials = root / "materials"
            app_dir = materials / "英语" / "一年级" / "上册"
            icon_dir = app_dir / "icons" / "android"
            screenshot_dir = app_dir / "截图"
            material_dir = app_dir / "智趣点读"
            icon_dir.mkdir(parents=True)
            screenshot_dir.mkdir(parents=True)
            material_dir.mkdir(parents=True)
            (icon_dir / "playstore-icon.png").write_bytes(b"icon")
            (screenshot_dir / "screen-1.jpg").write_bytes(b"s1")
            (screenshot_dir / "screen-2.jpg").write_bytes(b"s2")
            (material_dir / "小学三年级下册英语免责函oppo.docx").write_bytes(b"wrong")
            (app_dir / "1000一年级英语上册免责函oppo.png").write_bytes(b"right")

            snapshot = root / "packlist-scan.json"
            snapshot.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "result": [
                            {
                                "sheet": "CfgGameConfig",
                                "row": 4,
                                "channel": "xm1000",
                                "app_name": "一年级英语上册",
                                "pkg_name": "com.pelbs.book1000",
                                "version_code": "68",
                                "version_name": "3.1000.38.2",
                                "res_path": str(icon_dir),
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            suggestion = suggest_submission_materials(
                root=materials,
                pkg_name="com.pelbs.book1000",
                packlist_snapshot=snapshot,
                config_path=root / "config" / "oppo_submission.json",
            )

            self.assertIn("icons", suggestion.patch["submission.icon_url.path"])
            self.assertIn("screen-1.jpg", suggestion.patch["submission.pic_url.0.path"])
            self.assertIn("screen-2.jpg", suggestion.patch["submission.pic_url.1.path"])
            self.assertIn("1000一年级英语上册免责函oppo.png", suggestion.patch["submission.special_url.0.path"])
            self.assertNotIn("未匹配到截图", suggestion.warnings)

    def test_requires_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(MaterialIndexError):
                suggest_submission_materials(root=tmp)


if __name__ == "__main__":
    unittest.main()
