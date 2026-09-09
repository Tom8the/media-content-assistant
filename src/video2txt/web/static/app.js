const $ = (selector) => document.querySelector(selector);
const terminalStatuses = new Set(["completed", "failed", "cancelled"]);
const statusLabels = {
  queued: "等待处理",
  probing: "分析媒体",
  extracting: "提取音轨",
  transcribing: "语音转写",
  subtitle_processing: "处理字幕",
  aligning: "时间轴对齐",
  exporting: "生成结果",
  translating: "翻译中文字幕",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};
const modeLabels = { verbatim: "逐字稿", subtitle: "字幕稿", clean: "整理稿" };
const estimateRealtimeFactor = 0.22;
const estimateHardSubtitleFactor = 0.9;
const estimateSecondsPerMB = 1.85;
const apiHotwordsStorageKey = "video2txt.api-hotwords.v1";

let currentBatchId = null;
let currentBatch = null;
let currentTaskId = null;
let pollTimer = null;
let historyPage = 1;
const historyPageSize = 12;
let healthState = null;

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
  return document.querySelector('input[name="asr_mode"]:checked')?.value || "local";
}

function updateAsrControls() {
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
    return;
  }
  translation.disabled = !health.translation_models_available;
  translationHint.textContent = health.translation_models_available
    ? "开始提取前勾选才会生成中文文件；本地模式使用离线 NLLB 翻译。"
    : "离线翻译模型尚未安装";
  $("#submit-button").disabled = !health.model_configured;
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
  return ocrProgress(task)?.short || statusLabels[task.status] || task.status;
}

function selectedExportTypes(attribute) {
  return [...document.querySelectorAll(`input[${attribute}]:checked`)].map((input) => input.value);
}

function exportQuery(types) {
  return types.map((type) => `types=${encodeURIComponent(type)}`).join("&");
}

