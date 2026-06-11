from pathlib import Path
import tempfile
import unittest

from autoreview.oppo.agent import OppoSubmissionAgent, classify_review_state
from autoreview.oppo.batch import apply_submission_overrides, load_batch_jobs
from autoreview.oppo.config import OppoSubmissionConfig
from autoreview.oppo.errors import OppoConfigError
from autoreview.oppo.rejection import analyze_rejection_reason


REJECTION_REASON = (
    "开发者您好，您的应用未通过原因如下："
    "1.商店暂不收录新增简单套用模板/马甲的APP，APP与“五年级英语上册”存在套用马甲的嫌疑"
    "（经检测APK相似度0.92，最高为1），请多进行创新，在充分改动之前请勿重复提交；"
    "2.请补充提供贵司自己与应用一致的有效ICP备案网站（备案要求：备案网站可正常访问，"
    "页面须有可查的此资源的信息，备案号在网页中显示）提交文字/图片/其他任何形式均可，"
    "上传到测试附加说明/版权证明/特殊类证书中的任意一个窗口；感谢您的支持与配合"
)


class FakeOppoClient:
    def __init__(self):
        self.uploads = []
        self.releases = []

    def upload_file(self, path, file_type):
        self.uploads.append((Path(path).name, file_type))
        return {
            "url": f"https://cdn.example.com/{Path(path).name}",
            "md5": "abc123",
            "raw_file_type": file_type,
        }

    def release_version(self, params):
        self.releases.append(params)
        return {"task_id": "task-1"}

    def get_task_state(self, pkg_name, version_code):
        return {"task_state": "2", "pkg_name": pkg_name, "version_code": version_code}

    def get_app_info(self, pkg_name, version_code=None):
        return {"audit_status_name": "审核通过", "state": "1"}


class FakeRemoteMaterialClient(FakeOppoClient):
    def get_app_info(self, pkg_name, version_code=None):
        return {
            "pkg_name": pkg_name,
            "version_code": version_code,
            "app_name": "远程应用名",
            "ver_second_category_id": "6761",
            "ver_third_category_id": "6666",
            "icon_url": "https://example.com/icon.png",
            "pic_url": "https://example.com/s1.png,https://example.com/s2.png",
            "copyright_url": "https://example.com/copyright.pdf",
            "summary": "远程简介",
            "detail_desc": "远程详情",
            "update_desc": "远程更新",
            "privacy_source_url": "https://example.com/privacy.html",
            "online_type": "1",
            "test_desc": "远程测试说明",
            "business_username": "李老师",
            "business_email": "dev@example.com",
            "business_mobile": "13800000000",
            "age_level": "3",
            "adaptive_equipment": "4",
        }


def build_config(base_dir: Path) -> OppoSubmissionConfig:
    return OppoSubmissionConfig(
        client_id="client-id",
        client_secret="secret",
        config_path=base_dir / "oppo.json",
        submission={
            "pkg_name": "com.example.app",
            "version_code": "100",
            "version_name": "1.0.0",
            "apk_url": {"path": "app.apk", "cpu_code": 0},
            "app_name": "示例应用",
            "second_category_id": "1",
            "third_category_id": "101",
            "summary": "简介",
            "detail_desc": "详情",
            "update_desc": "更新说明",
            "privacy_source_url": "https://example.com/privacy.html",
            "icon_url": {"path": "icon.png"},
            "pic_url": [{"path": "screenshot-1.png"}, {"path": "screenshot-2.png"}],
            "online_type": "1",
            "test_desc": "无需登录",
            "copyright_url": {"path": "copyright.pdf"},
            "business_username": "张三",
            "business_email": "dev@example.com",
            "business_mobile": "13800000000",
            "age_level": "3",
            "adaptive_equipment": "1",
        },
    )


