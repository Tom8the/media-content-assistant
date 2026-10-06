const $ = (selector) => document.querySelector(selector);
const terminalStatuses = new Set(["completed", "failed", "cancelled"]);
const statusLabels = {
  queued: "等待处理",
  downloading: "获取抖音素材",
  recording: "直播录制中",
  probing: "分析媒体",
  extracting: "提取音轨",
  transcribing: "语音转写",
  subtitle_processing: "处理字幕",
  aligning: "时间轴对齐",
  exporting: "生成结果",
  translating: "翻译中文字幕",
  summarizing: "生成智能总结",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};
const modeLabels = { verbatim: "逐字稿", subtitle: "字幕稿", clean: "整理稿" };
const estimateRealtimeFactor = 0.22;
const estimateHardSubtitleFactor = 0.9;
const estimateSecondsPerMB = 1.85;
const apiHotwordsStorageKey = "video2txt.api-hotwords.v1";

const unavailableThumbnails = new Set();
let queueFilter = "all";
let currentBatchId = null;
let currentBatch = null;
let currentTaskId = null;
let pollTimer = null;
let historyPage = 1;
const historyPageSize = 12;
let healthState = null;
let douyinLoginTimer = null;
const cookieLoginPromptedTasks = new Set();

function closeDouyinLoginModal() {
  clearInterval(douyinLoginTimer);
  douyinLoginTimer = null;
  $("#douyin-login-modal").hidden = true;
}

function showDouyinLoginStatus(status) {
  $("#douyin-login-status").textContent = status.message || "正在获取抖音登录二维码…";
  const preview = $("#douyin-login-preview");
  if (status.preview_available) {
    preview.hidden = false;
    preview.src = `/api/douyin/cookie-login/preview?t=${Date.now()}`;
  } else {
    preview.hidden = true;
    preview.removeAttribute("src");
  }
  if (status.state === "success") {
    healthState = { ...healthState, douyin_cookie_configured: true };
    updateDouyinAvailability();
    clearInterval(douyinLoginTimer);
    douyinLoginTimer = null;
    window.setTimeout(() => {
      closeDouyinLoginModal();
      loadHealth();
    }, 900);
  }
}

async function refreshDouyinLoginStatus() {
  try {
    const status = await requestJson("/api/douyin/cookie-login", { cache: "no-store" });
    showDouyinLoginStatus(status);
    if (!["opening", "waiting"].includes(status.state)) {
      clearInterval(douyinLoginTimer);
      douyinLoginTimer = null;
    }
  } catch (error) {
    $("#douyin-login-status").textContent = error.message;
  }
}

async function openDouyinLoginModal() {
  $("#douyin-login-modal").hidden = false;
  $("#douyin-login-status").textContent = "正在打开抖音登录窗口…";
  try {
    const status = await requestJson("/api/douyin/cookie-login/start", { method: "POST" });
    showDouyinLoginStatus(status);
    clearInterval(douyinLoginTimer);
    if (["opening", "waiting"].includes(status.state)) {
      douyinLoginTimer = setInterval(refreshDouyinLoginStatus, 1500);
      window.setTimeout(refreshDouyinLoginStatus, 500);
    }
  } catch (error) {
    $("#douyin-login-status").textContent = error.message;
  }
}

async function cancelDouyinLogin() {
  try { await requestJson("/api/douyin/cookie-login/cancel", { method: "POST" }); } catch { /* Closing the local dialog must still work. */ }
  closeDouyinLoginModal();
}

function promptForCookieLogin(tasks) {
  const blocked = tasks.find((task) => task.cookie_login_required && !cookieLoginPromptedTasks.has(task.task_id));
  if (!blocked) return;
  cookieLoginPromptedTasks.add(blocked.task_id);
  openDouyinLoginModal();
}

function selectedSource() {
  return document.querySelector('input[name="input_source"]:checked')?.value || "file";
}

function downloadOnly() {
  return selectedSource() !== "file" && document.querySelector('input[name="douyin_action"]:checked')?.value === "download";
}

function wantsSummary() {
  return document.querySelector('input[name="douyin_action"]:checked')?.value === "summary";
}

function updateSourceControls() {
  const source = selectedSource();
  $("#download-only-option").hidden = source === "file";
  if (source === "file" && document.querySelector('input[name="douyin_action"]:checked')?.value === "download") {
    document.querySelector('input[name="douyin_action"][value="transcribe"]').checked = true;
  }
  $("#transcribe-action-label").textContent = source === "file" ? "转字幕" : "下载后转字幕";
  $("#summary-hint").hidden = !wantsSummary();
  $("#local-inputs").hidden = source !== "file";
  $("#douyin-inputs").hidden = source === "file";
  $("#recording-options").hidden = source !== "live";
  $("#video-download-options").hidden = source !== "video";
  $("#media-input").required = source === "file";
  $("#douyin-url").required = source !== "file";
  $("#douyin-url").disabled = source === "file";
  $("#recording-minutes").disabled = source !== "live";
  $("#douyin-url").rows = 3;
  const liveLimit = healthState?.max_live_recordings || 10;
  $("#download-quality-select").disabled = source !== "video";
  $("#profile-limit-input").disabled = source !== "video";
  $("#profile-start-date").disabled = source !== "video";
  $("#profile-end-date").disabled = source !== "video";
  $("#skip-downloaded-input").disabled = source !== "video";
  $("#redownload-missing-input").disabled = source !== "video";
  $("#douyin-url").placeholder = source === "video" ? "粘贴抖音视频、分享文案或账号主页链接。主页可按日期批量获取，自动按作品去重，最多 50 个视频。" : `粘贴多个直播间链接或分享文案，可用回车分隔。自动提取并去重，最多 ${liveLimit} 个直播间同时录制。`;
  document.querySelector(".file-limit").textContent = source === "live" ? `最多同时 ${liveLimit} 个直播间 · 每小时分段` : "最多 50 个 · 单个 5 GB";
  $("#douyin-hint").textContent = source === "live"
    ? "0 表示不限时，录到下播；也可随时结束录制。约每小时保存一段，结束后逐段转字幕。请保持服务运行。"
    : "支持单视频和账号主页批量下载；下载完成后自动转写，并保留原视频供下载。";
  $("#submit-button span").textContent = source === "live" ? "批量录制并转字幕" : source === "video" ? "批量下载并转字幕" : "开始批量提取与转写";
  $("#transcription-options").hidden = downloadOnly();
  if (downloadOnly()) {
    $("#douyin-hint").textContent = source === "live" ? "0 表示不限时，录到下播；也可随时结束录制。约每小时保存一个 MKV 文件，结束后可分别下载。请保持服务运行。" : "下载并保存原视频，不进行语音识别或字幕转写。";
    $("#submit-button span").textContent = source === "live" ? "开始批量录制" : "开始批量下载";
  }
  updateAsrControls();
  if (wantsSummary()) $("#submit-button span").textContent = source === "live" ? "批量录制、转字幕并总结" : source === "video" ? "批量下载、转字幕并总结" : "开始转字幕并总结";
}

