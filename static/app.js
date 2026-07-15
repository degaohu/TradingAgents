// ================================================================
//  TradingAgents Dashboard — App JS (Bilingual Edition)
//  Handles: Language, Form, Job submission, SSE progress, Rendering
// ================================================================

// ----------------------------------------------------------------
// 1. Language System
// ----------------------------------------------------------------
let currentLang = 'zh';

function setLang(lang) {
    currentLang = lang;
    document.getElementById('lang-btn-zh').classList.toggle('active', lang === 'zh');
    document.getElementById('lang-btn-en').classList.toggle('active', lang === 'en');
    document.querySelectorAll('[data-zh]').forEach(el => {
        if (el.children.length === 0) {
            el.textContent = lang === 'zh' ? el.dataset.zh : el.dataset.en;
        }
    });
    // Re-render dynamic pipeline stage labels for the new language.
    document.querySelectorAll('.pipeline-stage').forEach(row => {
        const lbl = row.querySelector('.stage-label');
        if (lbl) {
            const zh = row.dataset.labelZh, en = row.dataset.labelEn;
            if (zh || en) lbl.textContent = (lang === 'zh' ? zh : en) || lbl.textContent;
        }
        const badge = row.querySelector('.stage-status-badge');
        if (badge) {
            if (row.classList.contains('done')) {
                badge.textContent = lang === 'zh' ? "已完成" : "DONE";
            } else if (row.classList.contains('running')) {
                badge.textContent = lang === 'zh' ? "进行中" : "RUN";
            } else {
                badge.textContent = "";
            }
        }
    });

    if (typeof renderHistoryList === "function") {
        renderHistoryList();
    }
}

window.setLang = setLang;

const JOB_STORAGE_KEY = 'tradingagents.activeJobId';

