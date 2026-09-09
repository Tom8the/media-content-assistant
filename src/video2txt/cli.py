from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from pydantic import TypeAdapter

from video2txt import __version__
from video2txt.align.fusion import fuse_timeline
from video2txt.align.timeline import align_timeline
from video2txt.asr.faster_whisper import FasterWhisperEngine
from video2txt.config import load_settings
from video2txt.export.results import export_json, export_srt, export_text
from video2txt.media.audio import normalize_audio
from video2txt.media.probe import probe_media
from video2txt.media.subtitles import extract_text_subtitle
from video2txt.models import ASRMode, FusionMode, SourceType, SubtitleCue, Transcript
from video2txt.pipeline import TranscriptionPipeline
from video2txt.subtitles.parser import parse_subtitle_file
from video2txt.translation.models import install_nllb_model

app = typer.Typer(no_args_is_help=True, help="本地视频/音频转写与字幕融合工具。")


@app.command()
def version() -> None:
    """显示当前版本。"""
    typer.echo(__version__)


@app.command("show-config")
def show_config(
    config: Annotated[str | None, typer.Option("--config", help="TOML 配置文件路径。")] = None,
) -> None:
    """显示解析后的非敏感配置。"""
    settings = load_settings(config)
    typer.echo(settings.model_dump_json(indent=2))


@app.command()
def probe(
    input_path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    config: Annotated[Path | None, typer.Option("--config", help="TOML 配置文件路径。")] = None,
    output: Annotated[Path | None, typer.Option("--output", help="将探测结果写入 JSON。")] = None,
) -> None:
    """使用 FFprobe 检查媒体、音轨和字幕流。"""
    settings = load_settings(config)
    result = probe_media(input_path, ffprobe_path=settings.ffmpeg.ffprobe_path)
    serialized = result.model_dump_json(indent=2)
    if output is None:
        typer.echo(serialized)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(serialized + "\n", encoding="utf-8")
    typer.echo(str(output.resolve()))


@app.command("extract-audio")
def extract_audio_command(
    input_path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    output: Annotated[Path, typer.Option("--output", "-o", help="输出 WAV 路径。")],
    stream_index: Annotated[
        int | None, typer.Option("--audio-stream", help="FFprobe 返回的全局音轨索引。")
    ] = None,
    config: Annotated[Path | None, typer.Option("--config", help="TOML 配置文件路径。")] = None,
) -> None:
    """提取并标准化为单声道 16 kHz PCM WAV。"""
    settings = load_settings(config)
    result = normalize_audio(
        input_path,
        output,
        ffmpeg_path=settings.ffmpeg.ffmpeg_path,
        stream_index=stream_index,
    )
    typer.echo(str(result))


