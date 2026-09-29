from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from collections.abc import Callable
from concurrent.futures import Executor, Future, ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime
from multiprocessing import get_context
from pathlib import Path
from threading import Event, Lock
from typing import Annotated, Any, Literal
from uuid import uuid4

import pysubs2
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from starlette.exceptions import HTTPException as StarletteHTTPException

from video2txt import __version__
from video2txt.config import Settings, load_settings
from video2txt.models import ASRMode, FusionMode, TaskManifest, TaskProgress, TaskStatus
from video2txt.media import douyin
from video2txt.pipeline import TranscriptionPipeline, _write_text_atomic
from video2txt.translation.codex_cli import CodexCLIChineseTranslator

MEDIA_EXTENSIONS = {
    ".aac",
    ".avi",
    ".flac",
    ".m4a",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".ogg",
    ".wav",
    ".webm",
}
SUBTITLE_EXTENSIONS = {".ass", ".srt", ".ssa", ".vtt"}
DOWNLOAD_FILES = {
    "subtitles.srt",
    "translated_subtitles_zh.srt",
    "transcript.txt",
    "translated_transcript_zh.txt",
    "summary.md",
    "summary.txt",
}
EXPORT_FILES_BY_TYPE = {
    "text": "transcript.txt",
    "subtitle": "subtitles.srt",
    "translation": "translated_subtitles_zh.srt",
    "translation_text": "translated_transcript_zh.txt",
    "summary": "summary.md",
}
EXPORT_FILES_BY_KIND = {
    "all": (
        "transcript.txt",
        "subtitles.srt",
        "translated_subtitles_zh.srt",
        "translated_transcript_zh.txt",
    ),
    "subtitle": ("subtitles.srt",),
    "text": ("transcript.txt",),
}
ExportType = Literal["subtitle", "text", "translation", "translation_text", "summary"]
LegacyExportKind = Literal["all", "subtitle", "text"]
MAX_UPLOAD_GB = 5
MAX_UPLOAD_BYTES = MAX_UPLOAD_GB * 1024 * 1024 * 1024
MAX_BATCH_FILES = 50
CHUNK_SIZE = 1024 * 1024
SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
TERMINAL_STATUSES = {
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
}


def _model_display_name(model_path: Path | None) -> str | None:
    if model_path is None:
        return None
    for part in reversed(model_path.parts):
        prefix = "models--Systran--"
        if part.startswith(prefix):
            return part.removeprefix(prefix)
        if part.startswith("faster-whisper-"):
            return part
    return model_path.name


def _translation_models_available(settings: Settings) -> bool:
    model_dir = settings.translation.model_path
    return (model_dir / "model.bin").is_file() and (model_dir / "sentencepiece.bpe.model").is_file()


def _api_pipeline_available(settings: Settings) -> bool:
    return bool(
        os.getenv(settings.qwen_asr.api_key_environment, "").strip()
        and importlib.util.find_spec("requests")
        and shutil.which(settings.codex_translation.command)
    )


def _summary_available(settings: Settings) -> bool:
    try:
        CodexCLIChineseTranslator(settings.codex_translation, working_dir=Path(".")).ensure_available()
        return True
    except RuntimeError:
        return False


def _parse_api_hotwords(value: str) -> list[str]:
    words = list(dict.fromkeys(word.strip() for word in value.splitlines() if word.strip()))
    if len(words) > 2000:
        raise HTTPException(status_code=400, detail="API 热词最多 2000 个")
    if any(len(word) > 200 for word in words):
        raise HTTPException(status_code=400, detail="API 热词单条不能超过 200 个字符")
    return words


def _validate_id(value: str, label: str) -> str:
    if not SAFE_ID_PATTERN.fullmatch(value):
        raise HTTPException(status_code=404, detail=f"{label}不存在")
    return value


def _batch_manifest_path(settings: Settings, batch_id: str) -> Path:
    return settings.paths.work_dir.resolve() / "batches" / f"{batch_id}.json"


def _write_json_atomic(path: Path, payload: object) -> None:
    _write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _write_batch_manifest(settings: Settings, payload: dict[str, Any]) -> None:
    path = _batch_manifest_path(settings, payload["batch_id"])
    _write_json_atomic(path, payload)


def _task_job_path(settings: Settings, task_id: str) -> Path:
    return settings.paths.work_dir.resolve() / "queue" / f"{task_id}.json"


def _directory_size(path: Path) -> int:
    if not path.exists() or path.is_symlink():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file() and not child.is_symlink():
                total += child.stat().st_size
        except OSError:
            continue
    return total


def _remove_child_path(root: Path, target: Path) -> int:
    resolved_root = root.resolve()
    resolved_target = target.resolve()
    if resolved_target == resolved_root or resolved_root not in resolved_target.parents:
        raise RuntimeError(f"refusing to remove path outside managed root: {target}")
    size = _directory_size(resolved_target)
    if resolved_target.is_dir() and not resolved_target.is_symlink():
        shutil.rmtree(resolved_target)
    elif resolved_target.exists():
        resolved_target.unlink()
    return size


def _read_batch_manifest(settings: Settings, batch_id: str) -> dict[str, Any]:
    path = _batch_manifest_path(settings, _validate_id(batch_id, "批次"))
    if not path.is_file():
        raise HTTPException(status_code=404, detail="批次不存在")
    try:
        for attempt in range(5):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.02)
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=500, detail="批次记录损坏") from error
    if payload.get("batch_id") != batch_id or not isinstance(payload.get("task_ids"), list):
        raise HTTPException(status_code=500, detail="批次记录无效")
    return payload


def _media_stem(task: dict[str, Any]) -> str:
    source_name = Path(task.get("original_filename") or "task").stem
    safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", source_name).strip(" .")
    return safe_name or "task"


def _export_filename(task: dict[str, Any], internal_name: str) -> str:
    if internal_name in {"summary.md", "summary.txt"}:
        return f"{_media_stem(task)}.总结报告{Path(internal_name).suffix}"
    if internal_name == "translated_subtitles_zh.srt":
        return f"{_media_stem(task)}.zh.srt"
    if internal_name == "translated_transcript_zh.txt":
        return f"{_media_stem(task)}.zh.txt"
    suffix = ".srt" if internal_name == "subtitles.srt" else ".txt"
    return f"{_media_stem(task)}{suffix}"


def _backfill_translated_text(manifest: TaskManifest) -> None:
    """Create the Chinese TXT once for completed tasks made before this output existed."""
    target = manifest.output_dir / "translated_transcript_zh.txt"
    source = manifest.output_dir / "translated_subtitles_zh.srt"
    if target.is_file() or not source.is_file():
        return
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    try:
        subtitles = pysubs2.load(str(source), encoding="utf-8")
        content = "\n".join(
            event.plaintext.strip() for event in subtitles.events if event.plaintext.strip()
        )
        temporary.write_text(content + ("\n" if content else ""), encoding="utf-8")
        temporary.replace(target)
    except (OSError, UnicodeError, ValueError):
        temporary.unlink(missing_ok=True)


