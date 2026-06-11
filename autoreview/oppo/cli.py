"""CLI for OPPO app-store submission automation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from .agent import OppoSubmissionAgent
from .batch import apply_submission_overrides, load_batch_jobs
from .config import OppoSubmissionConfig
from .errors import OppoError
from .rejection import analyze_rejection_reason
from autoreview.packaging.runner import (
    PackageError,
    load_package_jobs,
    make_package_job,
    run_package_job,
)
from autoreview.feishu.long_connection import run_long_connection
from autoreview.feishu.server import run_server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autoreview-oppo",
        description="Submit Android apps to the OPPO App Market through OPPO Open Platform APIs.",
    )
    parser.add_argument(
        "-c",
        "--config",
        default="config/oppo_submission.json",
        help="Path to OPPO submission JSON config.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Only print final JSON output.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("validate", help="Validate local config and file references.")
    subparsers.add_parser(
        "prepare",
        help="Upload referenced files, resolve OPPO URLs, and print release params without submitting.",
    )

    submit = subparsers.add_parser("submit", help="Upload files and submit a new OPPO version.")
    submit.add_argument(
        "--wait-task",
        action="store_true",
        help="Wait until OPPO finishes the API submission task.",
    )
    submit.add_argument(
        "--wait-review",
        action="store_true",
        help="Poll app info until review is approved, published, or rejected.",
    )
    submit.add_argument(
        "--force",
        action="store_true",
        help="Bypass rejection guard after APK/materials have been fixed.",
    )
    submit.add_argument(
        "--apk",
        help="Override submission.apk_url.path for a simple one-APK submission.",
    )
    submit.add_argument(
        "--version-code",
        help="Override submission.version_code for this submission.",
    )
    submit.add_argument(
        "--version-name",
        help="Override submission.version_name for this submission.",
    )
    submit.add_argument(
        "--reuse-remote-materials",
        action="store_true",
        help="Reuse existing OPPO app_info URLs/materials for fields not provided locally.",
    )
    submit.add_argument(
        "--reuse-version-code",
        help="Version code used to read existing OPPO app_info materials. Defaults to current submission.version_code.",
    )

    batch_submit = subparsers.add_parser(
        "batch-submit",
        help="Submit multiple APKs/configs from a batch JSON file.",
    )
    batch_submit.add_argument(
        "--batch-file",
        required=True,
        help="Path to batch JSON. Items may override config, apk, version_code, version_name.",
    )
    batch_submit.add_argument(
        "--wait-task",
        action="store_true",
        help="Wait until OPPO finishes each API submission task.",
    )
    batch_submit.add_argument(
        "--wait-review",
        action="store_true",
        help="Poll each app info until review is approved, published, or rejected.",
    )
    batch_submit.add_argument(
        "--force",
        action="store_true",
        help="Bypass rejection guard after APK/materials have been fixed.",
    )
    batch_submit.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue later jobs if one submission fails.",
    )
    batch_submit.add_argument(
        "--reuse-remote-materials",
        action="store_true",
        help="Reuse existing OPPO app_info URLs/materials for each batch item.",
    )

    package_apk = subparsers.add_parser(
        "package-apk",
        help="Run package.js to build one or more product flavors.",
    )
    package_apk.add_argument(
        "--project-dir",
        required=True,
        help="Android project directory containing packlist.xls, jksconfig.txt and app/build.gradle.",
    )
    package_apk.add_argument(
        "--channels",
        nargs="+",
        required=True,
        help="Product flavor/channel names to write into packconfig.txt.",
    )
    package_apk.add_argument(
        "--script",
        default="package.js",
        help="Path to legacy package.js. Defaults to package.js in current working directory.",
    )
    package_apk.add_argument("--node", default="node", help="Node.js command.")
    package_apk.add_argument(
        "--run-start",
        action="store_true",
        help="Allow package.js to run start.bat after packaging. Disabled by default.",
    )
    package_apk.add_argument(
        "--dry-run",
        action="store_true",
        help="Print packaging plan without running node/package.js.",
    )

    batch_package = subparsers.add_parser(
        "batch-package",
        help="Run multiple package.js jobs from a batch JSON file.",
    )
    batch_package.add_argument(
        "--batch-file",
        required=True,
        help="Path to package batch JSON.",
    )
    batch_package.add_argument(
        "--script",
        default="package.js",
        help="Default package.js path for batch items.",
    )
    batch_package.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue later package jobs if one job fails.",
    )
    batch_package.add_argument(
        "--dry-run",
        action="store_true",
        help="Print packaging plans without running node/package.js.",
    )

    material = subparsers.add_parser("material", help="Upload files and update OPPO app materials.")
    material.add_argument(
        "--wait-task",
        action="store_true",
        help="Wait until OPPO finishes the material update task.",
    )

    status = subparsers.add_parser("status", help="Query task state and app review status.")
    status.add_argument(
        "--version-code",
        help="Override submission.version_code for status checks.",
    )

    analyze = subparsers.add_parser(
        "analyze-rejection",
        help="Analyze an OPPO rejection reason and decide whether same-APK resubmission is safe.",
    )
    analyze.add_argument(
        "--reason",
        help="Rejection reason text from OPPO backend.",
    )
    analyze.add_argument(
        "--reason-file",
        help="Path to a UTF-8 text file containing OPPO rejection reason.",
    )

    feishu = subparsers.add_parser("serve-feishu", help="Run Feishu webhook server.")
    feishu.add_argument("--host", default="0.0.0.0", help="Webhook server bind host.")
    feishu.add_argument("--port", type=int, default=8080, help="Webhook server bind port.")

    feishu_ws = subparsers.add_parser(
        "serve-feishu-ws",
        help="Run Feishu long-connection event receiver.",
    )
    feishu_ws.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARN", "ERROR"],
        help="Feishu SDK log level.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logger = (lambda message: None) if args.quiet else (lambda message: print(message, file=sys.stderr))

    try:
        if args.command == "serve-feishu":
            run_server(args.config, host=args.host, port=args.port)
            return 0
        if args.command == "serve-feishu-ws":
            run_long_connection(args.config, log_level=args.log_level)
            return 0

        if args.command == "package-apk":
            output = _run_package_apk(args, logger)
            print(json.dumps({"ok": True, "result": output}, ensure_ascii=False, indent=2, default=str))
            return 0

        if args.command == "batch-package":
            output = _run_batch_package(args, logger)
            print(json.dumps({"ok": True, "result": output}, ensure_ascii=False, indent=2, default=str))
            return 0

        if args.command == "analyze-rejection":
            output = analyze_rejection_reason(_load_reason(args))
            print(json.dumps({"ok": True, "result": output}, ensure_ascii=False, indent=2, default=str))
            return 0

        if args.command == "batch-submit":
            output = _run_batch_submit(args, logger)
            print(json.dumps({"ok": True, "result": output}, ensure_ascii=False, indent=2, default=str))
            return 0

        config = OppoSubmissionConfig.from_file(args.config)
        config = _config_with_cli_overrides(config, args)
        agent = OppoSubmissionAgent(config, logger=logger)
        if args.command == "submit" and args.reuse_remote_materials:
            config = agent.config_reusing_remote_materials(args.reuse_version_code)
            agent = OppoSubmissionAgent(config, logger=logger)

        if args.command == "validate":
            output: Any = agent.validate()
        elif args.command == "prepare":
            output = agent.prepare_release_params()
        elif args.command == "submit":
            output = agent.submit(
                wait_task=args.wait_task,
                wait_review=args.wait_review,
                force=args.force,
            )
        elif args.command == "material":
            output = agent.update_material(wait_task=args.wait_task)
        elif args.command == "status":
            output = agent.status(args.version_code)
        else:
            parser.error(f"Unknown command: {args.command}")
            return 2
    except OppoError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except PackageError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1

    print(json.dumps({"ok": True, "result": output}, ensure_ascii=False, indent=2, default=str))
    return 0


def _config_with_cli_overrides(config: OppoSubmissionConfig, args: argparse.Namespace) -> OppoSubmissionConfig:
    overrides: dict[str, Any] = {}
    if getattr(args, "apk", None):
        overrides["apk"] = args.apk
    if getattr(args, "version_code", None):
        overrides["version_code"] = args.version_code
    if getattr(args, "version_name", None):
        overrides["version_name"] = args.version_name
    return apply_submission_overrides(config, overrides, path_base=Path.cwd()) if overrides else config


def _run_batch_submit(args: argparse.Namespace, logger) -> list[dict[str, Any]]:
    jobs = load_batch_jobs(args.batch_file, args.config)
    results: list[dict[str, Any]] = []
    for index, job in enumerate(jobs, start=1):
        try:
            logger(f"Running batch job {index}/{len(jobs)}: {job.name}")
            config = OppoSubmissionConfig.from_file(job.config_path)
            config = apply_submission_overrides(config, job.overrides, path_base=job.path_base)
            agent = OppoSubmissionAgent(config, logger=logger)
            if args.reuse_remote_materials:
                agent = OppoSubmissionAgent(agent.config_reusing_remote_materials(), logger=logger)
            result = agent.submit(
                wait_task=args.wait_task,
                wait_review=args.wait_review,
                force=args.force,
            )
            results.append(
                {
                    "ok": True,
                    "name": job.name,
                    "config": str(job.config_path),
                    "result": result,
                }
            )
        except OppoError as exc:
            entry = {
                "ok": False,
                "name": job.name,
                "config": str(job.config_path),
                "error": str(exc),
            }
            results.append(entry)
            if not args.continue_on_error:
                raise OppoError(f"Batch job failed: {job.name}: {exc}") from exc
    return results


def _run_package_apk(args: argparse.Namespace, logger) -> dict[str, Any]:
    job = make_package_job(
        project_dir=args.project_dir,
        channels=args.channels,
        script_path=args.script,
        node_command=args.node,
        skip_start=not args.run_start,
    )
    return run_package_job(job, dry_run=args.dry_run, logger=logger)


def _run_batch_package(args: argparse.Namespace, logger) -> list[dict[str, Any]]:
    jobs = load_package_jobs(args.batch_file, args.script)
    results: list[dict[str, Any]] = []
    for index, job in enumerate(jobs, start=1):
        try:
            logger(f"Running package job {index}/{len(jobs)}: {job.name}")
            result = run_package_job(job, dry_run=args.dry_run, logger=logger)
            results.append({"ok": True, **result})
        except PackageError as exc:
            results.append({"ok": False, "name": job.name, "error": str(exc)})
            if not args.continue_on_error:
                raise
    return results


def _load_reason(args: argparse.Namespace) -> str:
    if args.reason:
        return args.reason
    if args.reason_file:
        with open(args.reason_file, "r", encoding="utf-8") as file_obj:
            return file_obj.read()
    raise OppoError("analyze-rejection requires --reason or --reason-file")


if __name__ == "__main__":
    raise SystemExit(main())
