"""High-level OPPO submission workflow orchestration."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path
import time
from typing import Any, Callable

from .client import OppoApiClient
from .config import OppoSubmissionConfig
from .errors import OppoApiError, OppoConfigError, OppoReviewTimeout
from .rejection import analyze_rejection_reason


JsonDict = dict[str, Any]
LogFn = Callable[[str], None]

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
APK_EXTENSIONS = {".apk", ".aab"}
RESOURCE_EXTENSIONS = {".pdf", ".zip", ".rar", ".mp4", ".doc", ".docx"}

RELEASE_REQUIRED_FIELDS = {
    "pkg_name",
    "version_code",
    "apk_url",
    "app_name",
    "second_category_id",
    "third_category_id",
    "summary",
    "detail_desc",
    "update_desc",
    "privacy_source_url",
    "icon_url",
    "pic_url",
    "online_type",
    "test_desc",
    "copyright_url",
    "business_username",
    "business_email",
    "business_mobile",
    "age_level",
    "adaptive_equipment",
}

LOCAL_ONLY_FIELDS = {
    "last_rejection_reason",
}

TASK_SUCCESS = "2"
TASK_FAILED = "3"
TASK_PENDING = "1"

FAILED_REVIEW_KEYWORDS = (
    "不通过",
    "拒绝",
    "驳回",
    "打回",
    "失败",
    "冻结",
)
PASSED_REVIEW_KEYWORDS = ("通过", "上架", "上线", "发布成功", "已发布")

REMOTE_REUSABLE_FIELDS = (
    "app_name",
    "second_category_id",
    "third_category_id",
    "summary",
    "detail_desc",
    "update_desc",
    "privacy_source_url",
    "icon_url",
    "pic_url",
    "landscape_pic_url",
    "copyright_url",
    "special_url",
    "special_file_url",
    "online_type",
    "test_desc",
    "business_username",
    "business_email",
    "business_mobile",
    "age_level",
    "adaptive_equipment",
)

REMOTE_FIELD_FALLBACKS = {
    "second_category_id": ("ver_second_category_id",),
    "third_category_id": ("ver_third_category_id",),
}


@dataclass(frozen=True)
class UploadResult:
    url: str
    md5: str | None
    raw: JsonDict


class OppoSubmissionAgent:
    def __init__(
        self,
        config: OppoSubmissionConfig,
        *,
        client: OppoApiClient | None = None,
        logger: LogFn | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.config = config
        self.client = client or OppoApiClient(
            client_id=config.client_id,
            client_secret=config.client_secret,
            settings=config.api,
        )
        self.logger = logger or (lambda message: None)
        self.sleep = sleep

    def validate(self) -> JsonDict:
        submission = self.config.resolved_submission()
        missing = sorted(field for field in RELEASE_REQUIRED_FIELDS if not submission.get(field))
        file_errors = list(self._iter_missing_files(submission))
        return {
            "valid": not missing and not file_errors,
            "missing_required_fields": missing,
            "missing_files": file_errors,
        }

    def config_reusing_remote_materials(self, version_code: str | None = None) -> OppoSubmissionConfig:
        submission = self.config.resolved_submission()
        pkg_name = submission.get("pkg_name")
        version_code = version_code or submission.get("version_code")
        if not pkg_name:
            raise OppoConfigError("submission.pkg_name is required for remote material reuse")
        if not version_code:
            raise OppoConfigError("submission.version_code is required for remote material reuse")

        info = self.client.get_app_info(str(pkg_name), str(version_code))
        updated_submission = deepcopy(self.config.submission)
        for field_name in REMOTE_REUSABLE_FIELDS:
            value = _first_remote_value(info, field_name)
            if value not in (None, ""):
                updated_submission[field_name] = value
        return replace(self.config, submission=updated_submission)

    def submit(
        self,
        *,
        wait_task: bool = False,
        wait_review: bool = False,
        force: bool = False,
    ) -> JsonDict:
        validation = self.validate()
        if not validation["valid"]:
            raise OppoConfigError(f"Invalid OPPO submission config: {validation}")
        guard = self.review_resubmit_guard()
        if guard["blocked"] and not force:
            raise OppoConfigError(
                "OPPO resubmission blocked by rejection guard: "
                f"{guard['analysis']['blocking_reasons']}. Use --force only after the APK/materials are fixed."
            )

        params = self.prepare_release_params()
        self.logger("Submitting OPPO release request")
        release = self.client.release_version(params)
        result: JsonDict = {
            "release": release,
            "pkg_name": params["pkg_name"],
            "version_code": str(params["version_code"]),
        }

        if wait_task:
            result["task"] = self.wait_for_task(params["pkg_name"], str(params["version_code"]))
        if wait_review:
            result["review"] = self.wait_for_review(params["pkg_name"], str(params["version_code"]))
        return result

    def review_resubmit_guard(self) -> JsonDict:
        reason = self.config.submission.get("last_rejection_reason")
        if not reason:
            return {"blocked": False, "analysis": None}
        analysis = analyze_rejection_reason(str(reason))
        return {
            "blocked": not analysis["can_resubmit_same_apk"],
            "analysis": analysis,
        }

    def update_material(self, *, wait_task: bool = False) -> JsonDict:
        validation = self.validate()
        if validation["missing_files"]:
            raise OppoConfigError(f"Invalid OPPO submission config: {validation}")

        params = self.prepare_material_params()
        if not params.get("pkg_name") or not params.get("version_code"):
            raise OppoConfigError("submission.pkg_name and submission.version_code are required")
        self.logger("Submitting OPPO material update request")
        update = self.client.update_material(params)
        result: JsonDict = {
            "update": update,
            "pkg_name": params["pkg_name"],
            "version_code": str(params["version_code"]),
        }
        if wait_task:
            result["task"] = self.wait_for_task(params["pkg_name"], str(params["version_code"]))
        return result

    def status(self, version_code: str | None = None) -> JsonDict:
        submission = self.config.resolved_submission()
        pkg_name = submission.get("pkg_name")
        version_code = version_code or submission.get("version_code")
        if not pkg_name:
            raise OppoConfigError("submission.pkg_name is required for status checks")
        if not version_code:
            raise OppoConfigError("submission.version_code is required for status checks")

        task: JsonDict | None = None
        try:
            task = self.client.get_task_state(str(pkg_name), str(version_code))
        except OppoApiError as exc:
            task = {"error": str(exc), "payload": exc.payload}
        info = self.client.get_app_info(str(pkg_name), str(version_code))
        return {
            "pkg_name": str(pkg_name),
            "version_code": str(version_code),
            "task": task,
            "app_info": info,
            "review_state": classify_review_state(info),
        }

    def prepare_release_params(self) -> JsonDict:
        return self._prepare_params(include_apk=True)

    def prepare_material_params(self) -> JsonDict:
        params = self._prepare_params(include_apk=False)
        params.pop("apk_url", None)
        return params

    def wait_for_task(self, pkg_name: str, version_code: str) -> JsonDict:
        polling = self.config.polling
        deadline = time.monotonic() + polling.task_timeout_seconds
        while True:
            task = self.client.get_task_state(pkg_name, version_code)
            task_state = str(task.get("task_state", ""))
            self.logger(f"OPPO task_state={task_state or '<empty>'}")
            if task_state == TASK_SUCCESS:
                return task
            if task_state == TASK_FAILED:
                raise OppoApiError(f"OPPO task failed: {task.get('err_msg') or task}", payload=task)
            if time.monotonic() >= deadline:
                raise OppoReviewTimeout(f"OPPO task did not finish for {pkg_name}@{version_code}")
            self.sleep(polling.task_interval_seconds)

    def wait_for_review(self, pkg_name: str, version_code: str) -> JsonDict:
        polling = self.config.polling
        deadline = time.monotonic() + polling.review_timeout_seconds
        while True:
            info = self.client.get_app_info(pkg_name, version_code)
            state = classify_review_state(info)
            self.logger(
                "OPPO review_state="
                f"{state} audit_status_name={info.get('audit_status_name', '')}"
            )
            if state in ("approved", "published"):
                return {"state": state, "app_info": info}
            if state == "rejected":
                raise OppoApiError(
                    f"OPPO review rejected: {extract_rejection_reason(info)}",
                    payload=info,
                )
            if time.monotonic() >= deadline:
                raise OppoReviewTimeout(f"OPPO review did not finish for {pkg_name}@{version_code}")
            self.sleep(polling.review_interval_seconds)

    def _prepare_params(self, *, include_apk: bool) -> JsonDict:
        submission = self.config.resolved_submission()
        prepared: JsonDict = {}
        for field_name, value in submission.items():
            if field_name in LOCAL_ONLY_FIELDS:
                continue
            if not include_apk and field_name == "apk_url":
                continue
            prepared[field_name] = self._resolve_field(field_name, value)
        return prepared

    def _resolve_field(self, field_name: str, value: Any) -> Any:
        if field_name == "apk_url":
            return self._resolve_apk_info(value)
        if isinstance(value, list):
            resolved = [self._resolve_field_item(field_name, item) for item in value]
            if field_name.endswith("_url") and not field_name.endswith("_material"):
                return ",".join(str(item) for item in resolved)
            return resolved
        return self._resolve_field_item(field_name, value)

    def _resolve_field_item(self, field_name: str, value: Any) -> Any:
        if is_file_ref(value):
            upload = self._upload_ref(value, field_name)
            if value.get("as_material") or field_name.endswith("_material"):
                return upload.raw
            return upload.url
        if isinstance(value, dict):
            return {
                key: self._resolve_field_item(field_name, item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._resolve_field_item(field_name, item) for item in value]
        return value

    def _resolve_apk_info(self, value: Any) -> list[JsonDict]:
        apk_items = value if isinstance(value, list) else [value]
        resolved: list[JsonDict] = []
        for item in apk_items:
            if is_file_ref(item):
                upload = self._upload_ref(item, "apk_url")
                resolved.append(
                    {
                        "url": upload.url,
                        "md5": upload.md5 or upload.raw.get("md5", ""),
                        "cpu_code": int(item.get("cpu_code", 0)),
                    }
                )
            elif isinstance(item, dict):
                if not item.get("url"):
                    raise OppoConfigError("Each apk_url item must provide url or path")
                resolved.append(
                    {
                        "url": item["url"],
                        "md5": item.get("md5", ""),
                        "cpu_code": int(item.get("cpu_code", 0)),
                    }
                )
            else:
                raise OppoConfigError("submission.apk_url must be an object or list of objects")
        return resolved

    def _upload_ref(self, ref: JsonDict, field_name: str) -> UploadResult:
        path = Path(ref["path"])
        file_type = str(ref.get("type") or infer_file_type(path, field_name))
        self.logger(f"Uploading {field_name}: {path.name} as {file_type}")
        raw = self.client.upload_file(path, file_type)
        url = raw.get("url")
        if not url:
            raise OppoApiError(f"Upload for {path} did not return url", payload=raw)
        return UploadResult(url=str(url), md5=raw.get("md5"), raw=raw)

    def _iter_missing_files(self, value: Any):
        if is_file_ref(value):
            path = Path(value["path"])
            if not path.exists() or not path.is_file():
                yield str(path)
        elif isinstance(value, dict):
            for item in value.values():
                yield from self._iter_missing_files(item)
        elif isinstance(value, list):
            for item in value:
                yield from self._iter_missing_files(item)


def is_file_ref(value: Any) -> bool:
    return isinstance(value, dict) and isinstance(value.get("path"), Path)


def infer_file_type(path: Path, field_name: str) -> str:
    suffix = path.suffix.lower()
    if field_name == "apk_url" or suffix in APK_EXTENSIONS:
        return "apk"
    if suffix in RESOURCE_EXTENSIONS:
        return "resource"
    if suffix in PHOTO_EXTENSIONS or field_name in {
        "icon_url",
        "pic_url",
        "landscape_pic_url",
        "video_pic_url",
        "cover_url",
        "special_url",
        "copyright_url",
    }:
        return "photo"
    return "resource"


def classify_review_state(info: JsonDict) -> str:
    text = " ".join(
        str(info.get(key) or "")
        for key in (
            "audit_status_name",
            "refuse_reason",
            "business_refuse_reason",
            "refuse_advice",
            "freeze_reason",
        )
    )
    if any(keyword in text for keyword in FAILED_REVIEW_KEYWORDS):
        return "rejected"
    if str(info.get("state", "")) == "1" and any(
        keyword in text for keyword in PASSED_REVIEW_KEYWORDS
    ):
        return "published"
    if any(keyword in text for keyword in PASSED_REVIEW_KEYWORDS):
        return "approved"
    if info.get("audit_status") or info.get("audit_status_name"):
        return "reviewing"
    return "unknown"


def extract_rejection_reason(info: JsonDict) -> str:
    for key in ("refuse_reason", "business_refuse_reason", "refuse_advice", "freeze_reason"):
        if info.get(key):
            return str(info[key])
    return str(info.get("audit_status_name") or info)


def _first_remote_value(info: JsonDict, field_name: str) -> Any:
    if info.get(field_name) not in (None, ""):
        return info[field_name]
    for fallback in REMOTE_FIELD_FALLBACKS.get(field_name, ()):
        if info.get(fallback) not in (None, ""):
            return info[fallback]
    return None