@app.command("transcribe-audio")
def transcribe_audio_command(
    input_path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    output: Annotated[Path, typer.Option("--output", "-o", help="输出 ASR JSON 路径。")],
    config: Annotated[Path | None, typer.Option("--config", help="TOML 配置文件路径。")] = None,
    model_path: Annotated[
        Path | None, typer.Option("--model-path", help="本地 faster-whisper 模型目录。")
    ] = None,
) -> None:
    """转写一个音频文件并保存完整词级结果。"""
    settings = load_settings(config)
    if model_path is not None:
        settings.asr.model_path = model_path
    transcript = FasterWhisperEngine(settings.asr).transcribe(input_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(transcript.model_dump_json(indent=2) + "\n", encoding="utf-8")
    typer.echo(str(output.resolve()))


@app.command("extract-subtitles")
def extract_subtitles_command(
    input_path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    output: Annotated[Path, typer.Option("--output", "-o", help="输出 SRT 路径。")],
    stream_index: Annotated[
        int | None, typer.Option("--subtitle-stream", help="FFprobe 返回的全局字幕流索引。")
    ] = None,
    config: Annotated[Path | None, typer.Option("--config", help="TOML 配置文件路径。")] = None,
) -> None:
    """从媒体中提取文本字幕流。"""
    settings = load_settings(config)
    result = extract_text_subtitle(
        input_path,
        output,
        ffmpeg_path=settings.ffmpeg.ffmpeg_path,
        stream_index=stream_index,
    )
    typer.echo(str(result))


@app.command("parse-subtitles")
def parse_subtitles_command(
    input_path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    output: Annotated[Path, typer.Option("--output", "-o", help="输出字幕 JSON 路径。")],
    language: Annotated[str | None, typer.Option("--language", help="字幕语言标记。")] = None,
) -> None:
    """解析 SRT/ASS/VTT 并清理显示样式。"""
    cues = parse_subtitle_file(input_path, source=SourceType.EXTERNAL_SUBTITLE, language=language)
    import json

    output.parent.mkdir(parents=True, exist_ok=True)
    payload = [cue.model_dump(mode="json") for cue in cues]
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    typer.echo(str(output.resolve()))


@app.command("fuse-results")
def fuse_results_command(
    asr_json: Annotated[Path, typer.Option("--asr-json", exists=True, dir_okay=False)],
    subtitle_json: Annotated[Path, typer.Option("--subtitle-json", exists=True, dir_okay=False)],
    output_dir: Annotated[Path, typer.Option("--output-dir", "-o")],
    mode: Annotated[FusionMode, typer.Option("--mode")] = FusionMode.VERBATIM,
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """对齐 ASR 与字幕 JSON，并导出融合结果。"""
    settings = load_settings(config)
    transcript = Transcript.model_validate_json(asr_json.read_text(encoding="utf-8"))
    subtitle_payload = subtitle_json.read_text(encoding="utf-8")
    subtitles = TypeAdapter(list[SubtitleCue]).validate_json(subtitle_payload)
    alignment = align_timeline(transcript.segments, subtitles, settings.alignment)
    segments = fuse_timeline(
        alignment,
        transcript.segments,
        subtitles,
        settings.alignment,
        mode=mode,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    export_text(segments, output_dir / "transcript.txt")
    export_srt(segments, output_dir / "subtitles.srt")
    export_json(alignment, segments, output_dir / "fusion.json")
    typer.echo(str(output_dir.resolve()))


@app.command()
def transcribe(
    input_path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    output_dir: Annotated[Path | None, typer.Option("--output-dir", "-o")] = None,
    mode: Annotated[FusionMode, typer.Option("--mode")] = FusionMode.VERBATIM,
    audio_stream: Annotated[int | None, typer.Option("--audio-stream")] = None,
    subtitle_stream: Annotated[int | None, typer.Option("--subtitle-stream")] = None,
    external_subtitle: Annotated[
        Path | None, typer.Option("--external-subtitle", exists=True, dir_okay=False)
    ] = None,
    hard_subtitles: Annotated[
        bool, typer.Option("--hard-subtitles", help="识别画面下方的硬字幕。")
    ] = False,
    translate_to_chinese: Annotated[
        bool, typer.Option("--translate-to-chinese", help="额外导出中文字幕 SRT。")
    ] = False,
    engine: Annotated[
        ASRMode,
        typer.Option(
            "--engine", help="local 使用 faster-whisper；api 使用千问 ASR 与 Codex CLI 翻译。"
        ),
    ] = ASRMode.LOCAL,
    api_hotword: Annotated[
        list[str] | None,
        typer.Option(
            "--api-hotword", help="API 模式热词；可重复传入，例如 --api-hotword RedotPay。"
        ),
    ] = None,
    model_path: Annotated[Path | None, typer.Option("--model-path")] = None,
    config: Annotated[Path | None, typer.Option("--config")] = None,
    task_id: Annotated[str | None, typer.Option("--task-id")] = None,
) -> None:
    """运行探测、转写、字幕提取、对齐、融合和导出的完整管线。"""
    settings = load_settings(config)
    if model_path is not None:
        settings.asr.model_path = model_path
    manifest = TranscriptionPipeline(settings).run(
        input_path,
        output_dir=output_dir,
        mode=mode,
        audio_stream_index=audio_stream,
        subtitle_stream_index=subtitle_stream,
        external_subtitle=external_subtitle,
        hard_subtitles=hard_subtitles,
        translate_to_chinese=translate_to_chinese,
        asr_mode=engine,
        api_hotwords=api_hotword,
        task_id=task_id,
    )
    typer.echo(manifest.model_dump_json(indent=2))


@app.command("install-translation-models")
def install_translation_models(
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """下载一次阿拉伯语/英语到中文的离线翻译模型。"""
    settings = load_settings(config)
    typer.echo(str(install_nllb_model(settings.translation.model_path)))


@app.command()
def serve(
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 8765,
    model_path: Annotated[Path | None, typer.Option("--model-path")] = None,
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """启动本地 Web 操作界面。"""
    try:
        import uvicorn
    except ImportError as error:
        raise typer.BadParameter('未安装 Web 依赖，请执行 pip install -e ".[web]"') from error

    from video2txt.web.app import create_app

    settings = load_settings(config)
    if model_path is not None:
        settings.asr.model_path = model_path
    uvicorn.run(create_app(settings), host=host, port=port, log_level="info")


if __name__ == "__main__":
    app()
