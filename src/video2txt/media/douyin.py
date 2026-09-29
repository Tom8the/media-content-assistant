"""Douyin acquisition adapters. Only completed media is handed to transcription."""

from __future__ import annotations

import asyncio
import importlib.util
import ipaddress
import json
import os
import re
import socket
import shutil
import subprocess
import time
from http.cookiejar import Cookie
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlsplit, urlunsplit

Progress = Callable[[str, int, int], None]
Stop = Callable[[], bool]
MAX_BYTES = 5 * 1024**3
MAX_LIVE_RECORDINGS = 10
USER_AGENT = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1"
PAGE_HOSTS = {
    "douyin.com",
    "www.douyin.com",
    "v.douyin.com",
    "live.douyin.com",
    "www.iesdouyin.com",
    "iesdouyin.com",
    "webcast.amemv.com",
}


class AcquisitionError(RuntimeError):
    pass


def available(kind: str) -> bool:
    modules = ("requests", "yt_dlp") if kind == "video" else ("streamget",)
    return all(importlib.util.find_spec(name) is not None for name in modules)


def _share_urls(text: str) -> list[str]:
    # Share captions often have newlines before the URL or Chinese text directly
    # after it. Keep URL queries/escaped paths while excluding caption punctuation.
    return list(dict.fromkeys(
        match.rstrip(".,;!?)\u005d}\u3001\"'")
        for match in re.findall(r"https?://[^\s<>\"'\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]+", text)
    ))


def extract_urls(text: str, kind: str) -> list[str]:
    candidates = _share_urls(text)
    if not candidates:
        raise AcquisitionError("未识别到链接，请粘贴抖音分享文案或完整链接")
    limit = 50 if kind == "video" else MAX_LIVE_RECORDINGS
    if len(candidates) > limit:
        raise AcquisitionError(f"一次最多提交 {limit} 个不同的抖音链接")
    result = []
    for index, candidate in enumerate(candidates, 1):
        try:
            result.append(extract_url(candidate, kind))
        except (ValueError, AcquisitionError) as error:
            raise AcquisitionError(f"第 {index} 个链接：{error}") from None
    return result


def extract_url(text: str, kind: str) -> str:
    if kind not in {"video", "live"}:
        raise AcquisitionError("请选择抖音视频或抖音直播")
    urls = _share_urls(text)
    if len(urls) != 1:
        raise AcquisitionError("请粘贴一个抖音分享链接，可包含分享文案")
    url = urls[0]
    parts = urlsplit(url)
    if (
        parts.hostname not in PAGE_HOSTS
        or parts.username
        or parts.password
        or parts.port not in (None, 80, 443)
    ):
        raise AcquisitionError("仅支持抖音官方视频或直播间链接")
    if kind == "video" and parts.hostname == "live.douyin.com":
        raise AcquisitionError("这是直播间链接，请选择抖音直播")
    if kind == "live" and re.search(r"/(?:share/)?video/\d+", parts.path):
        raise AcquisitionError("这是视频链接，请选择抖音视频")
    if parts.hostname in {"douyin.com", "www.douyin.com"} and not (
        re.search(r"/video/\d+", parts.path) or parse_qs(parts.query).get("modal_id")
    ):
        raise AcquisitionError(
            "请使用单个视频链接或 live.douyin.com 直播间链接，不支持账号主页"
        )
    return url


def _cookie() -> str:
    value = os.getenv("VIDEO2TXT_DOUYIN_COOKIE", "").strip()
    if "\r" in value or "\n" in value:
        raise AcquisitionError("抖音 Cookie 格式无效，请使用单行内容")
    return value


def _check_public_url(url: str) -> None:
    parts = urlsplit(url)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
    ):
        raise AcquisitionError("平台返回了不支持的媒体地址")
    addresses = socket.getaddrinfo(
        parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)
    )
    if not addresses or any(
        not ipaddress.ip_address(item[4][0]).is_global for item in addresses
    ):
        raise AcquisitionError("媒体地址不是公网地址")


def _get(session, url: str, *, page: bool = False):
    # Follow redirects explicitly so cookies never go to an unrelated media host.
    for _ in range(8):
        if page and urlsplit(url).hostname not in PAGE_HOSTS:
            raise AcquisitionError("分享链接跳转到了非抖音页面")
        _check_public_url(url)
        headers = {"User-Agent": USER_AGENT, "Referer": "https://www.douyin.com/"}
        if page and _cookie():
            headers["Cookie"] = _cookie()
        response = session.get(
            url, headers=headers, stream=True, allow_redirects=False, timeout=(15, 30)
        )
        if response.is_redirect:
            url = urljoin(url, response.headers.get("Location", ""))
            response.close()
            continue
        response.raise_for_status()
        return response
    raise AcquisitionError("分享链接跳转次数过多，请重新复制链接")


def _page(session, url: str) -> tuple[str, str]:
    with _get(session, url, page=True) as response:
        body = bytearray()
        for chunk in response.iter_content(65536):
            body.extend(chunk)
            if len(body) > 8 * 1024**2:
                raise AcquisitionError("抖音页面异常，请重新复制链接")
        return response.url, body.decode("utf-8", errors="replace")