def _selected_export_files(
    types: list[ExportType] | None,
    legacy_kind: LegacyExportKind | None,
) -> tuple[str, ...]:
    if types:
        selected_types = set(types)
        return tuple(
            filename
            for export_type, filename in EXPORT_FILES_BY_TYPE.items()
            if export_type in selected_types
        )
    if legacy_kind is not None:
        return EXPORT_FILES_BY_KIND[legacy_kind]
    return (EXPORT_FILES_BY_TYPE["text"],)


class WebTaskRegistry:
    def __init__(self) -> None:
        self._lock = Lock()
        self._items: dict[str, dict[str, Any]] = {}

    def set(self, task_id: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self._items[task_id] = payload

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._items.get(task_id)
            return dict(item) if item is not None else None

    def remove(self, task_id: str) -> None:
        with self._lock:
            self._items.pop(task_id, None)


TaskRunner = Callable[[dict[str, Any], dict[str, Any]], str | None]


def _run_pipeline_job(
    settings_payload: dict[str, Any],
    job: dict[str, Any],
) -> str | None:
    """Run one persisted job in an isolated worker process."""
    settings = Settings.model_validate(settings_payload)
    subtitle_value = job.get("subtitle_path")
    subtitle_path = Path(str(subtitle_value)) if subtitle_value else None
    try:
        if job.get("summary_only"):
            manifest = TaskManifest.model_validate_json(
                (settings.paths.output_dir / str(job["task_id"]) / "task.json").read_text(encoding="utf-8")
            )
            TranscriptionPipeline(settings).summarize_existing(manifest)
            return None
        TranscriptionPipeline(settings).run(
            Path(str(job["media_path"])),
            output_dir=settings.paths.output_dir / str(job["task_id"]),
            mode=FusionMode(str(job["mode"])),
            external_subtitle=subtitle_path,
            task_id=str(job["task_id"]),
            original_filename=str(job["original_filename"]),
            batch_id=str(job["batch_id"]) if job.get("batch_id") else None,
            hard_subtitles=bool(job.get("hard_subtitles")),
            translate_to_chinese=bool(job.get("translate_to_chinese")),
            asr_mode=ASRMode(str(job.get("asr_mode") or ASRMode.LOCAL)),
            api_hotwords=[str(item) for item in (job.get("api_hotwords") or [])],
            source_url=job.get("source_url"),
            source_kind=job.get("source_kind"),
            recording_minutes=job.get("recording_minutes"),
            summarize=bool(job.get("summarize")),
        )
    except Exception as error:
        return str(error)
    return None


async def _save_upload(upload: UploadFile, target: Path) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with target.open("wb") as handle:
        while chunk := await upload.read(CHUNK_SIZE):
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                handle.close()
                target.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=413,
                    detail=f"上传文件超过 {MAX_UPLOAD_GB} GB 限制",
                )
            handle.write(chunk)
    await upload.close()
    if total == 0:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="上传文件为空")
    return total


def _manifest_payload(manifest: TaskManifest) -> dict[str, Any]:
    if manifest.status == TaskStatus.COMPLETED:
        _backfill_translated_text(manifest)
    payload = manifest.model_dump(mode="json")
    payload["source_media_available"] = bool(manifest.source_kind and manifest.input_path.is_file())
    if manifest.input_path.is_file():
        payload["media_size"] = manifest.input_path.stat().st_size
    payload["download_files"] = [
        name for name in sorted(DOWNLOAD_FILES) if (manifest.output_dir / name).is_file()
    ]
    transcript = manifest.output_dir / "transcript.txt"
    if transcript.is_file():
        payload["transcript_preview"] = transcript.read_text(encoding="utf-8")
    report = manifest.output_dir / "summary.md"
    if report.is_file():
        payload["summary_preview"] = report.read_text(encoding="utf-8")
    for probe_path in (
        manifest.output_dir / "probe.json",
        manifest.work_dir / "probe.json",
    ):
        if not probe_path.is_file():
            continue
        try:
            duration = json.loads(probe_path.read_text(encoding="utf-8")).get("duration")
            if isinstance(duration, int | float) and duration > 0:
                payload["media_duration"] = float(duration)
        except (OSError, ValueError):
            pass
        break
    return payload