document.addEventListener("DOMContentLoaded", () => {
    const configForm    = document.getElementById("config-form");
    const welcomeView   = document.getElementById("welcome-view");
    const loadingView   = document.getElementById("loading-view");
    const resultsView   = document.getElementById("results-view");
    const submitBtn     = document.getElementById("submit-btn");
    const stopBtn       = document.getElementById("stop-analysis-btn");
    const previewBtn    = document.getElementById("preview-results-btn");
    const retryBtn      = document.getElementById("loading-retry-btn");
    const backToProgressBtn = document.getElementById("back-to-progress-btn");
    const liveStatusBanner  = document.getElementById("live-status-banner");
    const liveStatusText    = document.getElementById("live-status-text");
    const pipelineList  = document.getElementById("pipeline-list");
    const errorBanner   = document.getElementById("loading-error-banner");
    const errorMessage  = document.getElementById("loading-error-message");
    const exportPdfBtn  = document.getElementById("export-pdf-btn");

    setLang('zh');

    // Default analysis date to today (local time, YYYY-MM-DD).
    (function initTodayDate() {
        const el = document.getElementById("trade_date");
        if (el && !el.value) {
            const now = new Date();
            const yyyy = now.getFullYear();
            const mm = String(now.getMonth() + 1).padStart(2, '0');
            const dd = String(now.getDate()).padStart(2, '0');
            el.value = `${yyyy}-${mm}-${dd}`;
        }
        if (el) el.max = new Date().toISOString().slice(0, 10);
    })();

    // Ticker autosuggest (US equities + A-shares). Local list first for
    // instant offline hits; falls back to Yahoo Finance search for the long
    // tail. Keyboard: ArrowUp/Down/Enter/Escape.
    initTickerAutosuggest();

    // ----------------------------------------------------------------
    // 2. Tab Switching
    // ----------------------------------------------------------------
    document.querySelectorAll(".tabs-nav").forEach(nav => {
        const deck = nav.parentElement; // The dock-panel container containing nav and panels
        nav.querySelectorAll(".tab-btn").forEach(btn => {
            btn.addEventListener("click", () => {
                if (btn.id === "export-pdf-btn") return;
                nav.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
                deck.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("active"));
                btn.classList.add("active");
                const pane = deck.querySelector(`#pane-${btn.dataset.tab}`);
                if (pane) pane.classList.add("active");
            });
        });
    });

    // ----------------------------------------------------------------
    // 3. Job state
    // ----------------------------------------------------------------
    let currentJobId = null;
    let eventSource = null;
    let stageCounts = { total: 0, done: 0 };
    let activeStageIds = new Set();
    let stageLogs = {};      // stage_id -> string[] (log lines seen while it was running)
    let stageReports = {};   // stage_id -> {elementId: text} (reports delivered for this stage)
    let stageLabels = {};    // stage_id -> label (for the detail panel header)

    function showView(view) {
        [welcomeView, loadingView].forEach(v => v.classList.remove("active"));
        resultsView.classList.remove("active");
        if (view === "results") {
            resultsView.classList.add("active");
        } else {
            view.classList.add("active");
        }
    }

    function setSubmitting(isSubmitting) {
        submitBtn.disabled = isSubmitting;
        submitBtn.querySelector("span").textContent =
            currentLang === 'zh'
                ? (isSubmitting ? "分析中..." : "开始分析")
                : (isSubmitting ? "Analyzing..." : "Run Analysis");
    }

    function resetLoadingView() {
        pipelineList.innerHTML = "";
        previewBtn.style.display = "none";
        errorBanner.style.display = "none";
        stopBtn.style.display = "";
        stopBtn.disabled = false;
        stageCounts = { total: 0, done: 0 };
        activeStageIds = new Set();
        stageLogs = {};
        stageReports = {};
        stageLabels = {};
        exportPdfBtn.disabled = true;
        const consoleEl = document.getElementById("loading-console");
        consoleEl.innerHTML = '<div class="console-line">[SYSTEM] Stream listener initialized. Dispatching agents...</div>';
        
        const resultsConsole = document.getElementById("results-console");
        if (resultsConsole) {
            resultsConsole.innerHTML = '<div class="console-line">[SYSTEM] Stream listener initialized. Dispatching agents...</div>';
        }
    }

    function showInlineError(message) {
        errorBanner.style.display = "flex";
        errorMessage.textContent = message;
        stopBtn.style.display = "none";
    }

    // ----------------------------------------------------------------
    // 4. Form Submit — starts a job
    // ----------------------------------------------------------------
    configForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        await startAnalysis();
    });

    async function startAnalysis() {
        const ticker          = document.getElementById("ticker").value.trim().toUpperCase();
        const tradeDate       = document.getElementById("trade_date").value;
        const llmProviderEl   = document.getElementById("llm_provider");
        const deepThinkLLMEl  = document.getElementById("deep_think_llm");
        const quickThinkLLMEl = document.getElementById("quick_think_llm");
        const maxDebateRoundsEl = document.getElementById("max_debate_rounds");
        const checkpointEnabledEl = document.getElementById("checkpoint_enabled");

        const llmProvider     = llmProviderEl ? llmProviderEl.value : "deepseek";
        const deepThinkLLM    = deepThinkLLMEl ? deepThinkLLMEl.value.trim() : "deepseek-reasoner";
        const quickThinkLLM   = quickThinkLLMEl ? quickThinkLLMEl.value.trim() : "deepseek-chat";
        const maxDebateRounds = maxDebateRoundsEl ? (parseInt(maxDebateRoundsEl.value) || 1) : 1;
        const checkpointEnabled = checkpointEnabledEl ? checkpointEnabledEl.checked : false;

        if (!ticker) {
            showView(welcomeView);
            alert(currentLang === 'zh' ? "请输入有效的股票代码" : "Please enter a valid ticker symbol.");
            return;
        }

        resetLoadingView();
        showView(loadingView);
        setSubmitting(true);

        try {
            const response = await fetch("/api/analyze", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    ticker, trade_date: tradeDate, llm_provider: llmProvider,
                    deep_think_llm: deepThinkLLM, quick_think_llm: quickThinkLLM,
                    max_debate_rounds: maxDebateRounds, checkpoint_enabled: checkpointEnabled,
                    // Reports are written natively in this language by each agent's
                    // own prompt (get_language_instruction()) — not machine-translated
                    // after the fact — so quality matches the model's native output.
                    output_language: currentLang === 'zh' ? 'Chinese' : 'English'
                })
            });

            if (response.status === 409) {
                const body = await response.json();
                const running = body.detail || {};
                appendConsoleLine(
                    (currentLang === 'zh' ? "已有分析任务在运行，正在接入： " : "An analysis is already running, attaching: ")
                    + `${running.ticker} @ ${running.trade_date}`
                );
                attachToJob(running.job_id);
                return;
            }
            if (!response.ok) throw new Error("HTTP error " + response.status);

            const { job_id } = await response.json();
            attachToJob(job_id, /*isNew=*/true);
        } catch (err) {
            console.error(err);
            setSubmitting(false);
            showInlineError((currentLang === 'zh' ? "分析请求失败：" : "Analysis request failed: ") + err.message);
        }
    }

    // ----------------------------------------------------------------
    // 5. Job attachment — opens the SSE stream and wires event handling
    // ----------------------------------------------------------------
    function attachToJob(jobId, isNew) {
        currentJobId = jobId;
        localStorage.setItem(JOB_STORAGE_KEY, jobId);
        setSubmitting(true);
        if (!isNew) showView(loadingView);

        if (eventSource) eventSource.close();
        eventSource = new EventSource(`/api/jobs/${jobId}/events`);

        eventSource.onmessage = (evt) => {
            let payload;
            try {
                payload = JSON.parse(evt.data);
            } catch (err) {
                console.warn("Bad SSE payload", evt.data, err);
                return;
            }
            handleJobEvent(payload);
        };

        eventSource.onerror = () => {
            // EventSource retries transient drops on its own; a persistent
            // failure will keep firing this, so surface it once the job's
            // own terminal event never arrives instead of looping alerts.
        };
    }

    function handleJobEvent(payload) {
        switch (payload.type) {
            case "topology":
                buildPipeline(payload.stages);
                break;
            case "stage":
                updateStage(payload.stage_id, payload.status, payload.elapsed_s, payload.reports || {});
                break;
            case "log":
                appendConsoleLine(payload.message);
                tagLogForActiveStages(payload.message);
                break;
            case "result":
                closeStream();
                localStorage.removeItem(JOB_STORAGE_KEY);
                setSubmitting(false);
                displayResults(payload.data);
                break;
            case "error":
                closeStream();
                localStorage.removeItem(JOB_STORAGE_KEY);
                setSubmitting(false);
                showInlineError((currentLang === 'zh' ? "分析出错：" : "Analysis failed: ") + payload.message);
                break;
            case "cancelled":
                closeStream();
                localStorage.removeItem(JOB_STORAGE_KEY);
                setSubmitting(false);
                showInlineError(currentLang === 'zh' ? "分析已停止。" : "Analysis was stopped.");
                break;
            default:
                break; // ping / unknown — ignore
        }
    }

    function closeStream() {
        if (eventSource) {
            eventSource.close();
            eventSource = null;
        }
        liveStatusBanner.style.display = "none";
        stopLoadingHud();
    }

    stopBtn.addEventListener("click", async () => {
        if (!currentJobId) return;
        stopBtn.disabled = true;
        try {
            await fetch(`/api/jobs/${currentJobId}/cancel`, { method: "POST" });
        } catch (err) {
            console.error(err);
        }
    });

    retryBtn.addEventListener("click", () => {
        errorBanner.style.display = "none";
        showView(welcomeView);
    });

    previewBtn.addEventListener("click", () => {
        showView("results");
        liveStatusBanner.style.display = "flex";
    });

    backToProgressBtn.addEventListener("click", () => {
        showView(loadingView);
    });

    // ----------------------------------------------------------------
    // 5b. PDF export — AI-polishes the completed reports into one cohesive
    // document server-side, then hands it to the browser's native
    // print-to-PDF (no PDF-generation dependency needed).
    // ----------------------------------------------------------------
    exportPdfBtn.addEventListener("click", async () => {
        if (!currentJobId || exportPdfBtn.disabled) return;

        // Open synchronously, in direct response to the click, so popup
        // blockers don't treat the later async navigation as unsolicited.
        const printWin = window.open("", "_blank");
        if (!printWin) {
            alert(currentLang === 'zh'
                ? "浏览器阻止了弹出窗口，请允许弹窗后重试。"
                : "The browser blocked the popup — allow popups for this site and try again.");
            return;
        }
        printWin.document.write(_loadingPrintPage());

        exportPdfBtn.disabled = true;
        const label = exportPdfBtn.querySelector("span:last-child");
        const originalLabel = label.textContent;
        label.textContent = currentLang === 'zh' ? "AI 润色中…" : "AI polishing...";

        try {
            const resp = await fetch(`/api/jobs/${currentJobId}/polish`, { method: "POST" });
            if (!resp.ok) {
                const body = await resp.json().catch(() => ({}));
                throw new Error(body.detail || ("HTTP " + resp.status));
            }
            const { polished_markdown } = await resp.json();
            printWin.document.open();
            printWin.document.write(_buildPrintableHtml(polished_markdown));
            printWin.document.close();
            printWin.focus();
            // Let the popup finish laying out the freshly written document
            // before invoking the print dialog.
            setTimeout(() => printWin.print(), 350);
        } catch (err) {
            console.error(err);
            printWin.document.body.innerHTML =
                `<p style="color:#c0392b;font-family:sans-serif;padding:24px;">`
                + escapeHtml((currentLang === 'zh' ? "生成失败：" : "Failed to generate report: ") + err.message)
                + `</p>`;
        } finally {
            exportPdfBtn.disabled = false;
            label.textContent = originalLabel;
        }
    });

    function _loadingPrintPage() {
        const msg = currentLang === 'zh'
            ? "正在使用 AI 润色报告，请稍候（可能需要 10-60 秒）…"
            : "AI-polishing the report, please wait (this can take 10-60s)...";
        return `<!doctype html><html><head><meta charset="utf-8"><title>${
            currentLang === 'zh' ? "生成中…" : "Generating..."
        }</title></head><body style="font-family:-apple-system,sans-serif;color:#555;padding:60px;text-align:center;">
            <p>${escapeHtml(msg)}</p>
        </body></html>`;
    }

    function _buildPrintableHtml(markdown) {
        const ticker = document.getElementById("res-ticker").textContent;
        const date = document.getElementById("res-date").textContent;
        const title = `${ticker} · ${date} · Trading Analysis Report`;
        const generatedLine = currentLang === 'zh'
            ? `TradingAgents 生成 · ${new Date().toLocaleString()}`
            : `Generated by TradingAgents · ${new Date().toLocaleString()}`;
        return `<!doctype html>
<html lang="${currentLang === 'zh' ? 'zh-CN' : 'en'}">
<head>
<meta charset="utf-8">
<title>${escapeHtml(title)}</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif;
         color: #1a1a1a; max-width: 800px; margin: 40px auto; padding: 0 24px; line-height: 1.65; }
  h1, h2, h3, h4 { color: #111; margin-top: 28px; margin-bottom: 10px; }
  h1 { font-size: 24px; border-bottom: 2px solid #333; padding-bottom: 8px; }
  h2 { font-size: 19px; border-bottom: 1px solid #ddd; padding-bottom: 4px; }
  h3 { font-size: 16px; }
  p, li { font-size: 13.5px; }
  table { border-collapse: collapse; width: 100%; margin: 14px 0; font-size: 12.5px; }
  th, td { border: 1px solid #ccc; padding: 6px 10px; text-align: left; }
  th { background: #f2f2f2; }
  code { background: #f0f0f0; padding: 1px 5px; border-radius: 3px; font-size: 12px; }
  hr { border: none; border-top: 1px solid #ccc; margin: 20px 0; }
  .generated-line { color: #999; font-size: 11px; margin-bottom: 24px; }
  @media print {
    body { margin: 0; max-width: 100%; }
    @page { margin: 2cm; }
  }
</style>
</head>
<body>
  <div class="generated-line">${escapeHtml(generatedLine)}</div>
  ${renderMarkdown(markdown)}
</body>
</html>`;
    }

    // ----------------------------------------------------------------
    // 6. Pipeline progress rendering — each row expands on click to show
    // the reports and log lines captured while that stage was running.
    // ----------------------------------------------------------------
    function buildPipeline(stages) {
        const pipelineBody = document.getElementById("pipeline-body");
        if (pipelineBody) {
            pipelineBody.innerHTML = "";
        } else {
            pipelineList.innerHTML = "";
        }
        stageCounts = { total: stages.length, done: 0 };
        stages.forEach(stage => {
            // Keep both labels so we can re-render when the user flips language.
            stageLabels[stage.id] = { en: stage.label, zh: stage.label_zh || stage.label };
            stageLogs[stage.id] = [];
            stageReports[stage.id] = {};

            const row = document.createElement("div");
            row.className = "pipeline-stage pending";
            row.dataset.stageId = stage.id;
            row.dataset.labelEn = stage.label;
            row.dataset.labelZh = stage.label_zh || stage.label;
            row.innerHTML = `
                <span class="stage-chevron">›</span>
                <span class="stage-dot"></span>
                <span class="stage-label">${escapeHtml(currentLang === 'zh' ? row.dataset.labelZh : row.dataset.labelEn)}</span>
                <span class="stage-status-badge"></span>
                <span class="stage-elapsed"></span>
            `;
            row.addEventListener("click", () => toggleStageDetail(stage.id));
            if (pipelineBody) {
                pipelineBody.appendChild(row);
            } else {
                pipelineList.appendChild(row);
            }

            const detail = document.createElement("div");
            detail.className = "stage-detail";
            detail.dataset.detailFor = stage.id;
            detail.style.display = "none";
            if (pipelineBody) {
                pipelineBody.appendChild(detail);
            } else {
                pipelineList.appendChild(detail);
            }
        });
        // Kick off the progress-bar / ETA HUD.
        startLoadingHud(stages.length);
    }

    // ------------------------------------------------------------------
    // Loading HUD — progress bar, elapsed time, rolling ETA
    // ------------------------------------------------------------------
    let hudStartedAt = 0;
    let hudTimer = null;
    const HUD_ETA_SECONDS = 180; // Rough headline "1-3 minutes" expectation.

    function startLoadingHud(totalStages) {
        hudStartedAt = Date.now();
        if (hudTimer) clearInterval(hudTimer);
        renderLoadingHud(totalStages);
        hudTimer = setInterval(() => renderLoadingHud(totalStages), 500);
    }

    function stopLoadingHud() {
        if (hudTimer) { clearInterval(hudTimer); hudTimer = null; }
    }

    function renderLoadingHud(totalStages) {
        const hud = document.getElementById("loading-hud");
        if (!hud) return;
        const done = stageCounts.done;
        const total = totalStages || stageCounts.total || 1;
        const stageFrac = done / total;
        const elapsed = (Date.now() - hudStartedAt) / 1000;
        // Blend stage completion with a soft time-based curve: while no
        // stage has completed yet, we still want the bar to inch forward.
        const timeFrac = Math.min(0.95, elapsed / HUD_ETA_SECONDS);
        const pct = Math.max(2, Math.min(99, Math.round(100 * Math.max(stageFrac, timeFrac * 0.6))));
        hud.querySelector(".hud-bar-fill").style.width = pct + "%";
        hud.querySelector(".hud-pct").textContent = pct + "%";
        hud.querySelector(".hud-stages").textContent =
            (currentLang === 'zh' ? "阶段 " : "Stage ") + `${done}/${total}`;
        hud.querySelector(".hud-elapsed").textContent =
            (currentLang === 'zh' ? "已用 " : "Elapsed ") + fmtDuration(elapsed);

        // ETA: after 1 stage is done we extrapolate from real timing; before
        // that we fall back to the headline expectation.
        let etaSec;
        if (done > 0 && stageFrac > 0) {
            etaSec = Math.max(0, elapsed / stageFrac - elapsed);
        } else {
            etaSec = Math.max(0, HUD_ETA_SECONDS - elapsed);
        }
        hud.querySelector(".hud-eta").textContent =
            (currentLang === 'zh' ? "预计剩余 " : "ETA ") + fmtDuration(etaSec);
    }

    function fmtDuration(sec) {
        sec = Math.round(sec);
        const m = Math.floor(sec / 60);
        const s = sec % 60;
        return m > 0 ? `${m}m ${s}s` : `${s}s`;
    }

    function tagLogForActiveStages(message) {
        activeStageIds.forEach(stageId => {
            if (!stageLogs[stageId]) stageLogs[stageId] = [];
            stageLogs[stageId].push(message);
            const detail = pipelineList.querySelector(`[data-detail-for="${cssEscape(stageId)}"]`);
            if (detail && detail.style.display !== "none") renderStageDetail(stageId);
        });
    }

    function toggleStageDetail(stageId) {
        const row = pipelineList.querySelector(`[data-stage-id="${cssEscape(stageId)}"]`);
        const detail = pipelineList.querySelector(`[data-detail-for="${cssEscape(stageId)}"]`);
        if (!row || !detail) return;
        const isOpen = detail.style.display !== "none";
        if (isOpen) {
            detail.style.display = "none";
            row.classList.remove("expanded");
        } else {
            renderStageDetail(stageId);
            detail.style.display = "block";
            row.classList.add("expanded");
        }
    }

    function renderStageDetail(stageId) {
        const detail = pipelineList.querySelector(`[data-detail-for="${cssEscape(stageId)}"]`);
        if (!detail) return;
        const row = pipelineList.querySelector(`[data-stage-id="${cssEscape(stageId)}"]`);
        const status = row ? [...row.classList].find(c => ["pending", "running", "done"].includes(c)) : "pending";
        const reports = stageReports[stageId] || {};
        const logs = stageLogs[stageId] || [];

        const parts = [];
        if (status === "pending" && logs.length === 0) {
            parts.push(`<div class="stage-detail-empty">${currentLang === 'zh' ? '尚未开始' : 'Not started yet'}</div>`);
        }
        if (status === "running" && logs.length === 0) {
            parts.push(`<div class="stage-detail-empty">${currentLang === 'zh' ? '正在运行，等待第一条日志…' : 'Running, waiting for the first log line…'}</div>`);
        }
        const reportTexts = Object.values(reports).filter(Boolean);
        if (reportTexts.length > 0) {
            parts.push(`<div class="stage-detail-report markdown-content">${reportTexts.map(renderMarkdown).join("<hr>")}</div>`);
        }
        if (logs.length > 0) {
            const label = currentLang === 'zh' ? '运行日志' : 'Run log';
            parts.push(`
                <div class="stage-detail-log-header">${label} (${logs.length})</div>
                <div class="stage-detail-log">${logs.map(l => `<div>${escapeHtml(l)}</div>`).join("")}</div>
            `);
        }
        detail.innerHTML = parts.join("");
    }

    function updateStage(stageId, status, elapsedS, reports) {
        if (status === "running") activeStageIds.add(stageId);
        else activeStageIds.delete(stageId);

        if (!stageReports[stageId]) stageReports[stageId] = {};
        Object.assign(stageReports[stageId], reports);

        const row = pipelineList.querySelector(`[data-stage-id="${cssEscape(stageId)}"]`);
        if (row) {
            const wasDone = row.classList.contains("done");
            row.classList.remove("pending", "running", "done");
            row.classList.add(status);
            if (status === "done" && !wasDone) stageCounts.done += 1;
            if (status === "done" && elapsedS != null) {
                row.querySelector(".stage-elapsed").textContent = `${elapsedS}s`;
            }

            const badge = row.querySelector(".stage-status-badge");
            if (badge) {
                badge.classList.remove("running", "done");
                if (status === "running") {
                    badge.classList.add("running");
                    badge.textContent = currentLang === 'zh' ? "进行中" : "RUN";
                } else if (status === "done") {
                    badge.classList.add("done");
                    badge.textContent = currentLang === 'zh' ? "已完成" : "DONE";
                } else {
                    badge.textContent = "";
                }
            }
        }
        if (liveStatusText) {
            liveStatusText.textContent = (currentLang === 'zh' ? "分析仍在进行中… " : "Analysis still running… ")
                + `${stageCounts.done}/${stageCounts.total}`;
        }
        // First report available -> let the user peek at results without waiting.
        if (Object.keys(reports).length > 0) {
            previewBtn.style.display = "";
        }
        Object.entries(reports).forEach(([elementId, text]) => setMarkdown(elementId, text));

        // Live-refresh an already-open detail panel instead of waiting for the next click.
        const detail = pipelineList.querySelector(`[data-detail-for="${cssEscape(stageId)}"]`);
        if (detail && detail.style.display !== "none") renderStageDetail(stageId);
    }

    // ----------------------------------------------------------------
    // 7. Console Helper
    // ----------------------------------------------------------------
    function appendConsoleLine(text) {
        // Defensive: even if the backend somehow lets these through
        // (older SSE cache, upstream lib change), keep the console signal-only.
        if (!text) return;
        if (
            text.includes("HTTP Request:") ||
            text.includes("HTTP/1.1 200") ||
            text.includes("HTTP/1.1 201") ||
            /^\s*\[DEBUG\]/.test(text)
        ) return;
        const consoleEl = document.getElementById("loading-console");
        const line = document.createElement("div");
        line.className = "console-line";
        line.textContent = text;
        consoleEl.appendChild(line);
        const container = consoleEl.parentElement;
        container.scrollTop = container.scrollHeight;

        const resultsConsole = document.getElementById("results-console");
        if (resultsConsole) {
            const resultsLine = document.createElement("div");
            resultsLine.className = "console-line";
            resultsLine.textContent = text;
            resultsConsole.appendChild(resultsLine);
            resultsConsole.parentElement.scrollTop = resultsConsole.scrollHeight;
        }
    }

    // ----------------------------------------------------------------
    // 8. Main Results Renderer
    // ----------------------------------------------------------------
    function displayResults(data) {
        showView("results");
        liveStatusBanner.style.display = "none";
        exportPdfBtn.disabled = false;

        // Scroll the workstation view back to the top — the real
        // scroller is #results-view (see style.css .results-container).
        const scroller = document.querySelector("#results-view");
        if (scroller) {
            scroller.scrollTop = 0;
        }

        const summary = data.decision_summary || {};
        const action = (summary.action || data.decision || "HOLD").toUpperCase();

        // ── Decision Header ─────────────────────────────────────────
        const header = document.getElementById("decision-header");
        header.className = "decision-card glass-card";

        if (action.includes("BUY")) {
            header.classList.add("signal-buy");
            document.getElementById("final-action").textContent = currentLang === 'zh' ? "买入" : "BUY";
        } else if (action.includes("SELL")) {
            header.classList.add("signal-sell");
            document.getElementById("final-action").textContent = currentLang === 'zh' ? "卖出" : "SELL";
        } else {
            header.classList.add("signal-hold");
            document.getElementById("final-action").textContent = currentLang === 'zh' ? "持有" : "HOLD";
        }

        const confidenceEl = document.getElementById("decision-confidence");
        const RATING_MAP = {
            "Strong Buy": { "zh": "强烈买入", "en": "Strong Buy" },
            "Buy": { "zh": "买入", "en": "Buy" },
            "Overweight": { "zh": "增持", "en": "Overweight" },
            "Hold": { "zh": "持有", "en": "Hold" },
            "Underweight": { "zh": "减持", "en": "Underweight" },
            "Sell": { "zh": "卖出", "en": "Sell" },
            "Strong Sell": { "zh": "强烈卖出", "en": "Strong Sell" },
            "UNDERWEIGHT": { "zh": "减持", "en": "Underweight" },
            "OVERWEIGHT": { "zh": "增持", "en": "Overweight" }
        };

        const ratingText = summary.rating || data.decision || "";
        const mapped = RATING_MAP[ratingText] || RATING_MAP[ratingText.toUpperCase()];
        confidenceEl.textContent = mapped ? mapped[currentLang] : ratingText;

        const ticker = data.company_of_interest || data.ticker || "--";
        const date   = data.trade_date || "--";
        document.getElementById("res-ticker").textContent = ticker;
        document.getElementById("res-date").textContent   = date;
        document.getElementById("res-ticker-lg").textContent = ticker;
        document.getElementById("res-date-lg").textContent   = date;

        document.getElementById("res-entry").textContent    = formatPrice(summary.entry_price);
        document.getElementById("res-stop").textContent     = formatPrice(summary.stop_loss);
        document.getElementById("res-tp").textContent       = formatPrice(summary.price_target);
        document.getElementById("res-position").textContent = summary.position_sizing || "--";
        document.getElementById("res-horizon").textContent  =
            summary.time_horizon || (currentLang === 'zh' ? "未指定" : "Not specified");
        document.getElementById("res-rr").textContent =
            summary.risk_reward != null ? `${summary.risk_reward}×` : "--";
        document.getElementById("res-stop-pct").textContent =
            summary.downside_pct != null ? `${summary.downside_pct > 0 ? "+" : ""}${summary.downside_pct}%` : "";
        document.getElementById("res-tp-pct").textContent =
            summary.upside_pct != null ? `${summary.upside_pct > 0 ? "+" : ""}${summary.upside_pct}%` : "";

        // ── Sidebar decision snapshot ─────────────────────────────
        // Mirror the same numbers into the sticky TOC summary so they
        // stay visible as the reader scrolls the report.
        const tsSummary = document.getElementById("toc-summary");
        if (tsSummary) {
            tsSummary.setAttribute("aria-hidden", "false");

            const tsAction = document.getElementById("ts-action");
            const actionText =
                action.includes("BUY")  ? (currentLang === 'zh' ? "买入" : "BUY")  :
                action.includes("SELL") ? (currentLang === 'zh' ? "卖出" : "SELL") :
                                          (currentLang === 'zh' ? "持有" : "HOLD");
            tsAction.textContent = actionText;
            tsAction.className = "ts-action " + (
                action.includes("BUY") ? "buy" :
                action.includes("SELL") ? "sell" : "hold"
            );

            document.getElementById("ts-rating").textContent =
                (mapped ? mapped[currentLang] : ratingText) || "--";
            document.getElementById("ts-triplet").textContent =
                `${formatPrice(summary.entry_price)} / ${formatPrice(summary.stop_loss)} / ${formatPrice(summary.price_target)}`;
            document.getElementById("ts-rr").textContent =
                summary.risk_reward != null ? `${summary.risk_reward}×` : "--";
            document.getElementById("ts-position").textContent = summary.position_sizing || "--";

            // Conflict banner: server sets summary.consistency when the
            // rating-derived action disagrees with what the position
            // sizing text actually says (see web/decision.py).
            const conflictEl = document.getElementById("ts-conflict");
            const conflictText = document.getElementById("ts-conflict-text");
            const c = summary.consistency;
            if (c && c.conflict) {
                const ratingLabel =
                    c.rating_says === "BUY"  ? (currentLang === 'zh' ? "买入" : "BUY")  :
                    c.rating_says === "SELL" ? (currentLang === 'zh' ? "卖出" : "SELL") :
                                               (currentLang === 'zh' ? "持有" : "HOLD");
                const posLabel =
                    c.position_says === "BUY"  ? (currentLang === 'zh' ? "买入" : "BUY")  :
                    c.position_says === "SELL" ? (currentLang === 'zh' ? "卖出" : "SELL") :
                                                 (currentLang === 'zh' ? "持有" : "HOLD");
                conflictText.textContent = currentLang === 'zh'
                    ? `评级方向为「${ratingLabel}」，但仓位建议文本暗示「${posLabel}」。以仓位建议为准。`
                    : `Rating implies ${ratingLabel} but Position Sizing text implies ${posLabel}. Trust the Position Sizing narrative.`;
                conflictEl.style.display = "flex";
            } else {
                conflictEl.style.display = "none";
            }
        }

        if (ticker !== "--" && date !== "--") {
            loadAndDrawChart(ticker, date, summary.entry_price, summary.price_target, summary.stop_loss);
        }

        // Position the -2..+2 rating marker along the scale.
        const score = typeof summary.rating_score === "number" ? summary.rating_score : 0;
        const marker = document.getElementById("rating-marker");
        if (marker) {
            const pct = ((score + 2) / 4) * 100;    // -2 → 0%, +2 → 100%
            marker.style.left = pct + "%";
            marker.dataset.score = String(score);
            marker.className = "rating-marker " +
                (score >= 1 ? "buy" : score <= -1 ? "sell" : "hold");
        }

        document.getElementById("res-reasoning").textContent =
            summary.executive_summary || summary.reasoning ||
            (currentLang === 'zh' ? "暂无决策摘要。" : "No reasoning provided.");

        // ── Analyst Reports ─────────────────────────────────────────
        setMarkdown("report-market",       data.market_report);
        setMarkdown("report-sentiment",    data.sentiment_report);
        setMarkdown("report-news",         data.news_report);
        setMarkdown("report-fundamentals", data.fundamentals_report);

        // ── Investment Debate ────────────────────────────────────────
        const debateState = data.investment_debate_state || {};
        setMarkdown("debate-bull", debateState.bull_history);
        setMarkdown("debate-bear", debateState.bear_history);
        setMarkdown("debate-judge-content", debateState.judge_decision);

        // ── Risk Debate ──────────────────────────────────────────────
        const riskState = data.risk_debate_state || {};
        setMarkdown("risk-aggressive",    riskState.aggressive_history);
        setMarkdown("risk-neutral",       riskState.neutral_history);
        setMarkdown("risk-conservative",  riskState.conservative_history);
        setMarkdown("risk-judge-content", riskState.judge_decision);

        // ── Trader Plan ──────────────────────────────────────────────
        setMarkdown("trader-plan-content",   data.trader_investment_plan);
        setMarkdown("final-decision-content", data.final_trade_decision);

        // ── Raw Plan ─────────────────────────────────────────────────
        document.getElementById("raw-markdown").textContent = data.investment_plan || "";

        // Save to Local History Watchlist
        if (ticker !== "--" && date !== "--") {
            saveToHistory(ticker, date, ratingText, action, data);
        }
    }

    // Expose to global scope so the top-level loadHistoryItem() (which
    // lives outside this DOMContentLoaded closure) can call it.
    window.__taDisplayResults = displayResults;

    // ----------------------------------------------------------------
    // 9. Refresh recovery — reattach to a job still running after reload
    // ----------------------------------------------------------------
    (async function recoverActiveJob() {
        const savedJobId = localStorage.getItem(JOB_STORAGE_KEY);
        if (!savedJobId) return;
        try {
            const resp = await fetch(`/api/jobs/${savedJobId}`);
            if (!resp.ok) {
                localStorage.removeItem(JOB_STORAGE_KEY);
                return;
            }
            const snapshot = await resp.json();
            if (snapshot.status === "running") {
                resetLoadingView();
                attachToJob(savedJobId);
            } else {
                localStorage.removeItem(JOB_STORAGE_KEY);
                if (snapshot.status === "done" && snapshot.result) {
                    displayResults(snapshot.result);
                }
            }
        } catch (err) {
            console.warn("Could not recover active job", err);
        }
    })();

    // ----------------------------------------------------------------
    // 10. Helpers
    // ----------------------------------------------------------------

    function setMarkdown(id, text) {
        const el = document.getElementById(id);
        if (!el) return;
        const content = text ? text.trim() : "";
        if (!content) {
            el.innerHTML = '<div class="empty-state">' + (currentLang === 'zh' ? '暂无数据' : 'No data available') + '</div>';
        } else {
            el.innerHTML = renderMarkdown(content);
        }
    }

    function formatPrice(val) {
        if (val === null || val === undefined || val === "") return "--";
        const n = parseFloat(val);
        if (isNaN(n)) return String(val);
        return n.toFixed(2);
    }

    function cssEscape(value) {
        return window.CSS && CSS.escape ? CSS.escape(value) : value.replace(/"/g, '\\"');
    }

    function escapeHtml(text) {
        const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
        return String(text).replace(/[&<>"']/g, ch => map[ch]);
    }

    // ── Markdown Renderer ────────────────────────────────────────────
    // parseInline() escapes HTML entities before applying markdown-inline
    // transforms — every text-bearing node below (headers, list items,
    // table cells, paragraphs) routes through it, so this is the single
    // point that keeps fetched news/social content (untrusted, attacker-
    // reachable via prompt injection) from being interpreted as HTML.
    function renderMarkdown(md) {
        if (!md) return "";
        const lines  = md.split("\n");
        const html   = [];
        let inList   = false;
        let inOList  = false;
        let inTable  = false;
        let tableRows = [];

        for (let i = 0; i < lines.length; i++) {
            const raw  = lines[i];
            const line = raw.trim();

            if (inList && !line.match(/^[-*] /)) { html.push("</ul>"); inList = false; }
            if (inOList && !line.match(/^\d+\. /)) { html.push("</ol>"); inOList = false; }

            if (inTable && !line.startsWith("|")) {
                html.push(buildTable(tableRows));
                tableRows = []; inTable = false;
            }

            if (line.startsWith("#### ")) { html.push(`<h4>${parseInline(line.slice(5))}</h4>`); continue; }
            if (line.startsWith("### "))  { html.push(`<h3>${parseInline(line.slice(4))}</h3>`); continue; }
            if (line.startsWith("## "))   { html.push(`<h2>${parseInline(line.slice(3))}</h2>`); continue; }
            if (line.startsWith("# "))    { html.push(`<h1>${parseInline(line.slice(2))}</h1>`); continue; }

            if (line.match(/^[-*] /)) {
                if (!inList) { html.push("<ul>"); inList = true; }
                html.push(`<li>${parseInline(line.slice(2))}</li>`);
                continue;
            }
            if (line.match(/^\d+\. /)) {
                if (!inOList) { html.push("<ol>"); inOList = true; }
                html.push(`<li>${parseInline(line.replace(/^\d+\. /, ""))}</li>`);
                continue;
            }

            if (line.startsWith("|")) { inTable = true; tableRows.push(line); continue; }

            if (line === "---" || line === "***" || line === "___") { html.push("<hr>"); continue; }

            if (line === "") continue;

            html.push(`<p>${parseInline(line)}</p>`);
        }

        if (inList)  html.push("</ul>");
        if (inOList) html.push("</ol>");
        if (inTable) html.push(buildTable(tableRows));

        return html.join("\n");
    }

    function parseInline(text) {
        if (!text) return "";
        text = escapeHtml(text);
        text = text.replace(/\*\*\*(.+?)\*\*\*/g, "<strong><em>$1</em></strong>");
        text = text.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
        text = text.replace(/\*(.+?)\*/g, "<em>$1</em>");
        text = text.replace(/_(.+?)_/g, "<em>$1</em>");
        text = text.replace(/`(.+?)`/g, "<code>$1</code>");
        return text;
    }

    function buildTable(rows) {
        if (!rows.length) return "";
        const out   = ["<table>"];
        const parse = row => row.split("|").map(s => s.trim()).filter((_, i, a) => i > 0 && i < a.length - 1);

        const headers = parse(rows[0]);
        let start = 1;
        if (rows.length > 1 && rows[1].includes("---")) start = 2;

        out.push("<thead><tr>");
        headers.forEach(h => out.push(`<th>${parseInline(h)}</th>`));
        out.push("</tr></thead><tbody>");

        for (let i = start; i < rows.length; i++) {
            const cols = parse(rows[i]);
            if (!cols.length) continue;
            out.push("<tr>");
            cols.forEach(c => out.push(`<td>${parseInline(c)}</td>`));
            out.push("</tr>");
        }
        out.push("</tbody></table>");
        return out.join("\n");
    }

    // ------------------------------------------------------------------
    // Ticker autosuggest
    // ------------------------------------------------------------------
    // A curated seed of the most-typed US + A-share tickers. Covers the
    // instant-hit case; anything not here falls through to Yahoo Finance's
    // public search endpoint. Format: [symbol, name-zh|name-en, exchange].
    const TICKER_SEED = [
        // US mega/large cap
        ["AAPL", "苹果 Apple", "NASDAQ"],
        ["MSFT", "微软 Microsoft", "NASDAQ"],
        ["NVDA", "英伟达 NVIDIA", "NASDAQ"],
        ["GOOGL", "谷歌 Alphabet A", "NASDAQ"],
        ["GOOG", "谷歌 Alphabet C", "NASDAQ"],
        ["AMZN", "亚马逊 Amazon", "NASDAQ"],
        ["META", "Meta Platforms", "NASDAQ"],
        ["TSLA", "特斯拉 Tesla", "NASDAQ"],
        ["AVGO", "博通 Broadcom", "NASDAQ"],
        ["BRK.B", "伯克希尔 Berkshire B", "NYSE"],
        ["JPM", "摩根大通 JPMorgan", "NYSE"],
        ["V", "Visa", "NYSE"],
        ["MA", "万事达 Mastercard", "NYSE"],
        ["UNH", "联合健康 UnitedHealth", "NYSE"],
        ["XOM", "埃克森美孚 Exxon", "NYSE"],
        ["WMT", "沃尔玛 Walmart", "NYSE"],
        ["LLY", "礼来 Eli Lilly", "NYSE"],
        ["COST", "开市客 Costco", "NASDAQ"],
        ["NFLX", "奈飞 Netflix", "NASDAQ"],
        ["AMD", "AMD 超微", "NASDAQ"],
        ["INTC", "英特尔 Intel", "NASDAQ"],
        ["ORCL", "甲骨文 Oracle", "NYSE"],
        ["CRM", "Salesforce", "NYSE"],
        ["ADBE", "Adobe", "NASDAQ"],
        ["PLTR", "Palantir", "NYSE"],
        ["TLRY", "Tilray Brands", "NASDAQ"],
        ["COIN", "Coinbase", "NASDAQ"],
        ["UBER", "优步 Uber", "NYSE"],
        ["ABNB", "爱彼迎 Airbnb", "NASDAQ"],
        ["DIS", "迪士尼 Disney", "NYSE"],
        ["BA", "波音 Boeing", "NYSE"],
        ["GS", "高盛 Goldman Sachs", "NYSE"],
        ["MS", "摩根士丹利 Morgan Stanley", "NYSE"],
        ["BABA", "阿里巴巴 Alibaba", "NYSE"],
        ["JD", "京东 JD.com", "NASDAQ"],
        ["PDD", "拼多多 PDD Holdings", "NASDAQ"],
        ["BIDU", "百度 Baidu", "NASDAQ"],
        ["NIO", "蔚来 NIO", "NYSE"],
        ["XPEV", "小鹏 XPeng", "NYSE"],
        ["LI", "理想 Li Auto", "NASDAQ"],
        ["TSM", "台积电 TSMC", "NYSE"],
        // A 股：沪市 (6xxxxx.SS) + 深市 (0xxxxx/3xxxxx.SZ) + 北交所 (8/4xxxxx.BJ)
        ["600519.SS", "贵州茅台", "SSE"],
        ["601398.SS", "工商银行", "SSE"],
        ["601288.SS", "农业银行", "SSE"],
        ["601988.SS", "中国银行", "SSE"],
        ["601857.SS", "中国石油", "SSE"],
        ["600028.SS", "中国石化", "SSE"],
        ["600036.SS", "招商银行", "SSE"],
        ["601318.SS", "中国平安", "SSE"],
        ["601166.SS", "兴业银行", "SSE"],
        ["600030.SS", "中信证券", "SSE"],
        ["600009.SS", "上海机场", "SSE"],
        ["600276.SS", "恒瑞医药", "SSE"],
        ["600887.SS", "伊利股份", "SSE"],
        ["600690.SS", "海尔智家", "SSE"],
        ["601088.SS", "中国神华", "SSE"],
        ["601899.SS", "紫金矿业", "SSE"],
        ["601728.SS", "中国电信", "SSE"],
        ["600050.SS", "中国联通", "SSE"],
        ["601668.SS", "中国建筑", "SSE"],
        ["600031.SS", "三一重工", "SSE"],
        ["600585.SS", "海螺水泥", "SSE"],
        ["600809.SS", "山西汾酒", "SSE"],
        ["603288.SS", "海天味业", "SSE"],
        ["603259.SS", "药明康德", "SSE"],
        ["688981.SS", "中芯国际", "SSE-STAR"],
        ["688111.SS", "金山办公", "SSE-STAR"],
        ["688036.SS", "传音控股", "SSE-STAR"],
        ["000001.SZ", "平安银行", "SZSE"],
        ["000002.SZ", "万科A", "SZSE"],
        ["000333.SZ", "美的集团", "SZSE"],
        ["000651.SZ", "格力电器", "SZSE"],
        ["000858.SZ", "五粮液", "SZSE"],
        ["000568.SZ", "泸州老窖", "SZSE"],
        ["000725.SZ", "京东方A", "SZSE"],
        ["002415.SZ", "海康威视", "SZSE"],
        ["002594.SZ", "比亚迪", "SZSE"],
        ["002230.SZ", "科大讯飞", "SZSE"],
        ["002271.SZ", "东方雨虹", "SZSE"],
        ["002714.SZ", "牧原股份", "SZSE"],
        ["300750.SZ", "宁德时代", "ChiNext"],
        ["300059.SZ", "东方财富", "ChiNext"],
        ["300760.SZ", "迈瑞医疗", "ChiNext"],
        ["300015.SZ", "爱尔眼科", "ChiNext"],
        ["300124.SZ", "汇川技术", "ChiNext"],
        ["300274.SZ", "阳光电源", "ChiNext"],
    ];

    function initTickerAutosuggest() {
        const wrap  = document.getElementById("ticker-combobox");
        const input = document.getElementById("ticker");
        const list  = document.getElementById("ticker-suggest-list");
        if (!wrap || !input || !list) return;

        let items = [];
        let activeIdx = -1;
        let remoteTimer = null;
        let remoteCtrl = null;

        // Normalize A-share user input:
        //   "600519" -> "600519.SS"
        //   "000001" / "300750" -> "*.SZ"
        //   "8xxxxx" / "4xxxxx" -> "*.BJ"
        // Yahoo uses these suffixes on cn.finance data too.
        function normalizeAShare(raw) {
            const s = raw.trim();
            if (/^\d{6}$/.test(s)) {
                if (s.startsWith("6") || s.startsWith("9")) return s + ".SS";
                if (s.startsWith("0") || s.startsWith("2") || s.startsWith("3")) return s + ".SZ";
                if (s.startsWith("8") || s.startsWith("4")) return s + ".BJ";
            }
            return s.toUpperCase();
        }

        function localMatches(q) {
            const uq = q.toUpperCase();
            const num = q.replace(/\D/g, "");
            const scored = [];
            for (const [sym, name, ex] of TICKER_SEED) {
                let score = 0;
                if (sym === uq) score = 100;
                else if (sym.startsWith(uq)) score = 80;
                else if (num && sym.startsWith(num)) score = 70;
                else if (name.toUpperCase().includes(uq)) score = 40;
                else if (name.includes(q)) score = 40;
                if (score) scored.push({ symbol: sym, name, exch: ex, score });
            }
            scored.sort((a, b) => b.score - a.score);
            return scored.slice(0, 8);
        }

        async function fetchRemote(q, signal) {
            // Backend proxy that aggregates Yahoo (US/global) + Eastmoney
            // (A-share). Same-origin, so no CORS; failures fall back to
            // the local seed list silently.
            try {
                const res = await fetch(`/api/ticker-search?q=${encodeURIComponent(q)}`,
                                        { signal });
                if (!res.ok) return [];
                const data = await res.json();
                return (data.items || []).map(x => ({
                    symbol: x.symbol,
                    name: x.name || "",
                    exch: x.exch || "",
                    score: 10,
                }));
            } catch {
                return [];
            }
        }

        function render() {
            list.innerHTML = "";
            if (!items.length) { close(); return; }
            items.forEach((it, i) => {
                const li = document.createElement("li");
                li.role = "option";
                li.className = "ticker-suggest-item" + (i === activeIdx ? " active" : "");
                li.dataset.symbol = it.symbol;
                li.innerHTML =
                    `<span class="sym">${it.symbol}</span>` +
                    `<span class="nm">${it.name || ""}</span>` +
                    `<span class="ex">${it.exch || ""}</span>`;
                li.addEventListener("mousedown", (e) => {
                    // mousedown so it fires before input blur.
                    e.preventDefault();
                    pick(i);
                });
                list.appendChild(li);
            });
            list.hidden = false;
            input.setAttribute("aria-expanded", "true");
        }

        function close() {
            list.hidden = true;
            list.innerHTML = "";
            activeIdx = -1;
            input.setAttribute("aria-expanded", "false");
        }

        function pick(i) {
            if (i < 0 || i >= items.length) return;
            input.value = items[i].symbol;
            close();
        }

        function refresh() {
            const q = input.value.trim();
            if (!q) { items = []; render(); return; }
            items = localMatches(q);
            activeIdx = items.length ? 0 : -1;
            render();

            // Debounced remote fetch (only when query is meaningful and we
            // don't already have a strong exact-symbol hit).
            if (remoteCtrl) remoteCtrl.abort();
            clearTimeout(remoteTimer);
            if (q.length < 1) return;
            remoteTimer = setTimeout(async () => {
                remoteCtrl = new AbortController();
                const remote = await fetchRemote(q, remoteCtrl.signal);
                if (!remote.length) return;
                // Merge, dedupe by symbol, keep local first.
                const seen = new Set(items.map(x => x.symbol));
                for (const r of remote) {
                    if (!seen.has(r.symbol)) { items.push(r); seen.add(r.symbol); }
                }
                items = items.slice(0, 10);
                if (activeIdx < 0 && items.length) activeIdx = 0;
                render();
            }, 220);
        }

        input.addEventListener("input", refresh);
        input.addEventListener("focus", () => { if (input.value.trim()) refresh(); });
        input.addEventListener("blur",  () => setTimeout(close, 120));
        input.addEventListener("keydown", (e) => {
            if (list.hidden) return;
            if (e.key === "ArrowDown") { e.preventDefault(); activeIdx = Math.min(items.length - 1, activeIdx + 1); render(); }
            else if (e.key === "ArrowUp") { e.preventDefault(); activeIdx = Math.max(0, activeIdx - 1); render(); }
            else if (e.key === "Enter" && activeIdx >= 0) { e.preventDefault(); pick(activeIdx); }
            else if (e.key === "Escape") { close(); }
        });

        // On submit-time normalization: turn bare "600519" into "600519.SS".
        input.form && input.form.addEventListener("submit", () => {
            input.value = normalizeAShare(input.value);
        }, true);

        document.addEventListener("click", (e) => {
            if (!wrap.contains(e.target)) close();
        });
    }

    // ── Sidebar Collapse Logic ───────────────────────────────────────
    (function initSidebarCollapse() {
        const toggleBtn = document.getElementById("sidebar-toggle-btn");
        const appContainer = document.querySelector(".app-container");
        const sidebar = document.querySelector(".sidebar-config");
        if (!toggleBtn || !appContainer || !sidebar) return;

        const isCollapsed = localStorage.getItem("tradingagents.sidebarCollapsed") === "true";
        if (isCollapsed) {
            appContainer.classList.add("sidebar-collapsed");
            sidebar.classList.add("collapsed");
            toggleBtn.textContent = "▶";
        }

        toggleBtn.addEventListener("click", () => {
            const willCollapse = !appContainer.classList.contains("sidebar-collapsed");
            appContainer.classList.toggle("sidebar-collapsed", willCollapse);
            sidebar.classList.toggle("collapsed", willCollapse);
            toggleBtn.textContent = willCollapse ? "▶" : "◀";
            localStorage.setItem("tradingagents.sidebarCollapsed", String(willCollapse));
            
            // Re-render SVG chart on collapse/expand transition end to fit new space
            setTimeout(() => {
                triggerChartResize();
            }, 300);
        });
    })();

    // Initialize history watchlist rendering on load
    if (typeof renderHistoryList === "function") {
        renderHistoryList();
    }

    // Initialize sticky Table of Contents scroll behavior
    if (typeof initTOCScroller === "function") {
        initTOCScroller();
    }
});

// ── Globals and SVG Charting Helpers ─────────────────────────────────
let lastChartData = null;

function triggerChartResize() {
    if (lastChartData) {
        drawPriceChart(
            lastChartData.prices, 
            lastChartData.entry, 
            lastChartData.target, 
            lastChartData.stop, 
            lastChartData.tradeDate
        );
    }
}

window.addEventListener("resize", () => {
    triggerChartResize();
});

window.zoomCard = function(cardId) {
    const card = document.getElementById(cardId);
    if (!card) return;
    const isMax = card.classList.contains("maximized");
    // Remove maximized from all other cards first
    document.querySelectorAll(".analyst-card").forEach(c => c.classList.remove("maximized"));
    if (!isMax) {
        card.classList.add("maximized");
        card.querySelector(".card-zoom-btn").textContent = "✕";
    } else {
        card.querySelector(".card-zoom-btn").textContent = "🔎";
    }
};

async function loadAndDrawChart(ticker, date, entry, target, stop) {
    const chartContainer = document.getElementById("price-chart-container");
    const svg = document.getElementById("price-chart-svg");
    if (!chartContainer || !svg) return;

    chartContainer.style.display = "none";
    svg.innerHTML = "";
    lastChartData = null;

    try {
        const resp = await fetch(`/api/price/${encodeURIComponent(ticker)}?date=${encodeURIComponent(date)}`);
        if (!resp.ok) throw new Error("HTTP error " + resp.status);
        const data = await resp.json();
        if (data.status !== "success" || !data.prices || data.prices.length === 0) {
            console.warn("No price data for chart:", data.message);
            return;
        }

        lastChartData = {
            prices: data.prices,
            entry: entry,
            target: target,
            stop: stop,
            tradeDate: date
        };

        chartContainer.style.display = "block";
        drawPriceChart(data.prices, entry, target, stop, date);
    } catch (err) {
        console.error("Failed to load price chart:", err);
    }
}

function drawPriceChart(prices, entry, target, stop, tradeDate) {
    const svg = document.getElementById("price-chart-svg");
    if (!svg) return;

    svg.innerHTML = "";
    const width = svg.clientWidth || svg.parentElement.clientWidth || 800;
    const height = 120;
    
    const margin = { top: 12, right: 90, bottom: 20, left: 50 };
    const chartWidth = width - margin.left - margin.right;
    const chartHeight = height - margin.top - margin.bottom;

    const closeValues = prices.map(d => d.close);
    let allValues = [...closeValues];
    if (entry != null) allValues.push(entry);
    if (target != null) allValues.push(target);
    if (stop != null) allValues.push(stop);

    const yMin = Math.min(...allValues) * 0.98;
    const yMax = Math.max(...allValues) * 1.02;
    const yRange = yMax - yMin;

    const getX = (index) => margin.left + (index / (prices.length - 1)) * chartWidth;
    const getY = (val) => margin.top + chartHeight - ((val - yMin) / yRange) * chartHeight;

    // 1. Gridlines
    const gridLines = 3;
    for (let i = 0; i <= gridLines; i++) {
        const ratio = i / gridLines;
        const priceVal = yMin + ratio * yRange;
        const y = getY(priceVal);

        const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
        line.setAttribute("x1", margin.left);
        line.setAttribute("y1", y);
        line.setAttribute("x2", margin.left + chartWidth);
        line.setAttribute("y2", y);
        line.setAttribute("stroke", "rgba(255, 255, 255, 0.05)");
        line.setAttribute("stroke-width", "1");
        svg.appendChild(line);

        const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
        label.setAttribute("x", margin.left - 8);
        label.setAttribute("y", y + 4);
        label.setAttribute("fill", "var(--text-muted)");
        label.setAttribute("font-size", "10px");
        label.setAttribute("font-family", "var(--font-mono)");
        label.setAttribute("text-anchor", "end");
        label.textContent = priceVal.toFixed(2);
        svg.appendChild(label);
    }

    // 2. Build paths
    let pathD = "";
    let areaD = `M ${getX(0)} ${getY(yMin)} `;
    prices.forEach((p, idx) => {
        const x = getX(idx);
        const y = getY(p.close);
        if (idx === 0) {
            pathD += `M ${x} ${y} `;
        } else {
            pathD += `L ${x} ${y} `;
        }
        areaD += `L ${x} ${y} `;
    });
    areaD += `L ${getX(prices.length - 1)} ${getY(yMin)} Z`;

    const defs = document.createElementNS("http://www.w3.org/2000/svg", "defs");
    defs.innerHTML = `
        <linearGradient id="chart-grad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="var(--accent-blue)" stop-opacity="0.15"/>
            <stop offset="100%" stop-color="var(--accent-blue)" stop-opacity="0.0"/>
        </linearGradient>
    `;
    svg.appendChild(defs);

    const areaPath = document.createElementNS("http://www.w3.org/2000/svg", "path");
    areaPath.setAttribute("d", areaD);
    areaPath.setAttribute("fill", "url(#chart-grad)");
    svg.appendChild(areaPath);

    const linePath = document.createElementNS("http://www.w3.org/2000/svg", "path");
    linePath.setAttribute("d", pathD);
    linePath.setAttribute("fill", "none");
    linePath.setAttribute("stroke", "var(--accent-blue)");
    linePath.setAttribute("stroke-width", "1.8");
    svg.appendChild(linePath);

    // 3. Trade Date marker
    const tradeDateIdx = prices.findIndex(p => p.date === tradeDate);
    if (tradeDateIdx !== -1) {
        const x = getX(tradeDateIdx);
        const verticalLine = document.createElementNS("http://www.w3.org/2000/svg", "line");
        verticalLine.setAttribute("x1", x);
        verticalLine.setAttribute("y1", margin.top);
        verticalLine.setAttribute("x2", x);
        verticalLine.setAttribute("y2", margin.top + chartHeight);
        verticalLine.setAttribute("stroke", "rgba(255, 255, 255, 0.4)");
        verticalLine.setAttribute("stroke-width", "1");
        verticalLine.setAttribute("stroke-dasharray", "3,3");
        svg.appendChild(verticalLine);

        const intersectY = getY(prices[tradeDateIdx].close);
        const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
        dot.setAttribute("cx", x);
        dot.setAttribute("cy", intersectY);
        dot.setAttribute("r", "4");
        dot.setAttribute("fill", "var(--accent-pink)");
        dot.setAttribute("stroke", "#fff");
        dot.setAttribute("stroke-width", "1.5");
        svg.appendChild(dot);

        const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
        text.setAttribute("x", x);
        text.setAttribute("y", margin.top + chartHeight + 14);
        text.setAttribute("fill", "var(--accent-pink)");
        text.setAttribute("font-size", "10px");
        text.setAttribute("font-family", "var(--font-main)");
        text.setAttribute("font-weight", "600");
        text.setAttribute("text-anchor", "middle");
        text.textContent = tradeDate;
        svg.appendChild(text);
    }

    // Helper to draw horizontal guides
    function drawGuideLine(val, stroke, labelText, isDashed) {
        if (val == null) return;
        const y = getY(val);
        const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
        line.setAttribute("x1", margin.left);
        line.setAttribute("y1", y);
        line.setAttribute("x2", margin.left + chartWidth);
        line.setAttribute("y2", y);
        line.setAttribute("stroke", stroke);
        line.setAttribute("stroke-width", "1.2");
        if (isDashed) {
            line.setAttribute("stroke-dasharray", "4,3");
        }
        svg.appendChild(line);

        const txt = document.createElementNS("http://www.w3.org/2000/svg", "text");
        txt.setAttribute("x", margin.left + chartWidth + 6);
        txt.setAttribute("y", y + 3);
        txt.setAttribute("fill", stroke);
        txt.setAttribute("font-size", "10px");
        txt.setAttribute("font-family", "var(--font-main)");
        txt.setAttribute("font-weight", "600");
        txt.textContent = labelText + `: ${val.toFixed(2)}`;
        svg.appendChild(txt);
    }

    drawGuideLine(entry, "var(--accent-blue)", "Entry", false);
    drawGuideLine(target, "var(--signal-buy)", "Target", true);
    drawGuideLine(stop, "var(--signal-sell)", "Stop", true);

    // 5. X-Axis labels
    if (prices.length > 0) {
        const drawXLabel = (index, anchor) => {
            const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
            text.setAttribute("x", getX(index));
            text.setAttribute("y", margin.top + chartHeight + 14);
            text.setAttribute("fill", "var(--text-muted)");
            text.setAttribute("font-size", "10px");
            text.setAttribute("font-family", "var(--font-mono)");
            text.setAttribute("text-anchor", anchor);
            text.textContent = prices[index].date;
            svg.appendChild(text);
        };
        drawXLabel(0, "start");
        drawXLabel(prices.length - 1, "end");
    }

    // 6. Hover interactions
    const hoverRect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    hoverRect.setAttribute("x", margin.left);
    hoverRect.setAttribute("y", margin.top);
    hoverRect.setAttribute("width", chartWidth);
    hoverRect.setAttribute("height", chartHeight);
    hoverRect.setAttribute("fill", "transparent");
    hoverRect.style.cursor = "crosshair";
    svg.appendChild(hoverRect);

    const hoverLine = document.createElementNS("http://www.w3.org/2000/svg", "line");
    hoverLine.setAttribute("stroke", "rgba(255, 255, 255, 0.2)");
    hoverLine.setAttribute("stroke-width", "1");
    hoverLine.style.display = "none";
    svg.appendChild(hoverLine);

    const hoverDot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    hoverDot.setAttribute("r", "4");
    hoverDot.setAttribute("fill", "var(--accent-blue)");
    hoverDot.setAttribute("stroke", "#fff");
    hoverDot.setAttribute("stroke-width", "1");
    hoverDot.style.display = "none";
    svg.appendChild(hoverDot);

    let tooltip = document.getElementById("chart-hover-tooltip");
    if (!tooltip) {
        tooltip = document.createElement("div");
        tooltip.id = "chart-hover-tooltip";
        tooltip.className = "chart-tooltip";
        document.body.appendChild(tooltip);
    }

    hoverRect.addEventListener("mousemove", (e) => {
        const rect = svg.getBoundingClientRect();
        const mouseX = e.clientX - rect.left - margin.left;
        const frac = mouseX / chartWidth;
        const rawIdx = frac * (prices.length - 1);
        const idx = Math.max(0, Math.min(prices.length - 1, Math.round(rawIdx)));
        
        const p = prices[idx];
        const x = getX(idx);
        const y = getY(p.close);

        hoverLine.setAttribute("x1", x);
        hoverLine.setAttribute("y1", margin.top);
        hoverLine.setAttribute("x2", x);
        hoverLine.setAttribute("y2", margin.top + chartHeight);
        hoverLine.style.display = "block";

        hoverDot.setAttribute("cx", x);
        hoverDot.setAttribute("cy", y);
        hoverDot.style.display = "block";

        const tooltipX = e.pageX + 15;
        const tooltipY = e.pageY - 40;
        tooltip.style.left = tooltipX + "px";
        tooltip.style.top = tooltipY + "px";
        tooltip.style.display = "block";
        tooltip.innerHTML = `
            <div><strong>${p.date}</strong></div>
            <div>Price: $${p.close.toFixed(2)}</div>
        `;
    });

    hoverRect.addEventListener("mouseleave", () => {
        hoverLine.style.display = "none";
        hoverDot.style.display = "none";
        tooltip.style.display = "none";
    });
}

// ── Local History Watchlist Helper Functions ─────────────────────────
function saveToHistory(ticker, date, rating, action, fullData) {
    try {
        const key = 'tradingagents.history';
        const listStr = localStorage.getItem(key);
        let list = listStr ? JSON.parse(listStr) : [];
        
        list = list.filter(item => !(item.ticker === ticker && item.date === date));
        list.unshift({
            ticker: ticker,
            date: date,
            rating: rating || "Hold",
            action: action || "HOLD",
            timestamp: Date.now()
        });
        list = list.slice(0, 20);
        localStorage.setItem(key, JSON.stringify(list));
        
        const reportKey = `tradingagents.report.${ticker}.${date}`;
        localStorage.setItem(reportKey, JSON.stringify(fullData));
        
        renderHistoryList();
    } catch (e) {
        console.error("Failed to save history", e);
    }
}

function renderHistoryList() {
    const listEl = document.getElementById("history-list");
    if (!listEl) return;
    listEl.innerHTML = "";
    try {
        const key = 'tradingagents.history';
        const listStr = localStorage.getItem(key);
        const list = listStr ? JSON.parse(listStr) : [];
        if (list.length === 0) {
            listEl.innerHTML = `<div class="empty-state">${currentLang === 'zh' ? '无历史记录' : 'No history yet'}</div>`;
            return;
        }
        list.forEach(item => {
            const row = document.createElement("div");
            row.className = "history-item";
            const ratingText = item.rating;
            const action = item.action.toUpperCase();
            
            const RATING_MAP = {
                "Strong Buy": { "zh": "买入+", "en": "BUY+" },
                "Buy": { "zh": "买入", "en": "BUY" },
                "Overweight": { "zh": "增持", "en": "OW" },
                "Hold": { "zh": "持有", "en": "HOLD" },
                "Underweight": { "zh": "减持", "en": "UW" },
                "Sell": { "zh": "卖出", "en": "SELL" },
                "Strong Sell": { "zh": "卖出+", "en": "SELL+" }
            };
            const mapped = RATING_MAP[ratingText] || RATING_MAP[action];
            const displayRating = mapped ? mapped[currentLang] : ratingText;

            let badgeClass = "hold";
            if (action.includes("BUY")) badgeClass = "buy";
            else if (action.includes("SELL")) badgeClass = "sell";

            row.innerHTML = `
                <div>
                    <span class="hist-sym">${item.ticker}</span>
                    <span class="hist-date">${item.date}</span>
                </div>
                <span class="hist-badge ${badgeClass}">${displayRating}</span>
            `;
            
            row.addEventListener("click", () => {
                loadHistoryItem(item.ticker, item.date);
            });
            listEl.appendChild(row);
        });
    } catch (e) {
        console.error("Failed to render history list", e);
    }
}

function loadHistoryItem(ticker, date) {
    try {
        const key = `tradingagents.report.${ticker}.${date}`;
        const dataStr = localStorage.getItem(key);
        if (dataStr) {
            const data = JSON.parse(dataStr);
            if (typeof window.__taDisplayResults === "function") {
                window.__taDisplayResults(data);
            } else {
                console.error("displayResults not ready yet; is DOMContentLoaded fired?");
            }
        } else {
            alert(currentLang === 'zh' ? "未找到该历史报告的数据" : "Report data not found in local storage.");
        }
    } catch (e) {
        console.error("Failed to load history item", e);
    }
}

// ── Sticky Table of Contents (TOC) Scroller Navigation ────────────────
function initTOCScroller() {
    const links = document.querySelectorAll(".toc-link");
    const sections = document.querySelectorAll(".sec-anchor");
    // The report scroll lives on #results-view (.results-container), not
    // on .main-content — that outer wrapper is overflow:hidden so it
    // never emits scroll events. See style.css .results-container.
    const scroller = document.querySelector("#results-view") || document.querySelector(".main-content");
    if (!links.length || !sections.length || !scroller) return;

    // Handle scroll highlighting
    scroller.addEventListener("scroll", () => {
        let activeId = "";
        const scrollPos = scroller.scrollTop + 60; // offset for headers

        sections.forEach(sec => {
            if (scrollPos >= sec.offsetTop) {
                activeId = sec.id;
            }
        });

        // Fallback to first section if at top
        if (!activeId && sections.length > 0) {
            activeId = sections[0].id;
        }

        links.forEach(link => {
            const isActive = link.getAttribute("href") === `#${activeId}`;
            link.classList.toggle("active", isActive);
        });
    });

    // Handle click smooth scroll within the report scroller
    links.forEach(link => {
        link.addEventListener("click", e => {
            e.preventDefault();
            const targetId = link.getAttribute("href").substring(1);
            const targetEl = document.getElementById(targetId);
            if (targetEl) {
                scroller.scrollTo({
                    top: targetEl.offsetTop - 12,
                    behavior: "smooth"
                });
            }
        });
    });
}

// ── Reading font-size control ────────────────────────────────────────
// Binds the A− / A / A+ / A++ chip in the sidebar header to
// --reading-scale on <html>, which all .markdown-content sizing keys
// off. Persisted in localStorage so the choice sticks across sessions.
(function initReadingScale() {
    const KEY = 'tradingagents.readingScale';
    const btns = document.querySelectorAll('.rc-btn');
    if (!btns.length) return;

    const saved = parseFloat(localStorage.getItem(KEY) || '1');
    apply(Number.isFinite(saved) ? saved : 1);

    btns.forEach(btn => {
        btn.addEventListener('click', () => {
            const s = parseFloat(btn.dataset.scale);
            if (!Number.isFinite(s)) return;
            apply(s);
            try { localStorage.setItem(KEY, String(s)); } catch (e) { /* ignore */ }
        });
    });

    function apply(scale) {
        document.documentElement.style.setProperty('--reading-scale', String(scale));
        btns.forEach(b => {
            const isActive = Math.abs(parseFloat(b.dataset.scale) - scale) < 0.001;
            b.setAttribute('aria-current', String(isActive));
        });
    }
})();