function updateDouyinAvailability() {
  if (wantsSummary() && !healthState?.summary_available) {
    $("#submit-button").disabled = true;
    $("#summary-hint").textContent = "智能总结尚未就绪，请安装并登录 Codex CLI 后重启服务。";
  } else {
    $("#summary-hint").textContent = "通过已登录的 Codex 调用大模型，字幕文本会发送给模型处理；长直播按录制片段分别生成报告。";
  }
  const source = selectedSource();
  if (source === "file") return;
  const available = healthState?.[source === "live" ? "douyin_live_available" : "douyin_video_available"];
  $("#douyin-health").textContent = !available ? "抖音下载组件尚未就绪，请安装后重启服务。"
    : healthState?.douyin_cookie_configured ? "已配置抖音访问凭据。" : "尚未登录抖音，开始下载或录制前请扫码登录。";
  $("#douyin-login-button").disabled = !healthState?.douyin_cookie_login_available;
  $("#douyin-login-button").textContent = healthState?.douyin_cookie_configured ? "更新抖音登录" : "扫码登录抖音";
  if (!available) $("#submit-button").disabled = true;
}

function restoreApiHotwords() {
  try {
    const saved = localStorage.getItem(apiHotwordsStorageKey);
    if (saved !== null) $("#api-hotwords-input").value = saved;
  } catch {
    // The default hotwords remain available when browser storage is disabled.
  }
}

function saveApiHotwords() {
  try {
    localStorage.setItem(apiHotwordsStorageKey, $("#api-hotwords-input").value);
  } catch {
    // Saving hotwords must never prevent a transcription task from being submitted.
  }
}

function selectedAsrMode() {
  return $("#asr-mode-select").value || "local";
}

function updateAsrControls() {
  if (downloadOnly()) {
    $("#submit-button").disabled = false;
    updateDouyinAvailability();
    return;
  }
  const isApi = selectedAsrMode() === "api";
  const health = healthState || {};
  $("#api-asr-options").hidden = !isApi;
  const translation = $("#translate-to-chinese-input");
  const translationHint = $("#translation-hint");
  if (isApi) {
    translation.checked = true;
    translation.disabled = true;
    translationHint.textContent = "API 模式固定由本机 Codex CLI 翻译为简体中文；会同时导出中文 TXT 和 SRT。";
    $("#submit-button").disabled = !health.api_pipeline_available;
    updateDouyinAvailability();
    return;
  }
  translation.disabled = !health.translation_models_available;
  translationHint.textContent = health.translation_models_available
    ? "开始提取前勾选才会生成中文文件；本地模式使用离线 NLLB 翻译。"
    : "离线翻译模型尚未安装";
  $("#submit-button").disabled = !health.model_configured;
  updateDouyinAvailability();
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes)) return "";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function ocrProgress(task) {
  const progress = task.progress;
  if (progress?.stage !== "hard_subtitle_ocr" || !progress.total) return null;
  const percent = Math.min(100, Math.round(progress.current / progress.total * 100));
  return {
    percent,
    short: `OCR ${progress.current}/${progress.total}`,
    detail: `OCR ${progress.current}/${progress.total} · ${progress.ocr_calls} 次识别 · 跳过 ${progress.skipped} 帧`,
  };
}

function taskStatusLabel(task) {
  if (task.status === "completed" && task.summary_error) return "字幕完成 · 总结待重试";
  if (task.status === "summarizing" && task.progress?.total) return `智能总结 ${task.progress.current}/${task.progress.total}`;
  if (task.status === "recording" && task.progress) return task.progress.total
    ? `录制 ${Math.floor(task.progress.current / 60)} / ${Math.ceil(task.progress.total / 60)} 分钟`
    : `已录制 ${Math.floor(task.progress.current / 60)} 分钟 · 不限时`;
  if (task.status === "downloading" && task.progress?.total) return `下载 ${Math.min(100, Math.round(task.progress.current / task.progress.total * 100))}%`;
  return ocrProgress(task)?.short || statusLabels[task.status] || task.status;
}

function selectedExportTypes(attribute) {
  return [...document.querySelectorAll(`input[${attribute}]:checked`)].map((input) => input.value);
}

function exportQuery(types) {
  return types.map((type) => `types=${encodeURIComponent(type)}`).join("&");
}

