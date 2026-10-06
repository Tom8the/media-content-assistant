from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from video2txt.align.fusion import fuse_timeline
from video2txt.align.timeline import align_timeline
from video2txt.asr.faster_whisper import FasterWhisperEngine
from video2txt.asr.qwen import QwenFileTranscriptionEngine
from video2txt.cleaning import clean_fusion_segments
from video2txt.config import Settings
from video2txt.export.results import export_json, export_srt, export_text
from video2txt.media.audio import normalize_audio
from video2txt.media.probe import (
    probe_media,
    select_audio_stream,
    select_subtitle_stream,
    sha256_file,
)
from video2txt.media.subtitles import extract_text_subtitle
from video2txt.models import (
    ASRMode,
    FusionMode,
    SourceType,
    SubtitleCue,
    SubtitleKind,
    TaskManifest,
    TaskProgress,
    TaskStatus,
    Transcript,
)
from video2txt.ocr.hard_subtitles import HardSubtitleProgress, extract_hard_subtitles
from video2txt.subtitles.parser import parse_subtitle_file
from video2txt.translation import CodexCLIChineseTranslator, NLLBChineseTranslator
from video2txt.summary import SubtitleSummarizer


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        for attempt in range(5):
            try:
                temporary.replace(path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.02)
    finally:
        temporary.unlink(missing_ok=True)


def _write_model(path: Path, model: object) -> None:
    if hasattr(model, "model_dump_json"):
        content = model.model_dump_json(indent=2)  # type: ignore[attr-defined]
    else:
        raise TypeError(f"unsupported model type: {type(model)!r}")
    _write_text_atomic(path, content + "\n")


def _write_subtitles(path: Path, subtitles: list[SubtitleCue]) -> None:
    payload = [cue.model_dump(mode="json") for cue in subtitles]
    _write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _write_json_records(path: Path, records: list[object]) -> None:
    payload = [
        record.model_dump(mode="json") if hasattr(record, "model_dump") else record
        for record in records
    ]
    _write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


