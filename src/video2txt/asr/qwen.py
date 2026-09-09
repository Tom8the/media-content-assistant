"""Qwen file-transcription adapter used by the API pipeline."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse
from uuid import uuid4

from video2txt.config import QwenASRSettings
from video2txt.media.probe import sha256_file
from video2txt.models import Transcript, TranscriptSegment, TranscriptWord


class QwenASRError(RuntimeError):
    """Raised when Qwen cannot produce a safe, usable transcript."""


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", "utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise QwenASRError(f"无法读取 Qwen 结果：{path.name}") from error
    if not isinstance(payload, dict):
        raise QwenASRError(f"Qwen 结果格式无效：{path.name}")
    return payload


class _QwenClient:
    def __init__(self, api_key: str, base_url: str) -> None:
        try:
            import requests
        except ImportError as error:
            raise QwenASRError('未安装 API 依赖，请执行 pip install -e ".[api]"') from error
        self.requests = requests
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    def request_json(
        self, method: str, url: str, *, auth: bool = False, **kwargs: Any
    ) -> dict[str, Any]:
        headers = dict(kwargs.pop("headers", {}))
        if auth:
            headers["Authorization"] = f"Bearer {self.api_key}"
        attempts = 3 if method == "GET" else 1
        for attempt in range(attempts):
            try:
                response = self.requests.request(
                    method,
                    url,
                    headers=headers,
                    timeout=(15, 90),
                    allow_redirects=False,
                    **kwargs,
                )
            except self.requests.RequestException as error:
                if attempt + 1 < attempts:
                    time.sleep(2**attempt)
                    continue
                raise QwenASRError("Qwen 网络请求中断；提交请求不会自动重试。") from error
            if response.status_code in {429, 500, 502, 503, 504} and attempt + 1 < attempts:
                time.sleep(2**attempt)
                continue
            try:
                payload = response.json()
            except ValueError as error:
                raise QwenASRError(
                    f"Qwen 返回非 JSON 数据（HTTP {response.status_code}）。"
                ) from error
            if not isinstance(payload, dict):
                raise QwenASRError("Qwen 返回的 JSON 顶层必须为对象。")
            if not 200 <= response.status_code < 300:
                code = re.sub(r"[^A-Za-z0-9_.-]", "", str(payload.get("code", "Unknown")))[:120]
                message = str(payload.get("message", ""))[:240]
                raise QwenASRError(
                    f"Qwen 请求失败：HTTP {response.status_code}，{code} {message}".strip()
                )
            return payload
        raise AssertionError("unreachable")

    def upload(self, audio: Path, model: str) -> str:
        policy_response = self.request_json(
            "GET",
            f"{self.base_url}/uploads",
            auth=True,
            params={"action": "getPolicy", "model": model},
        )
        policy = policy_response.get("data", {})
        required = {
            "upload_host",
            "upload_dir",
            "oss_access_key_id",
            "signature",
            "policy",
            "x_oss_object_acl",
            "x_oss_forbid_overwrite",
        }
        if not isinstance(policy, dict) or not required.issubset(policy):
            raise QwenASRError("Qwen 临时上传凭证字段不完整，请检查模型权限和地域。")
        host = str(policy["upload_host"])
        if urlparse(host).scheme != "https":
            raise QwenASRError("Qwen 临时上传凭证未返回 HTTPS 地址。")
        key = (
            f"{str(policy['upload_dir']).rstrip('/')}/{uuid4().hex}{audio.suffix.lower() or '.wav'}"
        )
        fields = {
            "OSSAccessKeyId": policy["oss_access_key_id"],
            "Signature": policy["signature"],
            "policy": policy["policy"],
            "x-oss-object-acl": policy["x_oss_object_acl"],
            "x-oss-forbid-overwrite": policy["x_oss_forbid_overwrite"],
            "key": key,
            "success_action_status": "200",
        }
        try:
            with audio.open("rb") as media:
                response = self.requests.post(
                    host, data=fields, files={"file": media}, timeout=(15, 300)
                )
        except self.requests.RequestException as error:
            raise QwenASRError("Qwen 临时音频上传中断，尚未提交识别任务；可重新运行。") from error
        if response.status_code != 200:
            raise QwenASRError(f"Qwen 临时音频上传失败：HTTP {response.status_code}。")
        return f"oss://{key}"

    def submit(self, file_url: str, model: str, parameters: dict[str, Any]) -> dict[str, Any]:
        return self.request_json(
            "POST",
            f"{self.base_url}/services/audio/asr/transcription",
            auth=True,
            headers={"X-DashScope-Async": "enable", "X-DashScope-OssResourceResolve": "enable"},
            json={"model": model, "input": {"file_urls": [file_url]}, "parameters": parameters},
        )

    def fetch(self, task_id: str) -> dict[str, Any]:
        return self.request_json(
            "GET", f"{self.base_url}/tasks/{quote(task_id, safe='')}", auth=True
        )

    def download(self, url: str) -> dict[str, Any]:
        if urlparse(url).scheme != "https":
            raise QwenASRError("Qwen 结果下载链接不是 HTTPS 地址。")
        return self.request_json("GET", url)


class QwenFileTranscriptionEngine:
    """Upload a normalized local audio file and map Qwen timestamps to Transcript."""

    def __init__(self, settings: QwenASRSettings, *, hotwords: list[str]) -> None:
        if len(hotwords) > 2000:
            raise QwenASRError("热词最多 2000 个。")
        self.settings = settings
        self.hotwords = hotwords

    def _base_url(self) -> str:
        workspace = (self.settings.workspace_id or "").strip()
        if workspace:
            if not re.fullmatch(r"[A-Za-z0-9-]+", workspace):
                raise QwenASRError("Qwen workspace_id 只能含字母、数字和连字符。")
            region = "cn-beijing" if self.settings.region == "beijing" else "ap-southeast-1"
            return f"https://{workspace}.{region}.maas.aliyuncs.com/api/v1"
        host = (
            "dashscope.aliyuncs.com"
            if self.settings.region == "beijing"
            else "dashscope-intl.aliyuncs.com"
        )
        return f"https://{host}/api/v1"

    def _client(self) -> _QwenClient:
        api_key = os.getenv(self.settings.api_key_environment, "").strip()
        if not api_key:
            raise QwenASRError(
                f"尚未设置 {self.settings.api_key_environment}；API Key 不会写入配置或结果文件。"
            )
        return _QwenClient(api_key, self._base_url())

    def ensure_available(self) -> None:
        self._client()

    def _parameters(self) -> dict[str, Any]:
        vocabulary = {word: self.settings.hotword_weight for word in self.hotwords}
        result: dict[str, Any] = {
            "channel_id": [0],
            "diarization_enabled": False,
        }
        if vocabulary:
            result["vocabulary"] = vocabulary
        return result

    def _to_transcript(self, audio: Path, payload: dict[str, Any]) -> Transcript:
        transcripts = payload.get("transcripts", [])
        if (
            not isinstance(transcripts, list)
            or len(transcripts) != 1
            or not isinstance(transcripts[0], dict)
        ):
            raise QwenASRError("Qwen 结果未返回预期的单声道转写。")
        sentences = transcripts[0].get("sentences", [])
        if not isinstance(sentences, list):
            raise QwenASRError("Qwen 结果缺少句级时间戳。")
        segments: list[TranscriptSegment] = []
        for index, sentence in enumerate(sentences, start=1):
            if not isinstance(sentence, dict):
                continue
            text = str(sentence.get("text", "")).strip()
            start, end = sentence.get("begin_time"), sentence.get("end_time")
            if (
                not text
                or type(start) is not int
                or type(end) is not int
                or start < 0
                or end <= start
            ):
                raise QwenASRError("Qwen 返回无效句级时间戳，已保留原始结果供复核。")
            words: list[TranscriptWord] = []
            for word in sentence.get("words", []):
                if not isinstance(word, dict):
                    continue
                word_text = str(word.get("text", "")).strip()
                word_start, word_end = word.get("begin_time"), word.get("end_time")
                if (
                    word_text
                    and type(word_start) is int
                    and type(word_end) is int
                    and word_end >= word_start >= 0
                ):
                    words.append(
                        TranscriptWord(start=word_start / 1000, end=word_end / 1000, text=word_text)
                    )
            segments.append(
                TranscriptSegment(
                    id=f"qwen-{index:04d}",
                    start=start / 1000,
                    end=end / 1000,
                    text=text,
                    words=words,
                )
            )
        if not segments:
            raise QwenASRError("Qwen 未返回可用的句级转写。")
        return Transcript(
            engine="qwen-api",
            model=self.settings.model,
            language=(str(transcripts[0].get("language", "")).strip() or None),
            audio_sha256=sha256_file(audio),
            options={
                "language_detection": "auto",
                "hotwords": self.hotwords,
                "region": self.settings.region,
            },
            segments=segments,
        )

    def transcribe(self, audio_path: Path, state_dir: Path) -> Transcript:
        audio = audio_path.resolve()
        if not audio.is_file():
            raise FileNotFoundError(audio)
        raw_path, state_path, task_path = (
            state_dir / "qwen_asr_raw.json",
            state_dir / "qwen_asr_state.json",
            state_dir / "qwen_asr_task.json",
        )
        if raw_path.is_file():
            return self._to_transcript(audio, _read_json(raw_path))
        state = (
            _read_json(state_path)
            if state_path.is_file()
            else {"status": "new", "parameters": self._parameters()}
        )
        if state.get("parameters") != self._parameters():
            raise QwenASRError("已有 Qwen 任务的语言或热词参数不一致，拒绝覆盖。")
        task_id = state.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            if state.get("status") == "submitting":
                raise QwenASRError("上次 Qwen 提交结果不确定；请先在百炼控制台核对，避免重复计费。")
            client = self._client()
            file_url = client.upload(audio, self.settings.model)
            state["status"] = "submitting"
            _write_json(state_path, state)
            response = client.submit(file_url, self.settings.model, self._parameters())
            _write_json(state_dir / "qwen_asr_submit.json", response)
            task_id = response.get("output", {}).get("task_id")
            if not isinstance(task_id, str) or not task_id:
                raise QwenASRError("Qwen 未返回任务 ID；已保留提交状态，避免自动重复提交。")
            state.update(status="submitted", task_id=task_id)
            _write_json(state_path, state)
        client = self._client()
        deadline = time.monotonic() + self.settings.wait_timeout_seconds
        while True:
            response = client.fetch(task_id)
            _write_json(task_path, response)
            status = response.get("output", {}).get("task_status")
            state["status"] = status
            _write_json(state_path, state)
            if status == "SUCCEEDED":
                results = response.get("output", {}).get("results", [])
                if (
                    not isinstance(results, list)
                    or len(results) != 1
                    or results[0].get("subtask_status") != "SUCCEEDED"
                ):
                    raise QwenASRError("Qwen 子任务失败或结果数量异常。")
                result_url = results[0].get("transcription_url")
                if not isinstance(result_url, str) or not result_url:
                    raise QwenASRError("Qwen 结果缺少下载地址。")
                payload = client.download(result_url)
                _write_json(raw_path, payload)
                return self._to_transcript(audio, payload)
            if status not in {"PENDING", "RUNNING"}:
                raise QwenASRError(f"Qwen 任务状态异常：{status or 'UNKNOWN'}。")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise QwenASRError("等待 Qwen 任务超时；任务 ID 已保存，重新运行可继续查询。")
            time.sleep(min(self.settings.poll_interval_seconds, remaining))