function exportTypeLabel(types) {
  const labels = { text: "文本", subtitle: "原字幕", translation: "中文字幕", translation_text: "中文文本", summary: "总结报告" };
  return types.map((type) => labels[type] || type).join(" + ");
}

function estimatedTaskSeconds(task) {
  if (Number.isFinite(task.media_duration) && task.media_duration > 0) {
    const factor = task.hard_subtitles ? estimateHardSubtitleFactor : estimateRealtimeFactor;
    return Math.max(15, task.media_duration * factor + 8);
  }
  return Math.max(20, (task.media_size || 0) / 1024 / 1024 * estimateSecondsPerMB);
}

function formatRemaining(seconds) {
  const minutes = Math.max(1, Math.ceil(seconds / 60));
  if (minutes < 60) return `${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours} 小时 ${rest} 分钟` : `${hours} 小时`;
}

function renderBatchEstimate(tasks, allFinished) {
  if (allFinished) {
    $("#batch-estimate").textContent = "本批次处理已结束";
    return;
  }
  if (tasks.some((task) => task.source_kind && ["queued", "downloading", "recording"].includes(task.status))) {
    $("#batch-estimate").textContent = "媒体获取完成后开始转写；直播录制可提前停止。";
    return;
  }
  let remaining = 0;
  for (const task of tasks) {
    if (terminalStatuses.has(task.status)) continue;
    let taskRemaining = estimatedTaskSeconds(task);
    if (task.status !== "queued" && task.created_at) {
      const elapsed = Math.max(0, (Date.now() - new Date(task.created_at).getTime()) / 1000);
      taskRemaining = Math.max(5, taskRemaining - elapsed);
    }
    remaining += taskRemaining;
  }
  if (!remaining) {
    $("#batch-estimate").textContent = "预计完成时间：正在计算";
    return;
  }
  const completion = new Date(Date.now() + remaining * 1000).toLocaleString("zh-CN", {
    month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false,
  });
  $("#batch-estimate").textContent = `预计完成时间：${completion}（约 ${formatRemaining(remaining)}）`;
}

function showPanel(name) {
  ["completed", "failed"].forEach((panel) => {
    $(`#${panel}-result`).hidden = panel !== name;
  });
}

function updateFileLabel() {
  const files = [...$("#media-input").files];
  const zone = $("#media-dropzone");
  if (!files.length) {
    zone.classList.remove("has-file");
    $("#media-title").textContent = "批量拖入视频或音频";
    $("#media-meta").textContent = "支持一次选择多个 MP4、MKV、MOV、MP3、WAV";
    return;
  }
  const totalSize = files.reduce((sum, file) => sum + file.size, 0);
  zone.classList.add("has-file");
  $("#media-title").textContent = files.length === 1 ? files[0].name : `已选择 ${files.length} 个媒体文件`;
  $("#media-meta").textContent = `${formatBytes(totalSize)} · 已准备批量上传`;
}

function updateSubtitleLabel() {
  const files = [...$("#subtitle-input").files];
  $("#subtitle-name").textContent = files.length
    ? `已选择 ${files.length} 个字幕 · 将按同名媒体配对`
    : "可批量选择，按同名媒体自动配对";
}

function renderTaskRow(task, index, library = false) {

    const canView = terminalStatuses.has(task.status);
    const source = task.source_kind === "live" ? "抖音直播" : task.source_kind === "video" ? "抖音视频" : "本地文件";
    const treatment = task.transcribe_after_download === false ? "仅下载视频" : task.summarize ? "转字幕并智能总结" : task.source_kind ? "下载后转字幕" : "转字幕";
    const details = task.transcribe_after_download === false ? ["保留原视频", task.download_quality ? `清晰度：${task.download_quality}` : ""].filter(Boolean).join(" · ") : [modeLabels[task.mode], task.asr_mode === "api" ? "千问 API" : "本地模型", task.download_quality ? `清晰度：${task.download_quality}` : "", task.hard_subtitles ? "硬字幕识别" : "", task.translate_to_chinese ? "中文字幕" : ""].filter(Boolean).join(" · ");
    const progress = task.progress;
    const measurable = progress?.total > 0;
    const percent = task.status === "completed" ? 100 : measurable ? Math.max(0, Math.min(100, Math.round(progress.current / progress.total * 100))) : null;
    const progressText = percent !== null ? `${percent}%` : task.status === "recording" ? "持续录制" : task.status === "failed" ? "未完成" : task.status === "queued" ? "等待中" : "处理中";
    const meta = [task.created_at?.replace("T", " ").slice(0, 16), formatBytes(task.media_size)].filter(Boolean).join(" · ");
    const duration = task.media_duration ? `${Math.floor(task.media_duration / 60)}:${String(Math.floor(task.media_duration % 60)).padStart(2, "0")}` : "";
    const thumbKey = `${task.task_id}:${task.status}:${Math.floor(Date.now() / 30000)}`;
    const thumbnail = `<div class="task-thumbnail"><span aria-hidden="true">${task.source_kind === "live" ? "◉" : "▷"}</span>${unavailableThumbnails.has(thumbKey) ? "" : `<img data-thumb-key="${escapeHtml(thumbKey)}" src="/api/tasks/${encodeURIComponent(task.task_id)}/thumbnail?v=${Math.floor(Date.now() / 30000)}" alt="" loading="lazy" />`}${duration ? `<small>${duration}</small>` : ""}</div>`;
    return `<article class="queue-item" role="row">
      <div class="queue-number" role="cell">${index + 1}</div>
      <div class="queue-content" role="cell">${thumbnail}<div><strong title="${escapeHtml(task.original_filename)}">${escapeHtml(task.original_filename || task.task_id)}</strong><small>${escapeHtml(meta)}</small>${task.status === "failed" ? `<small class="queue-error">${escapeHtml(task.error || "处理失败")}</small>` : ""}</div></div>
      <div role="cell"><span class="source-badge">${source}</span></div>
      <div class="queue-treatment" role="cell"><strong>${treatment}</strong><small>${escapeHtml(details)}</small></div>
      ${library ? "" : `<div class="queue-meter" role="cell"><div class="queue-progress ${percent === null && !terminalStatuses.has(task.status) ? "indeterminate" : ""}" ${percent === null ? '' : `role="progressbar" aria-valuenow="${percent}" aria-valuemin="0" aria-valuemax="100"`} aria-label="${escapeHtml(progressText)}"><span style="width:${percent ?? 30}%"></span></div><small>${progressText}</small></div>
      <div role="cell"><span class="queue-status ${escapeHtml(task.status)}">${escapeHtml(taskStatusLabel(task))}</span></div>`}
      <div class="queue-actions" role="cell">
      ${task.source_kind === "live" && ["queued", "downloading", "recording"].includes(task.status) && !task.source_media_available ? `<button type="button" data-stop-task-id="${escapeHtml(task.task_id)}">结束录制</button>` : ""}
      ${task.source_media_available ? `<a href="/api/tasks/${escapeHtml(task.task_id)}/source">下载原视频</a>` : ""}
      <button type="button" ${library ? "data-task-id" : "data-batch-task-id"}="${escapeHtml(task.task_id)}" ${canView ? "" : "disabled"}>${task.summary_preview ? "查看报告" : "查看结果"}</button>
      ${library && task.status === "completed" && task.transcribe_after_download !== false ? `<a href="/api/tasks/${escapeHtml(task.task_id)}/export?types=text&types=subtitle${task.translate_to_chinese ? "&types=translation&types=translation_text" : ""}${task.summarize && !task.summary_error ? "&types=summary" : ""}">下载文件</a>` : ""}
      ${library && task.status === "failed" ? `<button type="button" data-retry-task-id="${escapeHtml(task.task_id)}">重试</button>` : ""}
      ${library ? `<button type="button" class="danger-button" data-delete-task-id="${escapeHtml(task.task_id)}">删除</button>` : ""}
      </div>
    </article>`;

}