def create_app(
    settings: Settings | None = None,
    *,
    task_executor: Executor | None = None,
    task_runner: TaskRunner = _run_pipeline_job,
) -> FastAPI:
    resolved_settings = settings or load_settings()
    static_dir = Path(__file__).with_name("static")
    multipart_temp_dir = resolved_settings.paths.work_dir.resolve() / "multipart"
    multipart_temp_dir.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(multipart_temp_dir)
    registry = WebTaskRegistry()
    executor = task_executor or ProcessPoolExecutor(
        max_workers=1,
        mp_context=get_context("spawn"),
    )
    settings_payload = resolved_settings.model_dump(mode="json")
    acquisition_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="douyin")
    live_executor = ThreadPoolExecutor(max_workers=douyin.MAX_LIVE_RECORDINGS, thread_name_prefix="douyin-live")
    batch_update_lock = Lock()
    shutdown_requested = Event()
    submitted_ids: set[str] = set()
    submission_lock = Lock()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):  # type: ignore[no-untyped-def]
        recover_pending_tasks()
        yield
        shutdown_requested.set()
        await asyncio.to_thread(acquisition_executor.shutdown, wait=True, cancel_futures=True)
        await asyncio.to_thread(live_executor.shutdown, wait=True, cancel_futures=True)
        executor.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title="Video2Txt", version=__version__, lifespan=lifespan)
    app.state.settings = resolved_settings
    app.state.registry = registry
    app.state.executor = executor
    app.state.multipart_temp_dir = multipart_temp_dir

    @app.exception_handler(StarletteHTTPException)
    async def friendly_http_error(
        _request: Request,
        error: StarletteHTTPException,
    ) -> JSONResponse:
        detail = error.detail
        if detail == "There was an error parsing the body":
            detail = "上传请求解析失败，请检查项目磁盘空间后重新选择文件上传"
        return JSONResponse(
            status_code=error.status_code,
            content={"detail": detail},
            headers=error.headers,
        )

    def load_task_manifest(task_id: str) -> TaskManifest | None:
        safe_task_id = _validate_id(task_id, "任务")
        for root in (
            resolved_settings.paths.output_dir.resolve(),
            resolved_settings.paths.work_dir.resolve(),
        ):
            manifest_path = root / safe_task_id / "task.json"
            if manifest_path.is_file():
                for attempt in range(5):
                    try:
                        return TaskManifest.model_validate_json(
                            manifest_path.read_text(encoding="utf-8")
                        )
                    except PermissionError:
                        # Windows may briefly deny reads while the worker replaces this file.
                        if attempt == 4:
                            raise
                        time.sleep(0.02)
        return None

    def task_payload(task_id: str) -> dict[str, Any]:
        safe_task_id = _validate_id(task_id, "任务")
        manifest = load_task_manifest(safe_task_id)
        if manifest is not None:
            return _manifest_payload(manifest)
        queued = registry.get(safe_task_id)
        if queued is not None:
            return queued
        raise HTTPException(status_code=404, detail="任务不存在")

    def persist_job(
        queued: dict[str, Any],
        media_path: Path,
        subtitle_path: Path | None,
        mode: FusionMode,
    ) -> dict[str, Any]:
        job = {
            **queued,
            "media_path": str(media_path.resolve()),
            "subtitle_path": str(subtitle_path.resolve()) if subtitle_path is not None else None,
            "mode": mode.value,
        }
        _write_json_atomic(
            _task_job_path(resolved_settings, queued["task_id"]),
            job,
        )
        return job

    def submit_task(
        queued: dict[str, Any],
        media_path: Path,
        subtitle_path: Path | None,
        mode: FusionMode,
    ) -> None:
        task_id = queued["task_id"]
        with submission_lock:
            if task_id in submitted_ids:
                return
            submitted_ids.add(task_id)
        registry.set(task_id, queued)
        job = persist_job(queued, media_path, subtitle_path, mode)

        def prepare_live_segments() -> list[tuple[dict[str, Any], Path]]:
            if queued.get("source_kind") != "live":
                return []
            manifest = load_task_manifest(task_id)
            if manifest is None:
                return []
            plan_path = manifest.work_dir / "live-segments.json"
            if plan_path.is_file():
                plan = json.loads(plan_path.read_text(encoding="utf-8"))
            else:
                parts = sorted(media_path.parent.glob(f"{media_path.stem}-segment-*.mkv"))
                if not parts:
                    return []
                title = Path(job["original_filename"]).stem
                plan = {"first_title": f"{title}_第001段.mkv", "children": []}
                for index, part in enumerate(parts, 2):
                    child_id = uuid4().hex
                    child_media = media_path.parent.parent / child_id / "source.mkv"
                    child = {**job, "task_id": child_id, "status": "queued", "progress": None,
                             "error": None, "input_path": str(child_media), "media_path": str(child_media),
                             "work_dir": str(resolved_settings.paths.work_dir.resolve() / child_id),
                             "output_dir": str(resolved_settings.paths.output_dir.resolve() / child_id),
                             "original_filename": f"{title}_第{index:03d}段.mkv"}
                    plan["children"].append({"source": str(part), "job": child})
                # Persist the move plan first so restart recovery never loses a segment.
                _write_json_atomic(plan_path, plan)
            children = []
            for entry in plan["children"]:
                child = entry["job"]
                source, destination = Path(entry["source"]), Path(child["media_path"])
                if not destination.is_file():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    source.replace(destination)
                existing = load_task_manifest(child["task_id"])
                if existing is None:
                    child_manifest = TaskManifest.model_validate(child)
                    _write_json_atomic(child_manifest.output_dir / "task.json", child_manifest.model_dump(mode="json"))
                if existing is None or existing.status not in TERMINAL_STATUSES:
                    persist_job(child, destination, None, mode)
                    children.append((child, destination))
            with batch_update_lock:
                batch = _read_batch_manifest(resolved_settings, job["batch_id"])
                for entry in plan["children"]:
                    child_id = entry["job"]["task_id"]
                    if child_id not in batch["task_ids"]:
                        batch["task_ids"].append(child_id)
                _write_batch_manifest(resolved_settings, batch)
            job["original_filename"] = plan["first_title"]
            manifest.original_filename = plan["first_title"]
            _write_json_atomic(manifest.output_dir / "task.json", manifest.model_dump(mode="json"))
            _write_json_atomic(_task_job_path(resolved_settings, task_id), job)
            return children

        def finalize_job(future: Future[str | None]) -> None:
            worker_crashed = False
            try:
                error_message = future.result()
            except Exception as error:
                worker_crashed = True
                error_message = f"工作进程异常退出：{error}"
            if error_message:
                registry.set(
                    task_id,
                    {**queued, "status": "failed", "error": error_message},
                )
                manifest = load_task_manifest(task_id)
                if manifest is not None:
                    manifest.status = TaskStatus.FAILED
                    manifest.error = error_message
                    manifest.updated_at = datetime.now().astimezone().isoformat(timespec="seconds")
                    _write_json_atomic(manifest.output_dir / "task.json", manifest.model_dump(mode="json"))
            if not worker_crashed:
                _task_job_path(resolved_settings, task_id).unlink(missing_ok=True)
            with submission_lock:
                submitted_ids.discard(task_id)

        def transcribe() -> None:
            children = [] if job.get("summary_only") else prepare_live_segments()
            if not job.get("summary_only") and queued.get("source_kind") and queued.get("transcribe_after_download") is False:
                now = datetime.now().astimezone().isoformat(timespec="seconds")
                manifest = load_task_manifest(task_id) or TaskManifest.model_validate({
                    **job, "input_path": media_path,
                    "work_dir": resolved_settings.paths.work_dir.resolve() / task_id,
                    "output_dir": resolved_settings.paths.output_dir.resolve() / task_id,
                    "created_at": now, "updated_at": now,
                })
                manifest.status = TaskStatus.COMPLETED
                manifest.progress = None
                manifest.error = None
                manifest.updated_at = now
                for root in (manifest.work_dir, manifest.output_dir):
                    _write_json_atomic(root / "task.json", manifest.model_dump(mode="json"))
                _task_job_path(resolved_settings, task_id).unlink(missing_ok=True)
                for child, destination in children:
                    submit_task(child, destination, None, mode)
                return
            future = executor.submit(task_runner, settings_payload, job)
            future.add_done_callback(finalize_job)
            for child, destination in children:
                submit_task(child, destination, None, mode)

        if not job.get("summary_only") and queued.get("source_kind") and not media_path.is_file():
            def acquire() -> None:
                manifest = load_task_manifest(task_id)
                if manifest is None:
                    return
                stop_file = manifest.work_dir / "stop-requested"

                def progress(stage: str, current: int, total: int) -> None:
                    manifest.status = TaskStatus(stage)
                    manifest.progress = TaskProgress(stage=stage, current=current, total=total)
                    manifest.updated_at = datetime.now().astimezone().isoformat(timespec="seconds")
                    _write_json_atomic(manifest.output_dir / "task.json", manifest.model_dump(mode="json"))

                def stopped() -> bool:
                    return shutdown_requested.is_set() or stop_file.exists()

                try:
                    if stopped():
                        raise douyin.AcquisitionError("任务已停止，尚未获取媒体")
                    progress("downloading", 0, 0)
                    if queued["source_kind"] == "live":
                        title = douyin.record_live(
                            queued["source_url"], media_path, queued["recording_minutes"],
                            resolved_settings.ffmpeg.ffmpeg_path, progress, stopped,
                        )
                    else:
                        title = douyin.download_video(queued["source_url"], media_path, progress, stopped)
                    safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip(" .")[:100] or "抖音素材"
                    job["original_filename"] = safe_title + media_path.suffix
                    manifest.original_filename = job["original_filename"]
                    manifest.status = TaskStatus.QUEUED
                    manifest.progress = None
                    _write_json_atomic(manifest.output_dir / "task.json", manifest.model_dump(mode="json"))
                    _write_json_atomic(_task_job_path(resolved_settings, task_id), job)
                    if not shutdown_requested.is_set():
                        transcribe()
                except Exception as error:
                    # Do not persist raw HTTP/FFmpeg errors: they may contain signed URLs or cookies.
                    manifest.status = TaskStatus.FAILED
                    manifest.error = str(error) if isinstance(error, douyin.AcquisitionError) else "获取抖音素材失败，请检查网络和链接后重试"
                    manifest.updated_at = datetime.now().astimezone().isoformat(timespec="seconds")
                    _write_json_atomic(manifest.output_dir / "task.json", manifest.model_dump(mode="json"))
                    _task_job_path(resolved_settings, task_id).unlink(missing_ok=True)

            (live_executor if queued.get("source_kind") == "live" else acquisition_executor).submit(acquire)
        else:
            transcribe()

    def recover_pending_tasks() -> None:
        queue_dir = resolved_settings.paths.work_dir.resolve() / "queue"
        if not queue_dir.is_dir():
            return
        for job_path in sorted(queue_dir.glob("*.json")):
            try:
                job = json.loads(job_path.read_text(encoding="utf-8"))
                task_id = _validate_id(str(job["task_id"]), "任务")
                manifest = load_task_manifest(task_id)
                if manifest is not None and manifest.status in TERMINAL_STATUSES:
                    job_path.unlink(missing_ok=True)
                    continue
                media_path = Path(str(job["media_path"]))
                if not job.get("summary_only") and job.get("source_kind") == "live" and not media_path.is_file():
                    if manifest is not None:
                        manifest.status = TaskStatus.FAILED
                        manifest.error = "服务重启导致录制中断，部分文件已保留；请重新提交直播间链接"
                        _write_json_atomic(manifest.output_dir / "task.json", manifest.model_dump(mode="json"))
                    job_path.unlink(missing_ok=True)
                    continue
                subtitle_value = job.get("subtitle_path")
                subtitle_path = Path(str(subtitle_value)) if subtitle_value else None
                if not job.get("summary_only") and ((not media_path.is_file() and not job.get("source_kind")) or (
                    subtitle_path is not None and not subtitle_path.is_file()
                )):
                    registry.set(
                        task_id,
                        {
                            **job,
                            "status": "failed",
                            "error": "恢复任务失败：原始上传文件不存在",
                        },
                    )
                    job_path.unlink(missing_ok=True)
                    continue
                queued = {
                    key: job.get(key)
                    for key in (
                        "task_id",
                        "batch_id",
                        "status",
                        "mode",
                        "hard_subtitles",
                        "translate_to_chinese",
                        "asr_mode",
                        "api_language",
                        "api_hotwords",
                        "original_filename",
                        "media_size",
                        "warnings",
                        "error",
                        "source_url",
                        "source_kind",
                        "recording_minutes",
                        "transcribe_after_download",
                        "summarize",
                        "summary_only",
                    )
                }
                queued["status"] = "queued"
                queued["asr_mode"] = job.get("asr_mode") or ASRMode.LOCAL.value
                queued["api_hotwords"] = job.get("api_hotwords") or []
                submit_task(
                    queued,
                    media_path,
                    subtitle_path,
                    FusionMode(str(job["mode"])),
                )
            except Exception:
                job_path.rename(job_path.with_suffix(".invalid"))

    def storage_payload() -> dict[str, Any]:
        work_root = resolved_settings.paths.work_dir.resolve()
        output_root = resolved_settings.paths.output_dir.resolve()
        return {
            "work_bytes": _directory_size(work_root),
            "output_bytes": _directory_size(output_root),
            "uploads_bytes": _directory_size(work_root / "uploads"),
            "cache_bytes": _directory_size(work_root / "cache"),
            "task_count": sum(1 for _ in output_root.glob("*/task.json"))
            if output_root.is_dir()
            else 0,
            "pending_count": sum(1 for _ in (work_root / "queue").glob("*.json"))
            if (work_root / "queue").is_dir()
            else 0,
        }

    async def prepare_task(
        media: UploadFile,
        subtitle: UploadFile | None,
        mode: FusionMode,
        batch_id: str | None = None,
        hard_subtitles: bool = False,
        translate_to_chinese: bool = False,
        asr_mode: ASRMode = ASRMode.LOCAL,
        api_hotwords: list[str] | None = None,
        summarize: bool = False,
    ) -> tuple[dict[str, Any], Path, Path | None]:
        media_suffix = Path(media.filename or "").suffix.lower()
        if media_suffix not in MEDIA_EXTENSIONS:
            raise HTTPException(status_code=400, detail="不支持该视频或音频格式")
        subtitle_suffix: str | None = None
        if subtitle is not None and subtitle.filename:
            subtitle_suffix = Path(subtitle.filename).suffix.lower()
            if subtitle_suffix not in SUBTITLE_EXTENSIONS:
                raise HTTPException(status_code=400, detail="不支持该字幕格式")

        task_id = uuid4().hex
        upload_group = batch_id or task_id
        upload_dir = resolved_settings.paths.work_dir.resolve() / "uploads" / upload_group / task_id
        media_path = upload_dir / f"source{media_suffix}"
        media_size = await _save_upload(media, media_path)
        subtitle_path: Path | None = None
        if subtitle is not None and subtitle_suffix:
            subtitle_path = upload_dir / f"subtitle{subtitle_suffix}"
            await _save_upload(subtitle, subtitle_path)

        queued = {
            "task_id": task_id,
            "batch_id": batch_id,
            "status": "queued",
            "mode": mode.value,
            "hard_subtitles": hard_subtitles,
            "translate_to_chinese": translate_to_chinese,
            "summarize": summarize,
            "asr_mode": asr_mode.value,
            "api_language": None,
            "api_hotwords": api_hotwords or [] if asr_mode == ASRMode.API else [],
            "original_filename": media.filename,
            "media_size": media_size,
            "warnings": [],
            "error": None,
        }
        return queued, media_path, subtitle_path

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        model_path = resolved_settings.asr.model_path
        return {
            "ok": True,
            "version": __version__,
            "model_configured": bool(model_path and model_path.is_dir()),
            "model_name": _model_display_name(model_path),
            "device": resolved_settings.asr.device,
            "compute_type": resolved_settings.asr.compute_type,
            "ocr_available": bool(
                importlib.util.find_spec("paddleocr") and importlib.util.find_spec("paddle")
            ),
            "translation_models_available": _translation_models_available(resolved_settings),
            "api_pipeline_available": _api_pipeline_available(resolved_settings),
            "api_key_environment": resolved_settings.qwen_asr.api_key_environment,
            "douyin_video_available": douyin.available("video"),
            "douyin_live_available": douyin.available("live") and bool(shutil.which(resolved_settings.ffmpeg.ffmpeg_path)),
            "douyin_cookie_configured": bool(os.getenv("VIDEO2TXT_DOUYIN_COOKIE", "").strip()),
            "max_live_recordings": douyin.MAX_LIVE_RECORDINGS,
            "summary_available": _summary_available(resolved_settings),
        }

    @app.post("/api/douyin", status_code=202)
    def create_douyin_task(
        url: Annotated[str, Form(max_length=65536)],
        source_kind: Annotated[Literal["video", "live"], Form()] = "video",
        transcribe_after_download: Annotated[bool, Form()] = True,
        summarize: Annotated[bool, Form()] = False,
        recording_minutes: Annotated[int, Form(ge=0, le=480)] = 0,
        mode: Annotated[FusionMode, Form()] = FusionMode.VERBATIM,
        hard_subtitles: Annotated[bool, Form()] = False,
        translate_to_chinese: Annotated[bool, Form()] = False,
        asr_mode: Annotated[ASRMode, Form()] = ASRMode.LOCAL,
        api_hotwords: Annotated[str, Form()] = "",
    ) -> dict[str, Any]:
        if summarize:
            transcribe_after_download = True
            if not _summary_available(resolved_settings):
                raise HTTPException(status_code=503, detail="智能总结不可用，请安装并登录 Codex CLI")
        try:
            source_urls = douyin.extract_urls(url, source_kind)
        except (ValueError, douyin.AcquisitionError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from None
        if not douyin.available(source_kind):
            raise HTTPException(status_code=503, detail='抖音组件未安装，请安装项目的 douyin 依赖组')
        if (source_kind == "live" or transcribe_after_download) and not shutil.which(resolved_settings.ffmpeg.ffmpeg_path):
            raise HTTPException(status_code=503, detail="未找到 FFmpeg，请检查项目配置")
        if transcribe_after_download and asr_mode == ASRMode.LOCAL and not (
            resolved_settings.asr.model_path and resolved_settings.asr.model_path.is_dir()
        ):
            raise HTTPException(status_code=503, detail="本地 ASR 模型尚未配置")
        if transcribe_after_download and asr_mode == ASRMode.LOCAL and translate_to_chinese and not _translation_models_available(resolved_settings):
            raise HTTPException(status_code=503, detail="离线中文翻译模型尚未安装")
        if transcribe_after_download and asr_mode == ASRMode.API and not _api_pipeline_available(resolved_settings):
            raise HTTPException(status_code=503, detail="API 模式不可用，请检查千问配置和 Codex CLI 登录")
        hotwords = _parse_api_hotwords(api_hotwords) if transcribe_after_download else []
        batch_id = uuid4().hex
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        jobs = []
        for index, source_url in enumerate(source_urls, 1):
            task_id = uuid4().hex
            work_dir = resolved_settings.paths.work_dir.resolve() / task_id
            output_dir = resolved_settings.paths.output_dir.resolve() / task_id
            media_path = resolved_settings.paths.work_dir.resolve() / "uploads" / batch_id / task_id / (
                "source.mkv" if source_kind == "live" else "source.mp4"
            )
            manifest = TaskManifest(
                task_id=task_id, batch_id=batch_id, status=TaskStatus.QUEUED,
                input_path=media_path, original_filename=f"抖音直播 {index}" if source_kind == "live" else f"抖音视频 {index}",
                work_dir=work_dir, output_dir=output_dir, mode=mode,
                asr_mode=asr_mode, hard_subtitles=hard_subtitles and transcribe_after_download,
                transcribe_after_download=transcribe_after_download,
                summarize=summarize,
                translate_to_chinese=transcribe_after_download and (translate_to_chinese or asr_mode == ASRMode.API),
                api_hotwords=hotwords if asr_mode == ASRMode.API else [],
                source_url=source_url, source_kind=source_kind,
                recording_minutes=recording_minutes if source_kind == "live" else None,
                created_at=now, updated_at=now,
            )
            work_dir.mkdir(parents=True, exist_ok=True)
            _write_json_atomic(output_dir / "task.json", manifest.model_dump(mode="json"))
            queued = manifest.model_dump(mode="json")
            jobs.append((queued, media_path))
        batch = {"batch_id": batch_id, "created_at": now, "task_ids": [job["task_id"] for job, _ in jobs], "mode": mode.value}
        _write_batch_manifest(resolved_settings, batch)
        for queued, media_path in jobs:
            submit_task(queued, media_path, None, mode)
        return {**batch, "tasks": [job for job, _ in jobs]}

    @app.post("/api/tasks/{task_id}/stop", status_code=202)
    def stop_recording(task_id: str) -> dict[str, Any]:
        manifest = load_task_manifest(task_id)
        if manifest is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        if manifest.source_kind != "live" or manifest.status not in {
            TaskStatus.QUEUED, TaskStatus.DOWNLOADING, TaskStatus.RECORDING,
        } or manifest.input_path.is_file():
            raise HTTPException(status_code=409, detail="该任务当前不在录制阶段")
        manifest.work_dir.mkdir(parents=True, exist_ok=True)
        (manifest.work_dir / "stop-requested").touch()
        message = "正在结束录制并保存分段视频"
        if manifest.transcribe_after_download:
            message += "，随后逐段转字幕"
        return {"task_id": task_id, "message": message}

    @app.get("/api/tasks/{task_id}/source")
    def download_source(task_id: str) -> FileResponse:
        manifest = load_task_manifest(task_id)
        if manifest is None or not manifest.source_kind:
            raise HTTPException(status_code=404, detail="没有下载的抖音素材")
        media = manifest.input_path.resolve()
        root = resolved_settings.paths.work_dir.resolve() / "uploads"
        if root not in media.parents or not media.is_file():
            raise HTTPException(status_code=404, detail="媒体尚未获取完成或已清理")
        return FileResponse(
            media, filename=_media_stem(manifest.model_dump()) + media.suffix,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/tasks/{task_id}/thumbnail")
    def task_thumbnail(task_id: str) -> FileResponse:
        manifest = load_task_manifest(task_id)
        if manifest is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        cached = manifest.work_dir / "thumbnail.jpg"
        if cached.is_file():
            return FileResponse(cached, media_type="image/jpeg")
        media = manifest.input_path.resolve()
        root = resolved_settings.paths.work_dir.resolve() / "uploads"
        if root not in media.parents:
            raise HTTPException(status_code=404, detail="无可用缩略图")
        if not media.is_file() and manifest.source_kind == "live":
            parts = sorted(media.parent.glob(f"{media.stem}.part-*.mkv"))
            if parts:
                media = parts[0]
        if not media.is_file() or media.stat().st_size < 1024:
            raise HTTPException(status_code=404, detail="画面尚未就绪")
        manifest.work_dir.mkdir(parents=True, exist_ok=True)
        temporary = cached.with_name(f"thumbnail-{uuid4().hex}.jpg")
        try:
            subprocess.run([
                resolved_settings.ffmpeg.ffmpeg_path, "-hide_banner", "-loglevel", "error",
                "-nostdin", "-i", str(media), "-frames:v", "1", "-vf",
                "scale=240:136:force_original_aspect_ratio=decrease:force_divisible_by=2,pad=240:136:(ow-iw)/2:(oh-ih)/2",
                "-q:v", "4", "-y", str(temporary),
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8, check=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if not temporary.is_file():
                raise HTTPException(status_code=404, detail="素材没有视频画面")
            temporary.replace(cached)
        except (OSError, subprocess.SubprocessError):
            raise HTTPException(status_code=404, detail="暂时无法生成缩略图") from None
        finally:
            temporary.unlink(missing_ok=True)
        return FileResponse(cached, media_type="image/jpeg")

    @app.get("/api/storage")
    def get_storage() -> dict[str, Any]:
        return storage_payload()

    @app.post("/api/storage/cleanup")
    def cleanup_storage(scope: Annotated[str, Form()]) -> dict[str, Any]:
        if scope not in {"temporary", "cache"}:
            raise HTTPException(status_code=400, detail="不支持该清理范围")
        work_root = resolved_settings.paths.work_dir.resolve()
        if scope == "cache" and storage_payload()["pending_count"]:
            raise HTTPException(status_code=409, detail="有任务正在运行，暂不能清理缓存")
        freed_bytes = 0
        if scope == "cache":
            freed_bytes += _remove_child_path(work_root, work_root / "cache")
        elif work_root.is_dir():
            for task_dir in work_root.iterdir():
                manifest_path = task_dir / "task.json"
                if not task_dir.is_dir() or not manifest_path.is_file():
                    continue
                try:
                    manifest = TaskManifest.model_validate_json(
                        manifest_path.read_text(encoding="utf-8")
                    )
                except (OSError, ValueError):
                    continue
                if manifest.status not in TERMINAL_STATUSES:
                    continue
                for target in (task_dir / "audio.wav", task_dir / "ocr-frames"):
                    freed_bytes += _remove_child_path(work_root, target)
        return {"scope": scope, "freed_bytes": freed_bytes, **storage_payload()}

    @app.get("/api/tasks")
    def list_tasks(
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 12,
    ) -> dict[str, Any]:
        records: dict[str, dict[str, Any]] = {}
        # Include local work-in-progress and waiting jobs, not just exported results.
        for root in (resolved_settings.paths.output_dir.resolve(), resolved_settings.paths.work_dir.resolve()):
            for path in root.glob("*/task.json"):
                try:
                    manifest = TaskManifest.model_validate_json(path.read_text(encoding="utf-8"))
                    previous = records.get(manifest.task_id)
                    if previous is None or manifest.updated_at > previous.get("updated_at", ""):
                        records[manifest.task_id] = _manifest_payload(manifest)
                except (OSError, ValueError):
                    continue
        for path in (resolved_settings.paths.work_dir.resolve() / "queue").glob("*.json"):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
                records.setdefault(job["task_id"], job)
            except (OSError, ValueError, KeyError):
                continue
        ordered = sorted(records.values(), key=lambda task: task.get("created_at") or task.get("updated_at") or "", reverse=True)
        total = len(ordered)
        start = (page - 1) * page_size
        tasks = ordered[start:start + page_size]
        return {
            "tasks": tasks,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": max(1, (total + page_size - 1) // page_size),
        }

    @app.delete("/api/tasks")
    def delete_all_tasks() -> dict[str, Any]:
        work_root = resolved_settings.paths.work_dir.resolve()
        output_root = resolved_settings.paths.output_dir.resolve()
        if storage_payload()["pending_count"]:
            raise HTTPException(status_code=409, detail="有任务正在运行，暂不能清空全部任务")

        manifests: dict[str, TaskManifest] = {}
        for root in (output_root, work_root):
            if not root.is_dir():
                continue
            for manifest_path in root.glob("*/task.json"):
                try:
                    manifest = TaskManifest.model_validate_json(
                        manifest_path.read_text(encoding="utf-8")
                    )
                except (OSError, ValueError):
                    continue
                manifests[manifest.task_id] = manifest
        if any(manifest.status not in TERMINAL_STATUSES for manifest in manifests.values()):
            raise HTTPException(status_code=409, detail="有任务正在运行，暂不能清空全部任务")

        freed_bytes = 0
        for child in list(output_root.iterdir()) if output_root.is_dir() else []:
            freed_bytes += _remove_child_path(output_root, child)
        for manifest in manifests.values():
            freed_bytes += _remove_child_path(work_root, work_root / manifest.task_id)
            registry.remove(manifest.task_id)
        for managed_name in ("uploads", "batches"):
            managed_root = work_root / managed_name
            if not managed_root.is_dir():
                continue
            for child in list(managed_root.iterdir()):
                freed_bytes += _remove_child_path(managed_root, child)

        return {
            "cleared_tasks": len(manifests),
            "freed_bytes": freed_bytes,
            **storage_payload(),
        }

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str) -> dict[str, Any]:
        return task_payload(task_id)

    @app.post("/api/tasks/{task_id}/retry", status_code=202)
    def retry_task(task_id: str) -> dict[str, Any]:
        manifest = load_task_manifest(task_id)
        if manifest is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        if manifest.status != TaskStatus.FAILED:
            raise HTTPException(status_code=409, detail="只有失败任务可以重试")
        if not manifest.input_path.is_file() and manifest.source_kind:
            return create_douyin_task(
                url=manifest.source_url, source_kind=manifest.source_kind,
                transcribe_after_download=manifest.transcribe_after_download,
                summarize=manifest.summarize,
                recording_minutes=manifest.recording_minutes if manifest.recording_minutes is not None else 0, mode=manifest.mode,
                hard_subtitles=manifest.hard_subtitles,
                translate_to_chinese=manifest.translate_to_chinese,
                asr_mode=manifest.asr_mode, api_hotwords="\n".join(manifest.api_hotwords),
            )
        if not manifest.input_path.is_file():
            raise HTTPException(status_code=409, detail="原始上传文件已被清理，无法重试")

        batch_id = uuid4().hex
        new_task_id = uuid4().hex
        upload_dir = resolved_settings.paths.work_dir.resolve() / "uploads" / batch_id / new_task_id
        media_path = upload_dir / f"source{manifest.input_path.suffix.lower()}"
        subtitle_value = manifest.artifacts.get("subtitle_source")
        subtitle_source = Path(subtitle_value) if subtitle_value else None
        subtitle_path: Path | None = None
        try:
            upload_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(manifest.input_path, media_path)
            if subtitle_source is not None and subtitle_source.is_file():
                subtitle_path = upload_dir / f"subtitle{subtitle_source.suffix.lower()}"
                shutil.copy2(subtitle_source, subtitle_path)
        except Exception:
            _remove_child_path(
                resolved_settings.paths.work_dir.resolve(),
                resolved_settings.paths.work_dir.resolve() / "uploads" / batch_id,
            )
            raise

        queued = {
            "task_id": new_task_id,
            "batch_id": batch_id,
            "status": "queued",
            "mode": manifest.mode.value,
            "hard_subtitles": manifest.hard_subtitles,
            "translate_to_chinese": manifest.translate_to_chinese,
            "asr_mode": manifest.asr_mode.value,
            "api_language": manifest.api_language,
            "api_hotwords": manifest.api_hotwords,
            "original_filename": manifest.original_filename or manifest.input_path.name,
            "source_url": manifest.source_url,
            "source_kind": manifest.source_kind,
            "transcribe_after_download": manifest.transcribe_after_download,
            "summarize": manifest.summarize,
            "recording_minutes": manifest.recording_minutes,
            "media_size": media_path.stat().st_size,
            "warnings": [],
            "error": None,
        }
        batch = {
            "batch_id": batch_id,
            "mode": manifest.mode.value,
            "hard_subtitles": manifest.hard_subtitles,
            "translate_to_chinese": manifest.translate_to_chinese,
            "asr_mode": manifest.asr_mode.value,
            "api_language": manifest.api_language,
            "api_hotwords": manifest.api_hotwords,
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "task_ids": [new_task_id],
        }
        _write_batch_manifest(resolved_settings, batch)
        submit_task(queued, media_path, subtitle_path, manifest.mode)
        return {**batch, "tasks": [queued]}

    @app.post("/api/tasks/{task_id}/summarize", status_code=202)
    def summarize_task(task_id: str) -> dict[str, Any]:
        manifest = load_task_manifest(task_id)
        if manifest is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        if manifest.status != TaskStatus.COMPLETED or _task_job_path(resolved_settings, task_id).exists():
            raise HTTPException(status_code=409, detail="请等待当前任务完成后再生成总结")
        if not (manifest.output_dir / "fusion.json").is_file():
            raise HTTPException(status_code=400, detail="请先完成字幕转写，再生成总结")
        if not _summary_available(resolved_settings):
            raise HTTPException(status_code=503, detail="智能总结不可用，请安装并登录 Codex CLI")
        with submission_lock:
            if task_id in submitted_ids:
                raise HTTPException(status_code=409, detail="总结任务已在处理中")
        manifest.summarize = True
        manifest.summary_error = None
        manifest.status = TaskStatus.SUMMARIZING
        manifest.progress = TaskProgress(stage="summarizing", current=0, total=1)
        manifest.updated_at = datetime.now().astimezone().isoformat(timespec="seconds")
        _write_json_atomic(manifest.output_dir / "task.json", manifest.model_dump(mode="json"))
        queued = {**manifest.model_dump(mode="json"), "summary_only": True}
        submit_task(queued, manifest.input_path, None, manifest.mode)
        return task_payload(task_id)

    @app.delete("/api/tasks/{task_id}")
    def delete_task(task_id: str) -> dict[str, Any]:
        payload = task_payload(task_id)
        try:
            status = TaskStatus(payload["status"])
        except ValueError as error:
            raise HTTPException(status_code=409, detail="任务状态无效") from error
        if status not in TERMINAL_STATUSES:
            raise HTTPException(status_code=409, detail="运行中的任务不能删除")

        safe_task_id = _validate_id(task_id, "任务")
        work_root = resolved_settings.paths.work_dir.resolve()
        output_root = resolved_settings.paths.output_dir.resolve()
        batch_id = payload.get("batch_id")
        freed_bytes = 0
        for target, root in (
            (work_root / safe_task_id, work_root),
            (output_root / safe_task_id, output_root),
        ):
            freed_bytes += _remove_child_path(root, target)
        upload_group = str(batch_id or safe_task_id)
        freed_bytes += _remove_child_path(
            work_root,
            work_root / "uploads" / upload_group / safe_task_id,
        )
        _task_job_path(resolved_settings, safe_task_id).unlink(missing_ok=True)
        registry.remove(safe_task_id)

        if batch_id:
            batch_path = _batch_manifest_path(
                resolved_settings, _validate_id(str(batch_id), "批次")
            )
            with batch_update_lock:
                if batch_path.is_file():
                    batch = _read_batch_manifest(resolved_settings, str(batch_id))
                    batch["task_ids"] = [
                        item for item in batch.get("task_ids", []) if item != safe_task_id
                    ]
                    if batch["task_ids"]:
                        _write_batch_manifest(resolved_settings, batch)
                    else:
                        batch_path.unlink(missing_ok=True)
        return {"task_id": safe_task_id, "freed_bytes": freed_bytes, **storage_payload()}

    @app.get("/api/tasks/{task_id}/files/{filename}")
    def download_task_file(task_id: str, filename: str) -> FileResponse:
        if filename not in DOWNLOAD_FILES:
            raise HTTPException(status_code=404, detail="文件不存在")
        safe_task_id = _validate_id(task_id, "任务")
        target = resolved_settings.paths.output_dir.resolve() / safe_task_id / filename
        if not target.is_file():
            raise HTTPException(status_code=404, detail="文件不存在")
        task = task_payload(safe_task_id)
        return FileResponse(target, filename=_export_filename(task, filename))

    @app.get("/api/tasks/{task_id}/export")
    def export_task(
        task_id: str,
        types: Annotated[list[ExportType] | None, Query()] = None,
        kind: LegacyExportKind | None = None,
    ) -> FileResponse:
        task = task_payload(task_id)
        if task["status"] != "completed":
            raise HTTPException(status_code=409, detail="任务尚未完成")
        output_dir = resolved_settings.paths.output_dir.resolve() / task["task_id"]
        selected = _selected_export_files(types, kind)
        available = [name for name in selected if (output_dir / name).is_file()]
        if len(available) != len(selected):
            raise HTTPException(status_code=404, detail="所选类型的文件不存在")
        if not available:
            raise HTTPException(status_code=404, detail="没有可下载的文件")
        if len(selected) == 1:
            internal_name = available[0]
            return FileResponse(
                output_dir / internal_name,
                filename=_export_filename(task, internal_name),
            )

        export_dir = resolved_settings.paths.work_dir.resolve() / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        archive_path = export_dir / f"{task['task_id']}-{uuid4().hex}.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for internal_name in available:
                archive.write(
                    output_dir / internal_name,
                    _export_filename(task, internal_name),
                )
        return FileResponse(
            archive_path,
            media_type="application/zip",
            filename=f"{_media_stem(task)}.zip",
            background=BackgroundTask(archive_path.unlink, missing_ok=True),
        )

    @app.post("/api/tasks", status_code=202)
    async def create_task(
        media: Annotated[UploadFile, File()],
        subtitle: Annotated[UploadFile | None, File()] = None,
        mode: Annotated[FusionMode, Form()] = FusionMode.VERBATIM,
        hard_subtitles: Annotated[bool, Form()] = False,
        translate_to_chinese: Annotated[bool, Form()] = False,
        asr_mode: Annotated[ASRMode, Form()] = ASRMode.LOCAL,
        api_hotwords: Annotated[str, Form()] = "",
        summarize: Annotated[bool, Form()] = False,
    ) -> dict[str, Any]:
        if summarize and not _summary_available(resolved_settings):
            raise HTTPException(status_code=503, detail="智能总结不可用，请安装并登录 Codex CLI")
        model_path = resolved_settings.asr.model_path
        if asr_mode == ASRMode.LOCAL and (model_path is None or not model_path.is_dir()):
            raise HTTPException(status_code=503, detail="本地 ASR 模型尚未配置")
        if (
            asr_mode == ASRMode.LOCAL
            and translate_to_chinese
            and not _translation_models_available(resolved_settings)
        ):
            raise HTTPException(status_code=503, detail="离线中文翻译模型尚未安装")
        if asr_mode == ASRMode.API and not _api_pipeline_available(resolved_settings):
            raise HTTPException(
                status_code=503,
                detail=(
                    "API 模式不可用：请设置 "
                    f"{resolved_settings.qwen_asr.api_key_environment}、安装 requests 并"
                    "登录 Codex CLI"
                ),
            )
        hotwords = _parse_api_hotwords(api_hotwords)
        effective_translation = translate_to_chinese or asr_mode == ASRMode.API

        queued, media_path, subtitle_path = await prepare_task(
            media,
            subtitle,
            mode,
            hard_subtitles=hard_subtitles,
            translate_to_chinese=effective_translation,
            asr_mode=asr_mode,
            api_hotwords=hotwords,
            summarize=summarize,
        )
        submit_task(queued, media_path, subtitle_path, mode)
        return queued

    @app.post("/api/batches", status_code=202)
    async def create_batch(
        media: Annotated[list[UploadFile], File()],
        subtitles: Annotated[list[UploadFile | str] | None, File()] = None,
        mode: Annotated[FusionMode, Form()] = FusionMode.VERBATIM,
        hard_subtitles: Annotated[bool, Form()] = False,
        translate_to_chinese: Annotated[bool, Form()] = False,
        asr_mode: Annotated[ASRMode, Form()] = ASRMode.LOCAL,
        api_hotwords: Annotated[str, Form()] = "",
        summarize: Annotated[bool, Form()] = False,
    ) -> dict[str, Any]:
        if summarize and not _summary_available(resolved_settings):
            raise HTTPException(status_code=503, detail="智能总结不可用，请安装并登录 Codex CLI")
        model_path = resolved_settings.asr.model_path
        if asr_mode == ASRMode.LOCAL and (model_path is None or not model_path.is_dir()):
            raise HTTPException(status_code=503, detail="本地 ASR 模型尚未配置")
        if (
            asr_mode == ASRMode.LOCAL
            and translate_to_chinese
            and not _translation_models_available(resolved_settings)
        ):
            raise HTTPException(status_code=503, detail="离线中文翻译模型尚未安装")
        if asr_mode == ASRMode.API and not _api_pipeline_available(resolved_settings):
            raise HTTPException(
                status_code=503,
                detail=(
                    "API 模式不可用：请设置 "
                    f"{resolved_settings.qwen_asr.api_key_environment}、安装 requests 并"
                    "登录 Codex CLI"
                ),
            )
        hotwords = _parse_api_hotwords(api_hotwords)
        effective_translation = translate_to_chinese or asr_mode == ASRMode.API
        if not media:
            raise HTTPException(status_code=400, detail="请至少上传一个媒体文件")
        if len(media) > MAX_BATCH_FILES:
            raise HTTPException(status_code=400, detail=f"单批最多 {MAX_BATCH_FILES} 个媒体文件")
        subtitle_uploads: list[UploadFile] = []
        for upload in subtitles or []:
            if isinstance(upload, str):
                continue
            if upload.filename:
                subtitle_uploads.append(upload)
            else:
                await upload.close()
        if len(subtitle_uploads) > MAX_BATCH_FILES:
            raise HTTPException(status_code=400, detail=f"单批最多 {MAX_BATCH_FILES} 个字幕文件")

        for upload in media:
            if Path(upload.filename or "").suffix.lower() not in MEDIA_EXTENSIONS:
                raise HTTPException(status_code=400, detail=f"不支持媒体文件：{upload.filename}")
        subtitle_map: dict[str, UploadFile] = {}
        for upload in subtitle_uploads:
            if Path(upload.filename or "").suffix.lower() not in SUBTITLE_EXTENSIONS:
                raise HTTPException(status_code=400, detail=f"不支持字幕文件：{upload.filename}")
            key = Path(upload.filename or "").stem.casefold()
            if key in subtitle_map:
                raise HTTPException(status_code=400, detail=f"字幕文件名重复：{upload.filename}")
            subtitle_map[key] = upload
        media_stems = {Path(upload.filename or "").stem.casefold() for upload in media}
        unmatched = [
            upload.filename for key, upload in subtitle_map.items() if key not in media_stems
        ]
        if unmatched:
            raise HTTPException(status_code=400, detail=f"字幕没有同名媒体：{unmatched[0]}")

        batch_id = uuid4().hex
        prepared: list[tuple[dict[str, Any], Path, Path | None]] = []
        try:
            for upload in media:
                subtitle = subtitle_map.get(Path(upload.filename or "").stem.casefold())
                prepared.append(
                    await prepare_task(
                        upload,
                        subtitle,
                        mode,
                        batch_id,
                        hard_subtitles=hard_subtitles,
                        translate_to_chinese=effective_translation,
                        asr_mode=asr_mode,
                        api_hotwords=hotwords,
                        summarize=summarize,
                    )
                )
        except Exception:
            upload_root = resolved_settings.paths.work_dir.resolve() / "uploads" / batch_id
            if upload_root.is_dir():
                shutil.rmtree(upload_root)
            raise

        batch = {
            "batch_id": batch_id,
            "mode": mode.value,
            "hard_subtitles": hard_subtitles,
            "translate_to_chinese": effective_translation,
            "asr_mode": asr_mode.value,
            "api_language": None,
            "api_hotwords": hotwords if asr_mode == ASRMode.API else [],
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "task_ids": [item[0]["task_id"] for item in prepared],
        }
        _write_batch_manifest(resolved_settings, batch)
        for queued, media_path, subtitle_path in prepared:
            submit_task(queued, media_path, subtitle_path, mode)
        return {**batch, "tasks": [item[0] for item in prepared]}

    @app.get("/api/batches/{batch_id}")
    def get_batch(batch_id: str) -> dict[str, Any]:
        batch = _read_batch_manifest(resolved_settings, batch_id)
        return {**batch, "tasks": [task_payload(task_id) for task_id in batch["task_ids"]]}

    @app.get("/api/batches/{batch_id}/export.zip")
    def export_batch(
        batch_id: str,
        types: Annotated[list[ExportType] | None, Query()] = None,
        kind: LegacyExportKind | None = None,
    ) -> FileResponse:
        batch = _read_batch_manifest(resolved_settings, batch_id)
        tasks = [task_payload(task_id) for task_id in batch["task_ids"]]
        if any(task["status"] not in {"completed", "failed", "cancelled"} for task in tasks):
            raise HTTPException(status_code=409, detail="批次仍在处理中")
        completed = [task for task in tasks if task["status"] == "completed"]
        if not completed:
            raise HTTPException(status_code=409, detail="批次中没有可导出的完成任务")

        export_dir = resolved_settings.paths.work_dir.resolve() / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        archive_path = export_dir / f"{batch_id}-{uuid4().hex}.zip"
        selected = _selected_export_files(types, kind)
        exported_count = 0
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            used_folders: set[str] = set()
            for task in completed:
                output_dir = resolved_settings.paths.output_dir.resolve() / task["task_id"]
                stem = _media_stem(task)
                folder = stem
                if folder.casefold() in used_folders:
                    folder = f"{stem}-{task['task_id'][:8]}"
                used_folders.add(folder.casefold())
                for internal_name in selected:
                    source = output_dir / internal_name
                    if source.is_file():
                        archive.write(
                            source,
                            f"{folder}/{_export_filename(task, internal_name)}",
                        )
                        exported_count += 1
        if exported_count == 0:
            archive_path.unlink(missing_ok=True)
            raise HTTPException(status_code=404, detail="所选类型的文件不存在")
        return FileResponse(
            archive_path,
            media_type="application/zip",
            filename=f"video2txt-{batch_id[:8]}.zip",
            background=BackgroundTask(archive_path.unlink, missing_ok=True),
        )

    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

    return app


app = create_app()
