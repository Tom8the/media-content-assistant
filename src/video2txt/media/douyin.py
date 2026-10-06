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
from dataclasses import dataclass
from datetime import date, datetime, timezone
from http.cookiejar import Cookie
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable
from pathlib import Path
from threading import Event, Lock, RLock, Thread
from urllib.parse import parse_qs, unquote, urljoin, urlsplit, urlunsplit

Progress = Callable[[str, int, int], None]
Stop = Callable[[], bool]
MAX_BYTES = 5 * 1024**3
MAX_LIVE_RECORDINGS = 10
USER_AGENT = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1"
PAGE_HOSTS = {
    "douyin.com",
    "www.douyin.com",
    "m.douyin.com",
    "v.douyin.com",
    "live.douyin.com",
    "www.iesdouyin.com",
    "iesdouyin.com",
    "webcast.amemv.com",
}

QUALITY_OPTIONS = {
    "highest", "original", "lowest", "1440p", "1080p", "720p", "540p", "480p", "360p"
}
QUALITY_HEIGHTS = {
    "1440p": 1440,
    "1080p": 1080,
    "720p": 720,
    "540p": 540,
    "480p": 480,
    "360p": 360,
}


@dataclass(frozen=True)
class VideoSource:
    """One downloadable work, resolved from a shared link or a profile page."""

    url: str
    video_id: str
    title: str | None = None
    published_at: date | None = None


@dataclass(frozen=True)
class VideoCandidate:
    url: str
    label: str
    height: int = 0
    bitrate: int = 0


@dataclass(frozen=True)
class DownloadedVideo:
    title: str
    video_id: str
    quality: str


class AcquisitionError(RuntimeError):
    pass


class CookieLoginRequiredError(AcquisitionError):
    """The platform requires an authenticated Douyin browser session."""


_COOKIE_STORE_PATH: Path | None = None
_COOKIE_LOCK = Lock()
_AUTH_COOKIE_NAMES = {"sessionid", "sessionid_ss", "sid_tt", "sid_guard"}


def configure_cookie_store(path: Path) -> None:
    """Set the project-local, ignored location used for a scanned-in Cookie."""
    global _COOKIE_STORE_PATH
    _COOKIE_STORE_PATH = path.resolve()