function renderBatch(batch) {
  showPanel("batch");
  currentBatch = batch;
  loadHistory();
}

function renderCompleted(task) {
  currentTaskId = task.task_id;
  showPanel("completed");
  $("#task-state").textContent = "已完成";
  const onlyVideo = task.transcribe_after_download === false;
  $("#completed-mode").textContent = onlyVideo ? "仅下载视频" : modeLabels[task.mode] || task.mode;
  $(".transcript-panel").hidden = onlyVideo;
  $("#download-row").hidden = onlyVideo;
  $("#completed-file-count").textContent = onlyVideo ? "视频文件" : task.translate_to_chinese ? "TXT / SRT / 中文字幕 / 中文文本" : "TXT / SRT";
  $("#transcript-preview").textContent = task.transcript_preview || "没有可预览文本";
  updateTaskExportLink();
  $("#warning-text").textContent = task.warnings?.filter((item) => item !== "ASR cache hit").join(" · ") || "";
  $("#source-download").hidden = !task.source_media_available;
  $("#source-download").href = `/api/tasks/${task.task_id}/source`;
  $("#summary-panel").hidden = onlyVideo;
  $("#summary-preview").hidden = !task.summary_preview;
  $("#summary-preview").textContent = task.summary_preview || "";
  $("#summary-downloads").hidden = !task.summary_preview;
  $("#summary-download-md").href = `/api/tasks/${task.task_id}/files/summary.md`;
  $("#summary-download-txt").href = `/api/tasks/${task.task_id}/files/summary.txt`;
  $("#summary-result-hint").textContent = task.summary_error ? `总结未完成，字幕已保留：${task.summary_error}` : task.summary_preview ? "根据转写字幕生成，未分析画面；重要信息请核对原视频。" : "可直接使用已有字幕生成报告，无需重新下载或转写。字幕会发送给 Codex 大模型处理。";
  $("#generate-summary").textContent = task.summary_error ? "重试总结" : task.summary_preview ? "重新生成总结" : "生成总结";
  $("#generate-summary").disabled = !healthState?.summary_available;
  $("#generate-summary").hidden = Boolean(task.summary_preview && !task.summary_error);
  const summaryExport = document.querySelector('input[data-task-export-type][value="summary"]');
  summaryExport.closest("label").hidden = !task.summary_preview;
  if (!task.summary_preview) summaryExport.checked = false;
  updateTaskExportLink();
  $("#completed-back").hidden = false;
  $("#completed-back").textContent = currentBatch ? "← 返回任务队列" : "← 返回最近任务";
  loadHistory();
}

function renderFailed(task) {
  currentTaskId = task.task_id;
  showPanel("failed");
  $("#task-state").textContent = "失败";
  $("#failure-message").textContent = task.error || "发生未知错误，请检查素材后重试。";
  $("#failed-back").hidden = false;
  $("#failed-back").textContent = currentBatch ? "← 返回任务队列" : "← 返回最近任务";
}