function exportTypeLabel(types) {
  const labels = { text: "文本", subtitle: "原字幕", translation: "中文字幕", translation_text: "中文文本" };
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
  ["empty", "batch", "completed", "failed"].forEach((panel) => {
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

function renderBatch(batch) {
  currentBatch = batch;
  showPanel("batch");
  const tasks = batch.tasks || [];
  const completed = tasks.filter((task) => task.status === "completed").length;
  const failed = tasks.filter((task) => task.status === "failed").length;
  const terminal = tasks.filter((task) => terminalStatuses.has(task.status)).length;
  const allFinished = tasks.length > 0 && terminal === tasks.length;
  const activeTask = tasks.find((task) => !terminalStatuses.has(task.status));
  $("#batch-total").textContent = tasks.length;
  $("#batch-completed").textContent = completed;
  $("#batch-failed").textContent = failed;
  $("#batch-progress-bar").style.width = `${tasks.length ? Math.round(terminal / tasks.length * 100) : 0}%`;
  renderBatchEstimate(tasks, allFinished);
  $("#task-state").textContent = allFinished
    ? `${completed} 个已完成`
    : `${terminal}/${tasks.length} 已处理${activeTask ? ` · ${taskStatusLabel(activeTask)}` : ""}`;
  $("#queue-list").innerHTML = tasks.map((task) => {
    const canView = terminalStatuses.has(task.status);
    const progress = ocrProgress(task);
    const normalDetail = [
      formatBytes(task.media_size),
      modeLabels[task.mode] || task.mode,
      task.hard_subtitles ? "硬字幕 OCR" : "",
      task.asr_mode === "api" ? "千问 API + Codex 翻译" : "本地模型",
      task.translate_to_chinese ? "中文字幕" : "",
    ].filter(Boolean).join(" · ");
    const detail = task.status === "failed"
      ? escapeHtml(task.error || "处理失败")
      : escapeHtml(progress?.detail || normalDetail);
    const progressBar = progress
      ? `<div class="queue-progress" aria-label="OCR 进度 ${progress.percent}%"><span style="width:${progress.percent}%"></span></div>`
      : "";
    return `<article class="queue-item">
      <div><strong title="${escapeHtml(task.original_filename)}">${escapeHtml(task.original_filename || task.task_id)}</strong><small>${detail}</small>${progressBar}</div>
      <span class="queue-status ${escapeHtml(task.status)}">${escapeHtml(taskStatusLabel(task))}</span>
      <button type="button" data-batch-task-id="${escapeHtml(task.task_id)}" ${canView ? "" : "disabled"}>查看${canView ? "结果" : ""} →</button>
    </article>`;
  }).join("");
  const exportLink = $("#batch-export");
  const exportTypes = selectedExportTypes("data-batch-export-type");
  const canExport = allFinished && completed > 0 && exportTypes.length > 0;
  exportLink.setAttribute("aria-disabled", String(!canExport));
  if (canExport) {
    exportLink.href = `/api/batches/${batch.batch_id}/export.zip?${exportQuery(exportTypes)}`;
    exportLink.textContent = `下载 ${completed} 个${exportTypeLabel(exportTypes)}（ZIP）`;
  } else {
    exportLink.removeAttribute("href");
    exportLink.textContent = "批量下载";
  }
  $("#completed-back").hidden = false;
  $("#failed-back").hidden = false;
  if (allFinished) $("#submit-button").disabled = false;
}

function renderCompleted(task) {
  currentTaskId = task.task_id;
  showPanel("completed");
  $("#task-state").textContent = "已完成";
  $("#completed-mode").textContent = modeLabels[task.mode] || task.mode;
  $("#completed-file-count").textContent = task.translate_to_chinese ? "TXT / SRT / 中文字幕 / 中文文本" : "TXT / SRT";
  $("#transcript-preview").textContent = task.transcript_preview || "没有可预览文本";
  updateTaskExportLink();
  $("#warning-text").textContent = task.warnings?.filter((item) => item !== "ASR cache hit").join(" · ") || "";
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
    else if (task.status === "failed") renderFailed(task);
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
  const mediaFiles = [...$("#media-input").files];
  if (!mediaFiles.length) { $("#form-error").textContent = "请先选择视频或音频。"; return; }
  if (mediaFiles.length > 30) { $("#form-error").textContent = "单批最多选择 30 个媒体文件。"; return; }
  resetBatchProgress(mediaFiles.length);
  $("#submit-button").disabled = true;
  try {
    const batch = await uploadBatch(new FormData(event.currentTarget));
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

async function loadHistory(page = historyPage) {
  try {
    historyPage = Math.max(1, page);
    const response = await fetch(`/api/tasks?page=${historyPage}&page_size=${historyPageSize}`, { cache: "no-store" });
    const { tasks, total = 0, total_pages: totalPages = 1 } = await response.json();
    if (!tasks.length && total > 0 && historyPage > 1) {
      await loadHistory(historyPage - 1);
      return;
    }
    if (!tasks.length) {
      $("#history-list").innerHTML = '<p class="history-empty">暂无历史任务</p>';
      $("#history-pagination").hidden = true;
      return;
    }
    $("#history-list").innerHTML = tasks.map((task) => `
      <article class="history-item">
        <div><strong>${escapeHtml(task.original_filename || task.task_id.slice(0, 10))}</strong><small>${escapeHtml(task.updated_at || "")} · ${escapeHtml(modeLabels[task.mode] || task.mode)}</small></div>
        <span class="history-status">${task.status === "completed" ? "已完成" : escapeHtml(statusLabels[task.status] || task.status)}</span>
        <div class="history-actions">
          <button type="button" data-task-id="${escapeHtml(task.task_id)}">查看结果 →</button>
          ${task.status === "completed" ? `<details class="history-download">
            <summary>下载</summary>
            <div class="history-download-menu">
              <a href="/api/tasks/${escapeHtml(task.task_id)}/export?types=text">文本文件</a>
              <a href="/api/tasks/${escapeHtml(task.task_id)}/export?types=subtitle">字幕文件</a>
              ${task.download_files?.includes("translated_subtitles_zh.srt") ? `<a href="/api/tasks/${escapeHtml(task.task_id)}/export?types=translation">中文字幕文件</a>` : ""}
              ${task.download_files?.includes("translated_transcript_zh.txt") ? `<a href="/api/tasks/${escapeHtml(task.task_id)}/export?types=translation_text">中文文本文件</a>` : ""}
              <a href="/api/tasks/${escapeHtml(task.task_id)}/export?types=text&types=subtitle${task.download_files?.includes("translated_subtitles_zh.srt") ? "&types=translation" : ""}${task.download_files?.includes("translated_transcript_zh.txt") ? "&types=translation_text" : ""}">全部</a>
            </div>
          </details>` : ""}
          ${task.status === "failed" ? `<button type="button" data-retry-task-id="${escapeHtml(task.task_id)}">重试</button>` : ""}
          ${terminalStatuses.has(task.status) ? `<button type="button" class="danger-button" data-delete-task-id="${escapeHtml(task.task_id)}">删除</button>` : ""}
        </div>
      </article>`).join("");
    $("#history-pagination").hidden = totalPages <= 1;
    $("#history-page-info").textContent = `第 ${historyPage} / ${totalPages} 页 · 共 ${total} 个任务`;
    $("#history-previous").disabled = historyPage <= 1;
    $("#history-next").disabled = historyPage >= totalPages;
  } catch { /* 页面主体仍可使用 */ }
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
document.querySelectorAll('input[name="asr_mode"]').forEach((input) => input.addEventListener("change", updateAsrControls));
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
  const button = event.target.closest("button[data-batch-task-id]");
  if (!button || button.disabled) return;
  try { await viewTask(button.dataset.batchTaskId); } catch (error) { $("#form-error").textContent = error.message; }
});
$("#history-list").addEventListener("click", async (event) => {
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
  window.scrollTo({ top: document.querySelector(".workspace").offsetTop - 20, behavior: "smooth" });
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
  [...event.dataTransfer.files].slice(0, 30).forEach((file) => transfer.items.add(file));
  $("#media-input").files = transfer.files;
  updateFileLabel();
});

loadHealth();
loadHistory();
loadStorage();