def _normalise_cookie_map(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    cookies: dict[str, str] = {}
    for name, cookie_value in value.items():
        if not isinstance(name, str) or not isinstance(cookie_value, str):
            continue
        if not name or any(char in name for char in "=;\r\n") or any(char in cookie_value for char in "\r\n;"):
            continue
        cookies[name] = cookie_value
    return cookies


def _stored_cookie_map() -> dict[str, str]:
    path = _COOKIE_STORE_PATH
    if path is None or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return _normalise_cookie_map(payload.get("cookies") if isinstance(payload, dict) else None)


def _cookie_map() -> dict[str, str]:
    value = os.getenv("VIDEO2TXT_DOUYIN_COOKIE", "").strip()
    if value:
        if "\r" in value or "\n" in value:
            raise AcquisitionError("抖音 Cookie 格式无效，请使用单行内容")
        pairs = {}
        for piece in value.split(";"):
            name, separator, cookie_value = piece.strip().partition("=")
            if separator and name:
                pairs[name] = cookie_value
        return _normalise_cookie_map(pairs)
    with _COOKIE_LOCK:
        return _stored_cookie_map()


def _cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{name}={value}" for name, value in cookies.items())


def cookie_configured() -> bool:
    try:
        return bool(_cookie_map())
    except AcquisitionError:
        return False


def save_scanned_cookie(cookies: dict[str, str]) -> None:
    """Persist only browser cookies locally; no Cookie value is sent to the web UI."""
    cleaned = _normalise_cookie_map(cookies)
    if not cleaned or not _AUTH_COOKIE_NAMES.intersection(cleaned):
        raise AcquisitionError("扫码尚未完成，请在抖音页面完成登录后重试")
    path = _COOKIE_STORE_PATH
    if path is None:
        raise AcquisitionError("抖音 Cookie 存储位置未配置")
    payload = {
        "version": 1,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "cookies": cleaned,
    }
    with _COOKIE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
    # The current service can use the new login immediately. It is never exposed
    # in an API response and a restart falls back to the project-local store.
    os.environ["VIDEO2TXT_DOUYIN_COOKIE"] = _cookie_header(cleaned)


def requires_cookie_login(error: BaseException) -> bool:
    message = str(error).lower()
    return any(marker in message for marker in (
        "验证", "安全校验", "安全验证", "cookie", "分享页未提供视频地址",
        "登录后", "登录验证",
    ))


class DouyinCookieLogin:
    """A short-lived local browser session for scanning a Douyin login QR code."""

    def __init__(self, timeout_seconds: int = 300) -> None:
        self._timeout_seconds = timeout_seconds
        self._lock = RLock()
        self._cancel = Event()
        self._thread: Thread | None = None
        self._state = "idle"
        self._message = "尚未开始扫码登录"
        self._started_at: str | None = None
        self._preview: bytes | None = None

    def status(self) -> dict[str, object]:
        with self._lock:
            return {
                "state": self._state,
                "message": self._message,
                "started_at": self._started_at,
                "preview_available": self._preview is not None and self._state in {"opening", "waiting"},
                "cookie_configured": cookie_configured(),
            }

    def preview(self) -> bytes | None:
        with self._lock:
            return self._preview if self._state in {"opening", "waiting"} else None

    def start(self) -> dict[str, object]:
        if importlib.util.find_spec("playwright") is None:
            with self._lock:
                self._state = "unavailable"
                self._message = "扫码组件尚未安装，请更新项目依赖后重启服务"
                self._preview = None
            return self.status()
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self.status()
            self._cancel = Event()
            self._state = "opening"
            self._message = "正在打开抖音登录窗口…"
            self._started_at = datetime.now().astimezone().isoformat(timespec="seconds")
            self._preview = None
            self._thread = Thread(target=self._run, name="douyin-cookie-login", daemon=True)
            self._thread.start()
        return self.status()

    def cancel(self) -> dict[str, object]:
        self._cancel.set()
        with self._lock:
            if self._state in {"opening", "waiting"}:
                self._state = "cancelled"
                self._message = "已取消扫码登录"
                self._preview = None
        return self.status()

    def _set_state(self, state: str, message: str, preview: bytes | None = None) -> None:
        with self._lock:
            self._state = state
            self._message = message
            if preview is not None:
                self._preview = preview

    @staticmethod
    def _douyin_cookies(context) -> dict[str, str]:
        return {
            item["name"]: item["value"]
            for item in context.cookies()
            if isinstance(item.get("name"), str)
            and isinstance(item.get("value"), str)
            and str(item.get("domain", "")).lstrip(".").endswith("douyin.com")
        }

    def _run(self) -> None:
        browser = None
        playwright = None
        try:
            from playwright.sync_api import sync_playwright

            playwright = sync_playwright().start()
            try:
                # Prefer the user's installed Chrome: its normal browser behavior is
                # less likely to trigger a platform risk-control page.
                browser = playwright.chromium.launch(channel="chrome", headless=False)
            except Exception:
                browser = playwright.chromium.launch(headless=False)
            context = browser.new_context(viewport={"width": 980, "height": 760})
            page = context.new_page()
            page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=45_000)
            try:
                page.get_by_text("登录", exact=True).first.click(timeout=5_000)
            except Exception:
                # The page layout changes often. The opened browser still lets the
                # user select its login button when this optional click misses.
                pass
            self._set_state("waiting", "请使用手机抖音扫描弹出的登录窗口中的二维码；完成后会自动保存。")
            deadline = time.monotonic() + self._timeout_seconds
            while not self._cancel.is_set() and time.monotonic() < deadline:
                cookies = self._douyin_cookies(context)
                if _AUTH_COOKIE_NAMES.intersection(cookies):
                    save_scanned_cookie(cookies)
                    self._set_state("success", "抖音登录成功，已保存到本项目，后续任务会自动使用。")
                    return
                try:
                    preview = page.screenshot(type="png")
                    self._set_state("waiting", "请使用手机抖音扫描弹出的登录窗口中的二维码；完成后会自动保存。", preview)
                except Exception:
                    # The native window remains usable even when a page preview cannot be captured.
                    pass
                time.sleep(2)
            if self._cancel.is_set():
                return
            self._set_state("expired", "二维码等待超时，请重新打开扫码登录。")
        except Exception as error:
            self._set_state("failed", "无法打开抖音扫码窗口。请确认已安装扫码组件后重试。")
        finally:
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
            if playwright is not None:
                try:
                    playwright.stop()
                except Exception:
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