async function viewTask(taskId, restoreBatch = false) {
  const previousTaskId = currentTaskId;
  // 先进入详情状态，防止已发出的批次轮询在详情请求返回前覆盖页面。
  currentTaskId = taskId;
  try {
    const response = await fetch(`/api/tasks/${taskId}`, { cache: "no-store" });
    if (!response.ok) throw new Error("无法读取任务状态");
    const task = await response.json();
    if (restoreBatch) {
      currentBatch = null;
      currentBatchId = null;
      if (task.batch_id) {
        try {
          const batchResponse = await fetch(`/api/batches/${task.batch_id}`, { cache: "no-store" });
          if (batchResponse.ok) {
            currentBatch = await batchResponse.json();
            currentBatchId = currentBatch.batch_id;
          }
        } catch { /* 批次记录不可用时仍可查看并返回最近任务 */ }
      }
    }
    $("#form-error").textContent = "";
    if (task.status === "completed") renderCompleted(task);
    else if (task.status === "failed" || task.status === "cancelled") renderFailed(task);
    else if (currentBatch) {
      currentTaskId = null;
      renderBatch(currentBatch);
      clearInterval(pollTimer);
      pollTimer = setInterval(pollBatch, 1000);
    }
  } catch (error) {
    currentTaskId = previousTaskId;
    throw error;
  }
}

async function pollBatch() {
  const batchId = currentBatchId;
  if (!batchId) return;
  try {
    const response = await fetch(`/api/batches/${batchId}`, { cache: "no-store" });
    if (!response.ok) throw new Error("无法读取批次状态");
    const batch = await response.json();
    // 新批次已开始或正在查看详情时，丢弃旧请求的页面更新。
    if (batchId !== currentBatchId) return;
    currentBatch = batch;
    $("#form-error").textContent = "";
    if (!currentTaskId) renderBatch(batch);
    if (batch.tasks.every((task) => terminalStatuses.has(task.status))) {
      clearInterval(pollTimer);
      pollTimer = null;
      loadHistory();
    }
  } catch (error) {
    if (batchId === currentBatchId) $("#form-error").textContent = error.message;
  }
}

function resetBatchProgress(expectedTotal) {
  clearInterval(pollTimer);
  pollTimer = null;
  currentBatchId = null;
  currentBatch = null;
  currentTaskId = null;

  showPanel("batch");
  $("#task-state").textContent = "上传中";
  $("#batch-total").textContent = expectedTotal;
  $("#batch-completed").textContent = "0";
  $("#batch-failed").textContent = "0";
  $("#batch-progress-bar").style.width = "0%";
  $("#batch-estimate").textContent = "预计完成时间：上传完成后计算";
  $("#queue-list").innerHTML = `<p class="history-empty">正在上传 ${expectedTotal} 个文件并创建任务…</p>`;
  $("#upload-progress").hidden = true;
  $("#upload-progress-bar").style.width = "0%";

  const exportLink = $("#batch-export");
  exportLink.removeAttribute("href");
  exportLink.setAttribute("aria-disabled", "true");
  exportLink.textContent = "批量下载";
}

function uploadBatch(formData) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", "/api/batches");
    request.upload.addEventListener("progress", (event) => {
      if (!event.lengthComputable) return;
      $("#upload-progress").hidden = false;
      $("#upload-progress-bar").style.width = `${Math.round(event.loaded / event.total * 100)}%`;
    });
    request.addEventListener("load", () => {
      let payload = {};
      try { payload = JSON.parse(request.responseText); } catch { payload = {}; }
      if (request.status >= 200 && request.status < 300) resolve(payload);
      else reject(new Error(payload.detail || "批量上传失败"));
    });
    request.addEventListener("error", () => reject(new Error("无法连接本地服务")));
    request.send(formData);
  });
}

async function submitForm(event) {
  event.preventDefault();
  $("#form-error").textContent = "";
  if (selectedSource() !== "file") {
    $("#submit-button").disabled = true;
    try {
      if (!healthState?.douyin_cookie_configured) {
        $("#form-error").textContent = "需要先完成抖音扫码登录，登录成功后再提交任务。";
        await openDouyinLoginModal();
        return;
      }
      // Do not upload local files from the hidden input when submitting a link.
      const source = selectedSource();
      const data = new FormData();
      data.set("url", $("#douyin-url").value);
      data.set("source_kind", source);
      data.set("transcribe_after_download", String(!downloadOnly()));
      data.set("summarize", String(wantsSummary()));
      data.set("recording_minutes", source === "live" ? $("#recording-minutes").value : "30");
      data.set("download_quality", $("#download-quality-select").value);
      data.set("profile_limit", $("#profile-limit-input").value);
      data.set("start_date", $("#profile-start-date").value);
      data.set("end_date", $("#profile-end-date").value);
      data.set("skip_downloaded", String($("#skip-downloaded-input").checked));
      data.set("redownload_missing", String($("#redownload-missing-input").checked));
      data.set("mode", $("#output-mode-select").value);
      data.set("asr_mode", selectedAsrMode());
      data.set("hard_subtitles", String($("#hard-subtitles-input").checked));
      data.set("translate_to_chinese", String($("#translate-to-chinese-input").checked));
      data.set("api_hotwords", $("#api-hotwords-input").value);
      const batch = await requestJson("/api/douyin", { method: "POST", body: data });
      clearInterval(pollTimer);
      currentTaskId = null;
      currentBatchId = batch.batch_id;
      renderBatch(batch);
      pollTimer = setInterval(pollBatch, 1000);
      await loadHistory();
      if (batch.skipped?.length) {
        const examples = batch.skipped.slice(0, 3).map((item) => item.title || item.video_id).join("、");
        $("#form-error").textContent = `已跳过 ${batch.skipped.length} 个历史视频${examples ? `：${examples}` : ""}`;
      }
      if (!batch.tasks?.length) $("#task-state").textContent = "没有需要下载的新视频";
    } catch (error) {
      $("#form-error").textContent = error.message;
      if (/登录验证|扫码登录/.test(error.message || "")) openDouyinLoginModal();
    } finally {
      updateAsrControls();
    }
    return;
  }
  const mediaFiles = [...$("#media-input").files];
  if (!mediaFiles.length) { $("#form-error").textContent = "请先选择视频或音频。"; return; }
  if (mediaFiles.length > 50) { $("#form-error").textContent = "单批最多选择 50 个媒体文件。"; return; }
  resetBatchProgress(mediaFiles.length);
  $("#submit-button").disabled = true;
  try {
    const data = new FormData(event.currentTarget);
    data.set("summarize", String(wantsSummary()));
    const batch = await uploadBatch(data);
    currentBatchId = batch.batch_id;
    currentBatch = batch;
    $("#upload-progress-bar").style.width = "100%";
    renderBatch(batch);
    clearInterval(pollTimer);
    pollTimer = setInterval(pollBatch, 1000);
    await pollBatch();
  } catch (error) {
    $("#form-error").textContent = error.message;
    $("#submit-button").disabled = false;
    showPanel("empty");
    $("#task-state").textContent = "等待素材";
  }
}