class OppoSubmissionAgentTest(unittest.TestCase):
    def test_validate_and_prepare_release_params(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            for file_name in (
                "app.apk",
                "icon.png",
                "screenshot-1.png",
                "screenshot-2.png",
                "copyright.pdf",
            ):
                (base_dir / file_name).write_bytes(b"test")

            fake_client = FakeOppoClient()
            agent = OppoSubmissionAgent(build_config(base_dir), client=fake_client)

            self.assertEqual(agent.validate()["valid"], True)
            params = agent.prepare_release_params()

            self.assertEqual(params["apk_url"][0]["url"], "https://cdn.example.com/app.apk")
            self.assertEqual(params["apk_url"][0]["md5"], "abc123")
            self.assertEqual(params["icon_url"], "https://cdn.example.com/icon.png")
            self.assertEqual(
                params["pic_url"],
                "https://cdn.example.com/screenshot-1.png,"
                "https://cdn.example.com/screenshot-2.png",
            )
            self.assertIn(("app.apk", "apk"), fake_client.uploads)
            self.assertIn(("icon.png", "photo"), fake_client.uploads)
            self.assertIn(("copyright.pdf", "resource"), fake_client.uploads)

    def test_classify_review_state(self):
        self.assertEqual(classify_review_state({"audit_status_name": "审核通过"}), "approved")
        self.assertEqual(
            classify_review_state({"audit_status_name": "上线", "state": "1"}),
            "published",
        )
        self.assertEqual(
            classify_review_state({"audit_status_name": "审核不通过", "refuse_reason": "资质缺失"}),
            "rejected",
        )
        self.assertEqual(classify_review_state({"audit_status_name": "审核中"}), "reviewing")

    def test_rejection_analysis_blocks_same_apk_resubmission(self):
        analysis = analyze_rejection_reason(REJECTION_REASON)

        self.assertEqual(analysis["similarity_score"], 0.92)
        self.assertEqual(analysis["similar_app"], "五年级英语上册")
        self.assertEqual(analysis["can_resubmit_same_apk"], False)
        self.assertIn("apk_similarity_or_template", analysis["categories"])
        self.assertIn("missing_icp_proof", analysis["categories"])
        self.assertIn("OPPO backend: 版权证明", analysis["evidence_targets"])
        self.assertIn("充分修改 APK", analysis["required_actions"][0])

    def test_rejection_analysis_extracts_unquoted_similar_app(self):
        analysis = analyze_rejection_reason(
            "APP与五年级英语上册存在套用马甲的嫌疑，经检测APK相似度0.92，请勿重复提交"
        )

        self.assertEqual(analysis["similar_app"], "五年级英语上册")

    def test_submit_guard_blocks_known_similarity_rejection(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            for file_name in (
                "app.apk",
                "icon.png",
                "screenshot-1.png",
                "screenshot-2.png",
                "copyright.pdf",
            ):
                (base_dir / file_name).write_bytes(b"test")

            config = build_config(base_dir)
            config.submission["last_rejection_reason"] = REJECTION_REASON
            agent = OppoSubmissionAgent(config, client=FakeOppoClient())

            with self.assertRaises(OppoConfigError):
                agent.submit()

    def test_apply_submission_overrides_sets_apk_and_version_in_memory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            config = build_config(base_dir)
            apk_path = base_dir / "release" / "new.apk"

            updated = apply_submission_overrides(
                config,
                {
                    "apk": "release/new.apk",
                    "version_code": "101",
                    "version_name": "1.0.1",
                },
                path_base=base_dir,
            )

            self.assertEqual(updated.submission["version_code"], "101")
            self.assertEqual(updated.submission["version_name"], "1.0.1")
            self.assertEqual(updated.submission["apk_url"]["path"], str(apk_path.resolve()))
            self.assertEqual(config.submission["version_code"], "100")

    def test_load_batch_jobs_resolves_relative_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            batch_path = base_dir / "batch.json"
            batch_path.write_text(
                """
                {
                  "defaults": {"config": "config/base.json"},
                  "items": [
                    {"name": "app-a", "apk": "release/a.apk", "version_code": "101"},
                    {"name": "app-b", "config": "config/b.json", "apk": "release/b.apk"}
                  ]
                }
                """,
                encoding="utf-8",
            )

            jobs = load_batch_jobs(batch_path, base_dir / "fallback.json")

            self.assertEqual(len(jobs), 2)
            self.assertEqual(jobs[0].name, "app-a")
            self.assertEqual(jobs[0].config_path, (base_dir / "config" / "base.json").resolve())
            self.assertEqual(jobs[0].overrides["apk"], "release/a.apk")
            self.assertEqual(jobs[1].config_path, (base_dir / "config" / "b.json").resolve())

    def test_config_reusing_remote_materials_replaces_local_file_refs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            config = build_config(base_dir)
            agent = OppoSubmissionAgent(config, client=FakeRemoteMaterialClient())

            updated = agent.config_reusing_remote_materials()

            self.assertEqual(updated.submission["app_name"], "远程应用名")
            self.assertEqual(updated.submission["second_category_id"], "6761")
            self.assertEqual(updated.submission["third_category_id"], "6666")
            self.assertEqual(updated.submission["icon_url"], "https://example.com/icon.png")
            self.assertEqual(
                updated.submission["pic_url"],
                "https://example.com/s1.png,https://example.com/s2.png",
            )
            self.assertEqual(updated.submission["copyright_url"], "https://example.com/copyright.pdf")


if __name__ == "__main__":
    unittest.main()