def _is_profile_url(url: str) -> bool:
    parts = urlsplit(url)
    return (
        parts.hostname in {"douyin.com", "www.douyin.com"}
        and bool(re.fullmatch(r"/user/[^/]+/?", parts.path))
    )


def _video_id_from_url(url: str) -> str | None:
    parts = urlsplit(url)
    match = re.search(r"/(?:share/)?video/(\d+)", parts.path)
    if match:
        return match[1]
    value = (parse_qs(parts.query).get("modal_id") or [""])[0]
    return value if value.isdigit() else None


def _validated_url(url: str, kind: str, *, allow_profile: bool = False) -> str:
    if kind not in {"video", "live"}:
        raise AcquisitionError("请选择抖音视频或抖音直播")
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
    if kind == "live" and _video_id_from_url(url):
        raise AcquisitionError("这是视频链接，请选择抖音视频")
    if parts.hostname in {"douyin.com", "www.douyin.com"} and not (
        _video_id_from_url(url) or (allow_profile and _is_profile_url(url))
    ):
        raise AcquisitionError(
            "请使用单个视频链接、账号主页链接或 live.douyin.com 直播间链接"
        )
    return url


def extract_url(text: str, kind: str) -> str:
    urls = _share_urls(text)
    if len(urls) != 1:
        raise AcquisitionError("请粘贴一个抖音分享链接，可包含分享文案")
    return _validated_url(urls[0], kind)


def _cookie() -> str:
    return _cookie_header(_cookie_map())


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


def resolve_video_source(url: str) -> VideoSource:
    """Resolve a shared link once so queue deduplication uses the work ID, not the URL."""
    import requests

    validated = _validated_url(url, "video")
    known_id = _video_id_from_url(validated)
    if known_id:
        return VideoSource(
            url=f"https://www.douyin.com/video/{known_id}", video_id=known_id
        )
    with requests.Session() as session:
        final_url, _ = _page(session, validated)
    video_id = _video_id_from_url(final_url)
    if not video_id:
        raise AcquisitionError("未找到单个视频，请确认链接不是图集、主页或直播间")
    return VideoSource(
        url=f"https://www.douyin.com/video/{video_id}", video_id=video_id
    )


def _entry_published_at(entry: dict[str, object]) -> date | None:
    value = entry.get("timestamp")
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, tz=timezone.utc).date()
    value = entry.get("upload_date")
    if isinstance(value, str) and re.fullmatch(r"\d{8}", value):
        try:
            return datetime.strptime(value, "%Y%m%d").date()
        except ValueError:
            return None
    return None


def _profile_sources(url: str, *, start_date: date | None, end_date: date | None, limit: int) -> list[VideoSource]:
    """Expand a Douyin profile through yt-dlp without downloading any media yet."""
    from yt_dlp import YoutubeDL

    # Date filtering needs a wider window than the requested output count: the first
    # few recent works may all be outside the requested period.
    scan_limit = 500 if start_date or end_date else limit
    options = {
        "quiet": True,
        "noplaylist": False,
        "extract_flat": True,
        "playlistend": scan_limit,
        "socket_timeout": 20,
        "retries": 1,
        "extractor_retries": 1,
    }
    try:
        with YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=False)
    except Exception as error:
        raise AcquisitionError(
            "账号主页作品获取失败，请检查链接；如遇平台验证，请配置抖音 Cookie 后重试"
        ) from error
    entries = info.get("entries") if isinstance(info, dict) else None
    if not isinstance(entries, list):
        raise AcquisitionError("账号主页未返回可下载作品")
    result: list[VideoSource] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        video_id = str(entry.get("id") or "")
        candidate_url = str(entry.get("webpage_url") or entry.get("original_url") or "")
        if not video_id.isdigit():
            video_id = _video_id_from_url(candidate_url) or ""
        if not video_id.isdigit() or video_id in seen:
            continue
        published_at = _entry_published_at(entry)
        if start_date and (published_at is None or published_at < start_date):
            continue
        if end_date and (published_at is None or published_at > end_date):
            continue
        seen.add(video_id)
        result.append(
            VideoSource(
                url=f"https://www.douyin.com/video/{video_id}",
                video_id=video_id,
                title=str(entry.get("title") or "").strip() or None,
                published_at=published_at,
            )
        )
        if len(result) >= limit:
            break
    if not result:
        range_label = "所选日期范围内" if start_date or end_date else ""
        raise AcquisitionError(f"账号主页{range_label}没有可下载的视频")
    return result


