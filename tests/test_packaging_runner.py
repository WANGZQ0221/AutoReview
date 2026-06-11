from pathlib import Path
import tempfile
import unittest

from autoreview.packaging.runner import (
    PackageError,
    load_package_jobs,
    make_package_job,
    run_package_job,
)
from autoreview.packaging.packlist import (
    require_single_package_channel,
    resolve_packlist_package,
    scan_packlist,
)


class PackagingRunnerTest(unittest.TestCase):
    def test_package_job_dry_run_writes_plan_without_packconfig(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            project_dir = self._make_project(base_dir)
            script_path = base_dir / "package.js"
            script_path.write_text("runStartProcess();\n", encoding="utf-8")
            job = make_package_job(
                project_dir=project_dir,
                channels=["book1400", "book1401"],
                script_path=script_path,
            )

            result = run_package_job(job, dry_run=True)

            self.assertEqual(result["channels"], ["book1400", "book1401"])
            self.assertEqual(result["packconfig"], "book1400 book1401")
            self.assertEqual((project_dir / "packconfig.txt").exists(), False)
            copied_script = project_dir / "autoreview_package.js"
            self.assertTrue(copied_script.exists())
            self.assertIn("跳过 start.bat", copied_script.read_text(encoding="utf-8"))

    def test_load_package_jobs_from_batch_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            batch_path = base_dir / "package_batch.json"
            batch_path.write_text(
                """
                {
                  "defaults": {
                    "project_dir": "android-project",
                    "script": "package.js"
                  },
                  "items": [
                    {"name": "a", "channels": ["book1400"]},
                    {"name": "b", "channels": "book1401 book1402"}
                  ]
                }
                """,
                encoding="utf-8",
            )

            jobs = load_package_jobs(batch_path, base_dir / "fallback.js")

            self.assertEqual(len(jobs), 2)
            self.assertEqual(jobs[0].channels, ["book1400"])
            self.assertEqual(jobs[1].channels, ["book1401", "book1402"])
            self.assertEqual(jobs[0].project_dir, (base_dir / "android-project").resolve())

    def test_package_job_validates_required_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            script_path = base_dir / "package.js"
            script_path.write_text("", encoding="utf-8")
            project_dir = base_dir / "android-project"
            project_dir.mkdir()
            job = make_package_job(
                project_dir=project_dir,
                channels=["book1400"],
                script_path=script_path,
            )

            with self.assertRaises(PackageError):
                run_package_job(job, dry_run=True)

    def test_scan_packlist_reads_channel_entries(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = self._make_project(Path(temp_dir))
            self._write_text_packlist(project_dir)

            entries = scan_packlist(project_dir)

            self.assertEqual(len(entries), 2)
            self.assertEqual(entries[0].channel, "xm1003")
            self.assertEqual(entries[0].pkg_name, "com.pelbs.book1003")
            self.assertEqual(entries[1].version_name, "3.1016.34.2")

    def test_resolve_packlist_package_finds_matching_channel(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = self._make_project(Path(temp_dir))
            self._write_text_packlist(project_dir)

            matches = resolve_packlist_package(project_dir, "com.pelbs.book1016")
            entry = require_single_package_channel(project_dir, "com.pelbs.book1016")

            self.assertEqual(len(matches), 1)
            self.assertEqual(entry.channel, "xm1016")
            self.assertEqual(entry.app_name, "四年级英语点读")

    def test_require_single_package_channel_errors_when_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = self._make_project(Path(temp_dir))
            self._write_text_packlist(project_dir)

            with self.assertRaises(PackageError):
                require_single_package_channel(project_dir, "com.pelbs.book43")

    @staticmethod
    def _make_project(base_dir: Path) -> Path:
        project_dir = base_dir / "android-project"
        (project_dir / "app").mkdir(parents=True)
        (project_dir / "app" / "build.gradle").write_text(
            "android { productFlavors {} buildTypes {} }",
            encoding="utf-8",
        )
        (project_dir / "packlist.xls").write_bytes(b"xls")
        (project_dir / "jksconfig.txt").write_text("signing=value", encoding="utf-8")
        return project_dir

    @staticmethod
    def _write_text_packlist(project_dir: Path) -> None:
        rows = [
            ["h0"],
            ["h1"],
            ["h2"],
            ["", "四年级英语上册", "xm1003", "wx", "com.pelbs.book1003", "", "", "64", "", "", "3.1003.34.2"],
            ["", "四年级英语点读", "xm1016", "wx", "com.pelbs.book1016", "", "", "64", "", "", "3.1016.34.2"],
        ]
        project_dir.joinpath("packlist.xls").write_text(
            "\n".join("\t".join(row) for row in rows),
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