def parse_share_video(html: str, video_id: str) -> tuple[str, str]:
    """Read page JSON, never execute scripts received from the platform."""
    payloads = []
    for match in re.finditer(r"(?:window\.|self\.)?_ROUTER_DATA\s*=\s*", html):
        try:
            payloads.append(json.JSONDecoder().raw_decode(html[match.end() :])[0])
        except ValueError:
            continue
    for match in re.finditer(
        r"<script[^>]+id=[\"\']RENDER_DATA[\"\'][^>]*>(.*?)</script>", html, re.S
    ):
        try:
            payloads.append(json.loads(unquote(match[1])))
        except ValueError:
            continue
    stack = payloads[:]
    while stack:
        item = stack.pop()
        if isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, dict):
            if str(item.get("aweme_id", item.get("awemeId", ""))) == video_id:
                video = item.get("video") or {}
                address = video.get("play_addr") or video.get("playAddr") or {}
                urls = address.get("url_list") or address.get("urlList") or []
                if urls:
                    return str(urls[0]), str(item.get("desc") or f"抖音视频_{video_id}")
            stack.extend(
                value for value in item.values() if isinstance(value, (dict, list))
            )
    raise AcquisitionError("分享页未提供视频地址")


def _unwatermarked_play_url(url: str) -> str:
    """Select Douyin's playback endpoint without modifying signed CDN URLs."""
    parts = urlsplit(url)
    if parts.hostname in {"aweme.snssdk.com", "www.iesdouyin.com", "iesdouyin.com"} and parts.path.rstrip("/") == "/aweme/v1/playwm":
        return urlunsplit(parts._replace(path=parts.path.replace("/playwm", "/play")))
    return url


def _download_media(
    session, url: str, target: Path, progress: Progress, stop: Stop
) -> None:
    with _get(session, url) as response, target.open("wb") as handle:
        content_type = response.headers.get("Content-Type", "").lower()
        if "text/html" in content_type or "application/json" in content_type:
            raise AcquisitionError("抖音返回了验证页面，请更新 Cookie 后重试")
        total = int(response.headers.get("Content-Length") or 0)
        if total > MAX_BYTES:
            raise AcquisitionError("视频超过 5 GB 下载限制")
        current = 0
        reported = 0.0
        for chunk in response.iter_content(1024 * 1024):
            if stop():
                raise AcquisitionError("下载已停止，可以重试")
            current += len(chunk)
            if current > MAX_BYTES:
                raise AcquisitionError("视频超过 5 GB 下载限制")
            handle.write(chunk)
            if time.monotonic() - reported > 1:
                progress("downloading", current, total)
                reported = time.monotonic()
        if not current or (total and current != total):
            raise AcquisitionError("视频下载不完整，请重试")


def download_video(url: str, target: Path, progress: Progress, stop: Stop) -> str:
    import requests
    from yt_dlp import YoutubeDL

    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".part")
    try:
        with requests.Session() as session:
            final_url, html = _page(session, url)
            parts = urlsplit(final_url)
            match = re.search(r"/(?:share/)?video/(\d+)", parts.path)
            video_id = (
                match[1]
                if match
                else (parse_qs(parts.query).get("modal_id") or [""])[0]
            )
            if not video_id.isdigit():
                raise AcquisitionError(
                    "未找到单个视频，请确认链接不是图集、主页或直播间"
                )
            try:
                media_url, title = parse_share_video(html, video_id)
            except AcquisitionError:
                _, html = _page(
                    session, f"https://www.iesdouyin.com/share/video/{video_id}/"
                )
                try:
                    media_url, title = parse_share_video(html, video_id)
                except AcquisitionError:
                    # Keep extraction separate from download to enforce size/redirect limits.
                    class QuietLogger:
                        def debug(self, *_):
                            pass

                        def warning(self, *_):
                            pass

                        def error(self, *_):
                            pass

                    options = {
                        "quiet": True,
                        "logger": QuietLogger(),
                        "noplaylist": True,
                        "socket_timeout": 20,
                        "retries": 1,
                        "extractor_retries": 1,
                        "format": "best[ext=mp4]/best",
                    }
                    with YoutubeDL(options) as downloader:
                        for pair in _cookie().split(";"):
                            name, separator, value = pair.strip().partition("=")
                            if separator and name:
                                downloader.cookiejar.set_cookie(
                                    Cookie(
                                        version=0,
                                        name=name,
                                        value=value,
                                        port=None,
                                        port_specified=False,
                                        domain=".douyin.com",
                                        domain_specified=True,
                                        domain_initial_dot=True,
                                        path="/",
                                        path_specified=True,
                                        secure=True,
                                        expires=None,
                                        discard=True,
                                        comment=None,
                                        comment_url=None,
                                        rest={},
                                    )
                                )
                        info = downloader.extract_info(
                            f"https://www.douyin.com/video/{video_id}", download=False
                        )
                    media_url, title = (
                        info["url"],
                        info.get("title") or f"抖音视频_{video_id}",
                    )
            _download_media(session, _unwatermarked_play_url(media_url), partial, progress, stop)
        partial.replace(target)
        return title
    except AcquisitionError:
        raise
    except Exception:
        raise AcquisitionError(
            "抖音视频下载失败：请检查网络、链接是否可播放；如遇验证，请配置或更新 VIDEO2TXT_DOUYIN_COOKIE 后重试"
        ) from None
    finally:
        partial.unlink(missing_ok=True)