def expand_video_sources(
    text: str,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int = 50,
) -> list[VideoSource]:
    """Turn direct links and profile links into unique individual video sources."""
    if not 1 <= limit <= 50:
        raise AcquisitionError("单次最多下载 50 个视频")
    if start_date and end_date and start_date > end_date:
        raise AcquisitionError("开始日期不能晚于结束日期")
    candidates = _share_urls(text)
    if not candidates:
        raise AcquisitionError("未识别到链接，请粘贴抖音分享文案、视频链接或账号主页链接")
    sources: list[VideoSource] = []
    seen: set[str] = set()
    for index, candidate in enumerate(candidates, 1):
        try:
            validated = _validated_url(candidate, "video", allow_profile=True)
            expanded = (
                _profile_sources(validated, start_date=start_date, end_date=end_date, limit=limit)
                if _is_profile_url(validated)
                else [resolve_video_source(validated)]
            )
        except AcquisitionError as error:
            raise AcquisitionError(f"第 {index} 个链接：{error}") from None
        for source in expanded:
            if source.video_id in seen:
                continue
            seen.add(source.video_id)
            sources.append(source)
            if len(sources) >= limit:
                return sources
    if not sources:
        raise AcquisitionError("没有可下载的视频")
    return sources


def _address_urls(address: object) -> list[str]:
    if not isinstance(address, dict):
        return []
    urls = address.get("url_list") or address.get("urlList") or []
    return [str(url) for url in urls if isinstance(url, str) and url.startswith(("http://", "https://"))]


def _candidate_metric(entry: dict[str, object], address: dict[str, object]) -> tuple[int, int]:
    def number(value: object) -> int:
        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0

    height = number(entry.get("height") or address.get("height"))
    width = number(entry.get("width") or address.get("width"))
    if height <= 0 and width > 0:
        height = width
    return height, number(entry.get("bit_rate") or entry.get("bitRate") or address.get("data_size"))


def _ordered_candidates(video: dict[str, object], quality: str) -> list[VideoCandidate]:
    """Choose page-provided bitrate tracks first and retain every URL as a fallback."""
    tracks: list[VideoCandidate] = []
    primary = video.get("play_addr") or video.get("playAddr") or {}
    if isinstance(primary, dict):
        height, bitrate = _candidate_metric(video, primary)
        tracks.extend(VideoCandidate(url, "最高可用", height, bitrate) for url in _address_urls(primary))
    bitrates = video.get("bit_rate") or video.get("bitRate") or []
    if isinstance(bitrates, list):
        for entry in bitrates:
            if not isinstance(entry, dict):
                continue
            address = entry.get("play_addr") or entry.get("playAddr") or {}
            if not isinstance(address, dict):
                continue
            height, bitrate = _candidate_metric(entry, address)
            label = f"{height}p" if height else "码率备选"
            tracks.extend(VideoCandidate(url, label, height, bitrate) for url in _address_urls(address))
    # Work metadata occasionally exposes a distinct download/original address.
    originals = [VideoCandidate(url, "原片", 0, 0) for url in _address_urls(video.get("download_addr") or video.get("downloadAddr"))]
    uri = ""
    if isinstance(primary, dict):
        uri = str(primary.get("uri") or "")
    uri = uri or str(video.get("vid") or "")
    if uri:
        originals.append(VideoCandidate(
            f"https://aweme.snssdk.com/aweme/v1/play/?video_id={uri}&ratio=default&line=0&watermark=0",
            "原片",
        ))
    if quality not in QUALITY_OPTIONS:
        raise AcquisitionError("不支持该下载清晰度")
    unique: list[VideoCandidate] = []
    seen: set[str] = set()
    for candidate in tracks:
        url = _unwatermarked_play_url(candidate.url)
        if url not in seen:
            seen.add(url)
            unique.append(VideoCandidate(url, candidate.label, candidate.height, candidate.bitrate))
    if quality == "highest":
        unique.sort(key=lambda item: (item.height, item.bitrate), reverse=True)
    elif quality == "lowest":
        unique.sort(key=lambda item: (item.height or 99_999, item.bitrate or 99_999))
    elif quality in QUALITY_HEIGHTS:
        target = QUALITY_HEIGHTS[quality]
        unique.sort(key=lambda item: (0 if item.height <= target else 1, abs(item.height - target), -item.bitrate))
    elif quality == "original":
        for candidate in originals:
            url = _unwatermarked_play_url(candidate.url)
            if url not in seen:
                seen.add(url)
                unique.insert(0, VideoCandidate(url, "原片", candidate.height, candidate.bitrate))
        unique.sort(key=lambda item: 0 if item.label == "原片" else 1)
    return unique