async function loadHealth() {
  try {
    const response = await fetch("/api/health", { cache: "no-store" });
    const health = await response.json();
    healthState = health;
    $("#app-version").textContent = health.version;
    const pill = $("#engine-pill");
    if (health.model_configured) {
      pill.classList.add("ready");
      $("#engine-label").textContent = `${health.model_name} · ${health.device.toUpperCase()} ${health.compute_type.toUpperCase()}`;
    } else {
      pill.classList.add("error");
      $("#engine-label").textContent = health.api_pipeline_available ? "千问 API · Codex CLI 可用" : "本地模型未配置";
    }
    if (!health.ocr_available) {
      $("#hard-subtitles-input").disabled = true;
      $("#ocr-hint").textContent = "本地 OCR 运行时未安装";
    }
    if (!health.api_pipeline_available) {
      $("#api-asr-hint").textContent = `API 模式未就绪：请在启动服务前设置 ${health.api_key_environment}，并确认 requests 与 Codex CLI 可用。`;
    }
    updateAsrControls();
  } catch {
    $("#engine-pill").classList.add("error");
    $("#engine-label").textContent = "本地服务未连接";
  }
}

const tableMarkup = new Map();
function updateTable(selector, html) {
  if (tableMarkup.get(selector) !== html) {
    $(selector).innerHTML = html;
    tableMarkup.set(selector, html);
  }
}
let dashboardLoading = false;
async function loadHistory(page = historyPage) {
  historyPage = Math.max(1, page);
  if (dashboardLoading) return;
  dashboardLoading = true;
  try {
    const first = await requestJson("/api/tasks?page=1&page_size=100");
    const tasks = [...first.tasks];
    for (let p = 2; p <= first.total_pages; p++) {
      const next = await requestJson(`/api/tasks?page=${p}&page_size=100`);
      tasks.push(...next.tasks);
    }
    const unique = [...new Map(tasks.map(task => [task.task_id, task])).values()];
    const active = unique.filter(task => !terminalStatuses.has(task.status));
    const finished = unique.filter(task => terminalStatuses.has(task.status));
    promptForCookieLogin(unique);
    $("#batch-result").hidden = false;
    $("#empty-result").hidden = true;
    updateTable("#queue-list", active.length ? active.map((task, index) => renderTaskRow(task, index)).join("") : '<p class="history-empty">暂无执行中的任务，已结束的任务请在内容库查看。</p>');
    $("#task-state").textContent = `${active.length} 个任务执行中`;
    $("#batch-total").textContent = active.length;
    $("#batch-estimate").textContent = "显示所有批次正在排队、下载、录制、转写或总结的任务；任务结束后自动移入内容库。";
    const totalPages = Math.max(1, Math.ceil(finished.length / historyPageSize));
    historyPage = Math.min(historyPage, totalPages);
    const start = (historyPage - 1) * historyPageSize;
    updateTable("#history-list", finished.slice(start, start + historyPageSize).map((task, index) => renderTaskRow(task, start + index, true)).join("") || '<p class="history-empty">暂无已结束的内容</p>');
    $("#history-pagination").hidden = totalPages <= 1;
    $("#history-page-info").textContent = `第 ${historyPage} / ${totalPages} 页 · 共 ${finished.length} 个已结束任务`;
    $("#history-previous").disabled = historyPage <= 1;
    $("#history-next").disabled = historyPage >= totalPages;
  } catch (error) {
    $("#task-state").textContent = "任务状态刷新失败，稍后重试";
  } finally { dashboardLoading = false; }
}

async function requestJson(url, options = {}) {
  const response = await fetch(url, options);
  let payload = {};
  try { payload = await response.json(); } catch { payload = {}; }
  if (!response.ok) throw new Error(payload.detail || "操作失败");
  return payload;
}

async function retryTask(taskId) {
  const batch = await requestJson(`/api/tasks/${taskId}/retry`, { method: "POST" });
  currentTaskId = null;
  currentBatchId = batch.batch_id;
  currentBatch = batch;
  renderBatch(batch);
  clearInterval(pollTimer);
  pollTimer = setInterval(pollBatch, 1000);
  await pollBatch();
  await loadHistory();
  await loadStorage();
}

async function deleteTask(taskId) {
  if (!window.confirm("确定删除这个任务及其输出、工作文件和上传素材吗？")) return;
  await requestJson(`/api/tasks/${taskId}`, { method: "DELETE" });
  if (currentTaskId === taskId) {
    currentTaskId = null;
    currentBatch = null;
    currentBatchId = null;
    showPanel("empty");
    $("#task-state").textContent = "等待素材";
  }
  await loadHistory();
  await loadStorage();
}

async function loadStorage() {
  try {
    const storage = await requestJson("/api/storage", { cache: "no-store" });
    $("#storage-work").textContent = formatBytes(storage.work_bytes);
    $("#storage-output").textContent = formatBytes(storage.output_bytes);
    $("#storage-uploads").textContent = formatBytes(storage.uploads_bytes);
    $("#storage-cache").textContent = formatBytes(storage.cache_bytes);
    $("#storage-summary").textContent = `${storage.task_count} 个历史任务 · ${storage.pending_count} 个待恢复任务`;
  } catch (error) {
    $("#storage-summary").textContent = error.message;
  }
}