async def _live_info(url: str):
    from streamget import DouyinLiveStream

    live = DouyinLiveStream(cookies=_cookie() or None)
    if urlsplit(url).hostname == "live.douyin.com":
        data = await live.fetch_web_stream_data(url)
    else:
        data = await live.fetch_app_stream_data(url)
    return await live.fetch_stream_url(data, "OD")


LIVE_SEGMENT_SECONDS = 3600
LIVE_STATUS_INTERVAL_SECONDS = 30


def _live_status(url: str):
    return asyncio.run(asyncio.wait_for(_live_info(url), timeout=15))


def _finish_recording(process) -> bool:
    try:
        process.stdin.write(b"q\n")
        process.stdin.flush()
        process.wait(timeout=20)
        return process.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        process.kill()
        process.wait()
        return False


def record_live(
    url: str, target: Path, minutes: int, ffmpeg: str, progress: Progress, stop: Stop
) -> str:
    if not 0 <= minutes <= 480:
        raise AcquisitionError("录制时长需为 0～480 分钟，0 表示录到下播")
    try:
        info = _live_status(url)
    except Exception:
        raise AcquisitionError(
            "直播解析失败：请检查直播间链接、网络和 Node.js；如遇验证请更新抖音 Cookie"
        ) from None
    if not info.is_live:
        raise AcquisitionError("主播当前未开播，请开播后重新提交")
    stream_url = info.flv_url or info.m3u8_url
    if not stream_url:
        raise AcquisitionError("直播间未返回可录制的视频流")
    _check_public_url(stream_url)
    if stop():
        raise AcquisitionError("录制已停止，尚未收到直播内容")
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f"{target.stem}.part-%06d.mkv")
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-rw_timeout",
        "15000000",
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_delay_max",
        "5",
        "-user_agent",
        "Mozilla/5.0",
        "-referer",
        "https://live.douyin.com/",
        "-i",
        stream_url,
        *(["-t", str(minutes * 60)] if minutes else []),
        "-map",
        "0:v:0?",
        "-map",
        "0:a:0",
        "-c",
        "copy",
        "-f",
        "segment",
        "-segment_time",
        str(LIVE_SEGMENT_SECONDS),
        "-segment_format",
        "matroska",
        "-reset_timestamps",
        "1",
        str(partial),
    ]
    process = None
    monitor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="live-status")
    pending = None
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        start = time.monotonic()
        next_check = start + LIVE_STATUS_INTERVAL_SECONDS
        offline_checks = 0
        stopped = False
        while process.poll() is None:
            elapsed = int(time.monotonic() - start)
            progress("recording", elapsed, minutes * 60)
            if stop() or (minutes and elapsed >= minutes * 60 + 20):
                stopped = True
                if not _finish_recording(process):
                    raise AcquisitionError("结束录制超时，部分分段文件已保留")
                break
            now = time.monotonic()
            if pending is not None and pending.done():
                try:
                    offline_checks = offline_checks + 1 if not pending.result().is_live else 0
                except Exception:
                    offline_checks = 0
                pending = None
                next_check = now + LIVE_STATUS_INTERVAL_SECONDS
                if offline_checks >= 2:
                    stopped = True
                    if not _finish_recording(process):
                        raise AcquisitionError("直播已下播，但录制文件收尾失败，部分文件已保留")
                    break
            if pending is None and now >= next_check:
                pending = monitor.submit(_live_status, url)
            if shutil.disk_usage(target.parent).free < 512 * 1024**2:
                _finish_recording(process)
                raise AcquisitionError(
                    "磁盘剩余空间不足 512 MB，已停止录制并保留分段文件"
                )
            time.sleep(1)
        if process.returncode != 0 and not stopped:
            try:
                ended = not _live_status(url).is_live
            except Exception:
                ended = False
            if not ended:
                raise AcquisitionError("直播连接中断或录制失败，已保留分段文件；请检查网络后重试")
        parts = sorted(target.parent.glob(f"{target.stem}.part-*.mkv"))
        parts = [part for part in parts if part.stat().st_size >= 1024]
        if not parts:
            raise AcquisitionError("未录到有效直播内容，请检查是否已下播")
        # The first file is published last, signalling that all parts are ready.
        for index, part in enumerate(parts[1:], 2):
            part.replace(target.with_name(f"{target.stem}-segment-{index:06d}.mkv"))
        parts[0].replace(target)
        return str(info.anchor_name or "抖音直播")
    except FileNotFoundError:
        raise AcquisitionError("未找到 FFmpeg，请检查项目配置") from None
    finally:
        monitor.shutdown(wait=False, cancel_futures=True)
        if process is not None:
            if process.poll() is None:
                process.kill()
                process.wait()
            if process.stdin:
                process.stdin.close()