def parse_share_video(html: str, video_id: str, quality: str = "highest") -> tuple[list[VideoCandidate], str]:
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
                candidates = _ordered_candidates(video, quality)
                if candidates:
                    return candidates, str(item.get("desc") or f"抖音视频_{video_id}")
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
            raise AcquisitionError("视频下载不完整")


def _download_candidates(
    session,
    candidates: list[VideoCandidate],
    target: Path,
    progress: Progress,
    stop: Stop,
    retry_times: int,
) -> VideoCandidate:
    if not candidates:
        raise AcquisitionError("抖音页面未提供可用的视频地址")
    last_error: AcquisitionError | None = None
    for attempt in range(retry_times + 1):
        for candidate in candidates:
            target.unlink(missing_ok=True)
            try:
                _download_media(session, candidate.url, target, progress, stop)
                return candidate
            except AcquisitionError as error:
                target.unlink(missing_ok=True)
                if stop():
                    raise error
                last_error = error
        if attempt < retry_times:
            # A new request to a playback endpoint can select another CDN node.
            time.sleep(min(2**attempt, 4))
    if last_error is not None:
        raise AcquisitionError(f"所有视频地址下载失败：{last_error}")
    raise AcquisitionError("所有视频地址下载失败")


def download_video(
    url: str,
    target: Path,
    progress: Progress,
    stop: Stop,
    *,
    quality: str = "highest",
    retry_times: int = 3,
) -> DownloadedVideo:
    import requests
    from yt_dlp import YoutubeDL

    if quality not in QUALITY_OPTIONS:
        raise AcquisitionError("不支持该下载清晰度")
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".part")
    page_parse_failed = False
    try:
        with requests.Session() as session:
            final_url, html = _page(session, url)
            video_id = _video_id_from_url(final_url) or _video_id_from_url(url) or ""
            if not video_id.isdigit():
                raise AcquisitionError(
                    "未找到单个视频，请确认链接不是图集、主页或直播间"
                )
            try:
                candidates, title = parse_share_video(html, video_id, quality)
            except AcquisitionError:
                page_parse_failed = True
                _, html = _page(
                    session, f"https://www.iesdouyin.com/share/video/{video_id}/"
                )
                try:
                    candidates, title = parse_share_video(html, video_id, quality)
                except AcquisitionError:
                    # Keep extraction separate from download to enforce size/redirect limits.
                    class QuietLogger:
                        def debug(self, *_):
                            pass

                        def warning(self, *_):
                            pass

                        def error(self, *_):
                            pass

                    format_selector = {
                        "highest": "best[ext=mp4]/best",
                        "original": "best[ext=mp4]/best",
                        "lowest": "worst[ext=mp4]/worst",
                    }.get(quality, f"best[height<={QUALITY_HEIGHTS.get(quality, 1080)}][ext=mp4]/best")
                    options = {
                        "quiet": True,
                        "logger": QuietLogger(),
                        "noplaylist": True,
                        "socket_timeout": 20,
                        "retries": retry_times,
                        "extractor_retries": retry_times,
                        "format": format_selector,
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
                    candidates, title = (
                        [VideoCandidate(str(info["url"]), quality)],
                        info.get("title") or f"抖音视频_{video_id}",
                    )
            selected = _download_candidates(session, candidates, partial, progress, stop, retry_times)
        partial.replace(target)
        return DownloadedVideo(title=title, video_id=video_id, quality=selected.label)
    except AcquisitionError as error:
        if requires_cookie_login(error):
            raise CookieLoginRequiredError("抖音需要登录验证，请扫码登录后重试") from None
        raise
    except Exception:
        if page_parse_failed:
            raise CookieLoginRequiredError("抖音需要登录验证，请扫码登录后重试") from None
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
    except Exception as error:
        if requires_cookie_login(error):
            raise CookieLoginRequiredError("抖音需要登录验证，请扫码登录后重试") from None
        raise AcquisitionError(
            "直播解析失败：请检查直播间链接、网络和 Node.js"
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