class TranscriptionPipeline:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _write_manifest(self, manifest: TaskManifest) -> None:
        manifest.updated_at = _now()
        _write_model(manifest.work_dir / "task.json", manifest)
        if manifest.source_kind or manifest.summarize or manifest.status in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
            _write_model(manifest.output_dir / "task.json", manifest)

    def _set_status(self, manifest: TaskManifest, status: TaskStatus) -> None:
        manifest.status = status
        self._write_manifest(manifest)

    def summarize_existing(self, manifest: TaskManifest) -> TaskManifest:
        from video2txt.models import FusionSegment

        manifest.summarize = True
        manifest.summary_error = None
        manifest.warnings = [w for w in manifest.warnings if not w.startswith("智能总结未完成：")]
        self._set_status(manifest, TaskStatus.SUMMARIZING)
        try:
            payload = json.loads((manifest.output_dir / "fusion.json").read_text(encoding="utf-8"))
            segments = [FusionSegment.model_validate(item) for item in payload["segments"]]
            def progress(current: int, total: int) -> None:
                manifest.progress = TaskProgress(stage="summarizing", current=current, total=total)
                self._write_manifest(manifest)
            report = SubtitleSummarizer(self.settings.codex_translation, manifest.work_dir).generate(
                segments, manifest.original_filename or manifest.input_path.name, progress
            )
            for key, name in (("summary_md", "summary.md"), ("summary_txt", "summary.txt")):
                path = manifest.output_dir / name
                _write_text_atomic(path, report)
                manifest.artifacts[key] = str(path)
        except Exception as error:
            manifest.summary_error = str(error)[:1000]
            manifest.warnings.append("智能总结未完成：" + manifest.summary_error)
        manifest.progress = None
        self._set_status(manifest, TaskStatus.COMPLETED)
        return manifest

    def _asr_cache_path(self, audio: Path, *, asr_mode: ASRMode, api_hotwords: list[str]) -> Path:
        options: dict[str, object] = {"mode": asr_mode.value}
        if asr_mode == ASRMode.LOCAL:
            options["asr"] = self.settings.asr.model_dump(mode="json")
        else:
            options["qwen_asr"] = self.settings.qwen_asr.model_dump(mode="json")
            options["hotwords"] = api_hotwords
        payload = json.dumps(
            {"audio_sha256": sha256_file(audio), "options": options},
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
        key = hashlib.sha256(payload).hexdigest()
        return self.settings.paths.work_dir.resolve() / "cache" / "asr" / f"{key}.json"

    def _transcribe_with_cache(
        self,
        audio: Path,
        manifest: TaskManifest,
        *,
        asr_mode: ASRMode,
        api_hotwords: list[str],
    ) -> Transcript:
        cache_path = self._asr_cache_path(audio, asr_mode=asr_mode, api_hotwords=api_hotwords)
        if cache_path.is_file():
            manifest.warnings.append("ASR cache hit")
            return Transcript.model_validate_json(cache_path.read_text(encoding="utf-8"))
        if asr_mode == ASRMode.LOCAL:
            transcript = FasterWhisperEngine(self.settings.asr).transcribe(audio)
        else:
            transcript = QwenFileTranscriptionEngine(
                self.settings.qwen_asr, hotwords=api_hotwords
            ).transcribe(audio, manifest.work_dir)
        _write_model(cache_path, transcript)
        return transcript

    def run(
        self,
        input_path: Path,
        *,
        output_dir: Path | None = None,
        mode: FusionMode = FusionMode.VERBATIM,
        audio_stream_index: int | None = None,
        subtitle_stream_index: int | None = None,
        external_subtitle: Path | None = None,
        task_id: str | None = None,
        original_filename: str | None = None,
        batch_id: str | None = None,
        hard_subtitles: bool | None = None,
        translate_to_chinese: bool = False,
        asr_mode: ASRMode = ASRMode.LOCAL,
        api_hotwords: list[str] | None = None,
        source_url: str | None = None,
        source_kind: str | None = None,
        source_video_id: str | None = None,
        download_quality: str | None = None,
        recording_minutes: int | None = None,
        summarize: bool = False,
    ) -> TaskManifest:
        source = input_path.resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        resolved_task_id = task_id or uuid4().hex
        work_dir = self.settings.paths.work_dir.resolve() / resolved_task_id
        result_dir = (output_dir or self.settings.paths.output_dir / resolved_task_id).resolve()
        use_hard_subtitles = self.settings.ocr.enabled if hard_subtitles is None else hard_subtitles
        normalized_hotwords = list(
            dict.fromkeys(word.strip() for word in (api_hotwords or []) if word.strip())
        )
        should_translate = translate_to_chinese or asr_mode == ASRMode.API
        created_at = _now()
        manifest = TaskManifest(
            task_id=resolved_task_id,
            status=TaskStatus.QUEUED,
            input_path=source,
            original_filename=original_filename,
            batch_id=batch_id,
            source_url=source_url,
            source_kind=source_kind,
            source_video_id=source_video_id,
            download_quality=download_quality,
            recording_minutes=recording_minutes,
            summarize=summarize,
            work_dir=work_dir,
            output_dir=result_dir,
            mode=mode,
            asr_mode=asr_mode,
            api_language=None,
            api_hotwords=normalized_hotwords if asr_mode == ASRMode.API else [],
            hard_subtitles=use_hard_subtitles,
            translate_to_chinese=should_translate,
            translation_backend=("codex-cli" if asr_mode == ASRMode.API else "nllb")
            if should_translate
            else None,
            created_at=created_at,
            updated_at=created_at,
        )
        work_dir.mkdir(parents=True, exist_ok=True)
        result_dir.mkdir(parents=True, exist_ok=True)
        self._write_manifest(manifest)

        try:
            if asr_mode == ASRMode.API:
                QwenFileTranscriptionEngine(
                    self.settings.qwen_asr,
                    hotwords=normalized_hotwords,
                ).ensure_available()
                CodexCLIChineseTranslator(
                    self.settings.codex_translation, working_dir=work_dir
                ).ensure_available()
            self._set_status(manifest, TaskStatus.PROBING)
            probe = probe_media(source, ffprobe_path=self.settings.ffmpeg.ffprobe_path)
            manifest.input_sha256 = probe.sha256
            _write_model(work_dir / "probe.json", probe)
            _write_model(result_dir / "probe.json", probe)

            audio_stream = select_audio_stream(
                probe,
                stream_index=audio_stream_index,
                language=None if asr_mode == ASRMode.API else self.settings.asr.language,
            )
            manifest.selected_audio_stream = audio_stream.index

            self._set_status(manifest, TaskStatus.EXTRACTING)
            audio_path = normalize_audio(
                source,
                work_dir / "audio.wav",
                ffmpeg_path=self.settings.ffmpeg.ffmpeg_path,
                stream_index=audio_stream.index,
            )
            manifest.artifacts["normalized_audio"] = str(audio_path)

            self._set_status(manifest, TaskStatus.TRANSCRIBING)
            transcript = self._transcribe_with_cache(
                audio_path,
                manifest,
                asr_mode=asr_mode,
                api_hotwords=normalized_hotwords,
            )
            _write_model(work_dir / "asr.json", transcript)
            _write_model(result_dir / "asr.json", transcript)
            manifest.artifacts["asr_json"] = str(result_dir / "asr.json")

            self._set_status(manifest, TaskStatus.SUBTITLE_PROCESSING)
            subtitles: list[SubtitleCue] = []
            if external_subtitle is not None:
                subtitles = parse_subtitle_file(
                    external_subtitle,
                    source=SourceType.EXTERNAL_SUBTITLE,
                    language=self.settings.asr.language,
                )
                manifest.artifacts["subtitle_source"] = str(external_subtitle.resolve())
            else:
                subtitle_stream = select_subtitle_stream(
                    probe,
                    stream_index=subtitle_stream_index,
                    language=self.settings.asr.language,
                )
                if subtitle_stream is not None:
                    manifest.selected_subtitle_stream = subtitle_stream.index
                    if subtitle_stream.subtitle_kind == SubtitleKind.TEXT:
                        extracted = extract_text_subtitle(
                            source,
                            work_dir / "subtitles-extracted.srt",
                            ffmpeg_path=self.settings.ffmpeg.ffmpeg_path,
                            stream_index=subtitle_stream.index,
                        )
                        subtitles = parse_subtitle_file(
                            extracted,
                            source=SourceType.EMBEDDED_TEXT,
                            language=subtitle_stream.language,
                        )
                        manifest.artifacts["extracted_subtitle"] = str(extracted)
                    else:
                        subtitle_kind = subtitle_stream.subtitle_kind.value
                        manifest.warnings.append(
                            f"字幕流 {subtitle_stream.index} 是 {subtitle_kind} "
                            "字幕；画面 OCR 不会渲染该软字幕流"
                        )

                if not subtitles and use_hard_subtitles:
                    if not probe.video_streams:
                        manifest.warnings.append("输入没有视频画面，已跳过硬字幕 OCR")
                    else:
                        ocr_targets = (
                            work_dir / "ocr_observations.json",
                            result_dir / "ocr_observations.json",
                        )
                        partial_observations: list[object] = []
                        last_progress_write = -self.settings.ocr.progress_interval_frames

                        def update_ocr_progress(progress: HardSubtitleProgress) -> None:
                            nonlocal last_progress_write
                            if progress.observation is not None:
                                partial_observations.append(progress.observation)
                            manifest.progress = TaskProgress(
                                stage="hard_subtitle_ocr",
                                current=progress.processed_frames,
                                total=progress.total_frames,
                                ocr_calls=progress.ocr_calls,
                                skipped=progress.skipped_frames,
                            )
                            should_persist = bool(
                                progress.processed_frames == 0
                                or progress.processed_frames == progress.total_frames
                                or progress.processed_frames - last_progress_write
                                >= self.settings.ocr.progress_interval_frames
                            )
                            if not should_persist:
                                return
                            for target in ocr_targets:
                                _write_json_records(target, partial_observations)
                            manifest.artifacts["ocr_observations"] = str(ocr_targets[1])
                            self._write_manifest(manifest)
                            last_progress_write = progress.processed_frames

                        ocr_result = extract_hard_subtitles(
                            source,
                            work_dir / "ocr-frames",
                            self.settings.ocr,
                            ffmpeg_path=self.settings.ffmpeg.ffmpeg_path,
                            duration=probe.duration,
                            progress_callback=update_ocr_progress,
                        )
                        subtitles = ocr_result.cues
                        for target in ocr_targets:
                            _write_json_records(target, ocr_result.observations)
                        manifest.artifacts["ocr_observations"] = str(
                            result_dir / "ocr_observations.json"
                        )
                        manifest.warnings.append(
                            f"硬字幕 OCR 抽取 {ocr_result.frame_count} 帧，"
                            f"执行 {ocr_result.ocr_calls} 次识别，生成 {len(subtitles)} 条字幕"
                        )

            _write_subtitles(work_dir / "subtitle_raw.json", subtitles)
            _write_subtitles(result_dir / "subtitle_raw.json", subtitles)
            manifest.artifacts["subtitle_json"] = str(result_dir / "subtitle_raw.json")

            self._set_status(manifest, TaskStatus.ALIGNING)
            alignment = align_timeline(transcript.segments, subtitles, self.settings.alignment)
            fusion = fuse_timeline(
                alignment,
                transcript.segments,
                subtitles,
                self.settings.alignment,
                mode=mode,
            )
            source_fusion = fusion
            if mode == FusionMode.CLEAN:
                cleaning = clean_fusion_segments(fusion)
                fusion = cleaning.segments
                manifest.warnings.append(
                    f"整理稿调整 {cleaning.changed_segments} 段，"
                    f"移除 {cleaning.removed_duplicates} 个相邻重复和 "
                    f"{cleaning.removed_fillers} 个空填充段，"
                    f"整理为 {cleaning.paragraph_count} 个段落"
                )
            excluded_hard_subtitles = sum(
                segment.decision == "hard_subtitle_unmatched_review" for segment in fusion
            )
            if excluded_hard_subtitles:
                manifest.warnings.append(
                    f"{excluded_hard_subtitles} 条未匹配硬字幕已保留在融合详情中，但未直接写入正文"
                )
            if subtitles:
                matched = sum(group.matched for group in alignment.groups)
                if matched == 0:
                    manifest.warnings.append("ASR 与字幕没有可靠匹配，请检查是否来自同一媒体")

            self._set_status(manifest, TaskStatus.EXPORTING)
            if mode == FusionMode.CLEAN:
                manifest.artifacts["transcript_source_txt"] = str(
                    export_text(source_fusion, result_dir / "transcript_source.txt")
                )
            manifest.artifacts["transcript_txt"] = str(
                export_text(fusion, result_dir / "transcript.txt")
            )
            manifest.artifacts["subtitles_srt"] = str(
                export_srt(fusion, result_dir / "subtitles.srt")
            )
            manifest.artifacts["fusion_json"] = str(
                export_json(alignment, fusion, result_dir / "fusion.json")
            )
            if should_translate:
                self._set_status(manifest, TaskStatus.TRANSLATING)
                if asr_mode == ASRMode.API:
                    translated = CodexCLIChineseTranslator(
                        self.settings.codex_translation, working_dir=work_dir
                    ).translate_segments(fusion)
                else:
                    translated = NLLBChineseTranslator(
                        self.settings.translation
                    ).translate_segments(fusion)
                manifest.artifacts["translated_subtitles_zh_srt"] = str(
                    export_srt(translated, result_dir / "translated_subtitles_zh.srt")
                )
                manifest.artifacts["translated_transcript_zh_txt"] = str(
                    export_text(translated, result_dir / "translated_transcript_zh.txt")
                )

            if summarize:
                return self.summarize_existing(manifest)
            self._set_status(manifest, TaskStatus.COMPLETED)
            return manifest
        except Exception as error:
            manifest.error = str(error)
            self._set_status(manifest, TaskStatus.FAILED)
            raise