async function cleanupStorage(scope) {
  const label = scope === "cache" ? "识别缓存" : "已完成任务的临时音频和抽帧";
  if (!window.confirm(`确定清理${label}吗？这些文件均可重新生成。`)) return;
  const form = new FormData();
  form.append("scope", scope);
  const result = await requestJson("/api/storage/cleanup", { method: "POST", body: form });
  $("#storage-summary").textContent = `本次释放 ${formatBytes(result.freed_bytes)}`;
  await loadStorage();
}

async function clearAllTasks() {
  const confirmed = window.confirm("确定清空所有任务吗？所有输出文件、任务记录、上传素材和工作文件都会删除，且无法恢复；识别缓存会保留。");
  if (!confirmed) return;
  const result = await requestJson("/api/tasks", { method: "DELETE" });
  currentTaskId = null;
  currentBatch = null;
  currentBatchId = null;
  clearInterval(pollTimer);
  pollTimer = null;
  showPanel("empty");
  $("#task-state").textContent = "等待素材";
  $("#form-error").textContent = `已清空 ${result.cleared_tasks} 个任务，释放 ${formatBytes(result.freed_bytes)}`;
  await loadHistory();
  await loadStorage();
}

function returnFromTask() {
  currentTaskId = null;
  if (currentBatch) {
    renderBatch(currentBatch);
    return;
  }
  showPanel("empty");
  $("#task-state").textContent = "等待素材";
  document.querySelector(".history-section").scrollIntoView({ behavior: "smooth", block: "start" });
}

function closeHistoryDownloadMenus(except = null) {
  document.querySelectorAll("details.history-download[open]").forEach((menu) => {
    if (menu !== except) menu.open = false;
  });
}

function updateTaskExportLink() {
  const exportLink = $("#task-export");
  const exportTypes = selectedExportTypes("data-task-export-type");
  if (!currentTaskId || !exportTypes.length) {
    exportLink.removeAttribute("href");
    exportLink.setAttribute("aria-disabled", "true");
    return;
  }
  exportLink.setAttribute("aria-disabled", "false");
  exportLink.href = `/api/tasks/${currentTaskId}/export?${exportQuery(exportTypes)}`;
  exportLink.textContent = exportTypes.length === 1
    ? `下载${exportTypeLabel(exportTypes)}`
    : `下载 ${exportTypeLabel(exportTypes)}（ZIP）`;
}

$("#task-form").addEventListener("submit", submitForm);
$("#generate-summary").addEventListener("click", async () => {
  const taskId = currentTaskId;
  if (!taskId) return;
  $("#generate-summary").disabled = true;
  try {
    await requestJson(`/api/tasks/${taskId}/summarize`, { method: "POST" });
    $("#task-state").textContent = "正在生成总结";
    $("#summary-result-hint").textContent = "正在根据已有字幕生成报告，请稍候…";
    clearInterval(pollTimer);
    const timer = setInterval(async () => {
      if (currentTaskId !== taskId) { clearInterval(timer); return; }
      try {
        const task = await requestJson(`/api/tasks/${taskId}`);
        if (currentTaskId !== taskId) return;
        $("#task-state").textContent = taskStatusLabel(task);
        if (terminalStatuses.has(task.status)) {
          clearInterval(timer);
          if (task.status === "completed") renderCompleted(task); else renderFailed(task);
        }
      } catch (error) { $("#summary-result-hint").textContent = error.message; }
    }, 1000);
    pollTimer = timer;
  } catch (error) {
    $("#summary-result-hint").textContent = error.message;
    $("#generate-summary").disabled = false;
  }
});
document.querySelectorAll('input[name="input_source"]').forEach((input) => input.addEventListener("change", updateSourceControls));
restoreApiHotwords();
$("#api-hotwords-input").addEventListener("input", saveApiHotwords);
$("#media-input").addEventListener("change", updateFileLabel);
$("#subtitle-input").addEventListener("change", updateSubtitleLabel);
$("#refresh-history").addEventListener("click", () => loadHistory(1));
$("#history-previous").addEventListener("click", () => loadHistory(historyPage - 1));
$("#history-next").addEventListener("click", () => loadHistory(historyPage + 1));
$("#completed-back").addEventListener("click", returnFromTask);
$("#failed-back").addEventListener("click", returnFromTask);
document.querySelectorAll("input[data-batch-export-type]").forEach((input) => input.addEventListener("change", () => { if (currentBatch) renderBatch(currentBatch); }));
document.querySelectorAll("input[data-task-export-type]").forEach((input) => input.addEventListener("change", updateTaskExportLink));
$("#asr-mode-select").addEventListener("change", updateAsrControls);
$("#reset-button").addEventListener("click", () => { currentTaskId = null; currentBatch = null; currentBatchId = null; showPanel("empty"); $("#task-state").textContent = "等待素材"; });
$("#delete-completed-task").addEventListener("click", async () => { if (currentTaskId) await deleteTask(currentTaskId); });
$("#retry-failed-task").addEventListener("click", async () => { if (currentTaskId) await retryTask(currentTaskId); });
$("#delete-failed-task").addEventListener("click", async () => { if (currentTaskId) await deleteTask(currentTaskId); });
$("#refresh-storage").addEventListener("click", loadStorage);
$("#clear-all-tasks").addEventListener("click", async () => {
  try { await clearAllTasks(); } catch (error) { $("#storage-summary").textContent = error.message; }
});
document.querySelectorAll("[data-cleanup-scope]").forEach((button) => button.addEventListener("click", async () => cleanupStorage(button.dataset.cleanupScope)));
$("#copy-button").addEventListener("click", async () => {
  await navigator.clipboard.writeText($("#transcript-preview").textContent);
  $("#copy-button").textContent = "已复制";
  setTimeout(() => { $("#copy-button").textContent = "复制"; }, 1200);
});
$("#queue-list").addEventListener("click", async (event) => {
  const stop = event.target.closest("button[data-stop-task-id]");
  if (stop) {
    stop.disabled = true;
    try {
      const result = await requestJson(`/api/tasks/${stop.dataset.stopTaskId}/stop`, { method: "POST" });
      $("#batch-estimate").textContent = result.message;
    } catch (error) { $("#form-error").textContent = error.message; }
    return;
  }
  const button = event.target.closest("button[data-batch-task-id]");
  if (!button || button.disabled) return;
  try { await viewTask(button.dataset.batchTaskId); } catch (error) { $("#form-error").textContent = error.message; }
});
$("#history-list").addEventListener("click", async (event) => {
  const batchButton = event.target.closest("button[data-view-batch]");
  if (batchButton) {
    try {
      const batch = await requestJson(`/api/batches/${batchButton.dataset.viewBatch}`);
      clearInterval(pollTimer);
      currentTaskId = null;
      currentBatchId = batch.batch_id;
      queueFilter = "all";
      renderBatch(batch);
      if (batch.tasks.some((task) => !terminalStatuses.has(task.status))) pollTimer = setInterval(pollBatch, 1000);
      document.querySelector(".result-card").scrollIntoView({behavior:"smooth", block:"start"});
    } catch (error) { $("#form-error").textContent = error.message; }
    return;
  }

  const stop = event.target.closest("button[data-stop-task-id]");
  if (stop) {
    stop.disabled = true;
    try {
      const result = await requestJson(`/api/tasks/${stop.dataset.stopTaskId}/stop`, { method: "POST" });
      $("#batch-estimate").textContent = result.message;
      stop.textContent = "正在结束录制…";
    } catch (error) { $("#form-error").textContent = error.message; stop.disabled = false; }
    return;
  }
  const retryButton = event.target.closest("button[data-retry-task-id]");
  if (retryButton) {
    try { await retryTask(retryButton.dataset.retryTaskId); } catch (error) { $("#form-error").textContent = error.message; }
    return;
  }
  const deleteButton = event.target.closest("button[data-delete-task-id]");
  if (deleteButton) {
    try { await deleteTask(deleteButton.dataset.deleteTaskId); } catch (error) { $("#form-error").textContent = error.message; }
    return;
  }
  const button = event.target.closest("button[data-task-id]");
  if (!button) return;
  try { await viewTask(button.dataset.taskId, true); } catch (error) { $("#form-error").textContent = error.message; }
  document.querySelector(".result-card").scrollIntoView({ behavior: "smooth", block: "start" });
});

document.addEventListener("click", (event) => {
  const activeMenu = event.target.closest("details.history-download");
  closeHistoryDownloadMenus(activeMenu);
  if (event.target.closest(".history-download-menu a") && activeMenu) activeMenu.open = false;
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeHistoryDownloadMenus();
});

const dropzone = $("#media-dropzone");
["dragenter", "dragover"].forEach((name) => dropzone.addEventListener(name, (event) => { event.preventDefault(); dropzone.classList.add("dragging"); }));
["dragleave", "drop"].forEach((name) => dropzone.addEventListener(name, (event) => { event.preventDefault(); dropzone.classList.remove("dragging"); }));
dropzone.addEventListener("drop", (event) => {
  if (!event.dataTransfer.files.length) return;
  const transfer = new DataTransfer();
  [...event.dataTransfer.files].slice(0, 50).forEach((file) => transfer.items.add(file));
  $("#media-input").files = transfer.files;
  updateFileLabel();
});

updateSourceControls();
loadHealth();
loadHistory();
loadStorage();

document.querySelectorAll('input[name="douyin_action"]').forEach((input) => input.addEventListener("change", updateSourceControls));
$("#douyin-login-button").addEventListener("click", openDouyinLoginModal);
$("#douyin-login-retry").addEventListener("click", openDouyinLoginModal);
$("#douyin-login-close").addEventListener("click", cancelDouyinLogin);
$("#douyin-login-cancel").addEventListener("click", cancelDouyinLogin);

// Keep the compact settings informative without expanding the main form.
$("#output-mode-select").addEventListener("change", () => {
  $("#output-mode-hint").textContent = {
    verbatim: "保留原话，字幕用于纠错。",
    subtitle: "字幕优先，语音识别补充遗漏。",
    clean: "对比后去填充、去重复、补标点并分段。",
  }[$("#output-mode-select").value];
});
document.querySelectorAll(".main-nav a").forEach((link) => {
  link.addEventListener("click", () => {
    document.querySelectorAll(".main-nav a").forEach((item) => {
      item.classList.toggle("active", item === link);
      if (item === link) item.setAttribute("aria-current", "location");
      else item.removeAttribute("aria-current");
    });
  });
});

document.querySelectorAll("[data-queue-filter]").forEach((button) => {
  button.addEventListener("click", () => {
    queueFilter = button.dataset.queueFilter;
    if (currentBatch) renderBatch(currentBatch);
  });
});

document.addEventListener("error", (event) => {
  if (event.target.matches("img[data-thumb-key]")) {
    if (unavailableThumbnails.size > 500) unavailableThumbnails.clear();
    unavailableThumbnails.add(event.target.dataset.thumbKey);
    event.target.remove();
  }
}, true);

setInterval(() => loadHistory(), 2500);
