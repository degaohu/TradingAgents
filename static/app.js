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
    // Re-render toc-name spans (they have data-zh but also children-level span)
    document.querySelectorAll('.toc-name[data-zh]').forEach(el => {
        el.textContent = lang === 'zh' ? el.dataset.zh : el.dataset.en;
    });
    // Re-render agent status labels
    document.querySelectorAll('.agent-status-label[data-zh]').forEach(el => {
        el.textContent = lang === 'zh' ? el.dataset.zh : el.dataset.en;
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
    // Swap disclaimer body between Chinese and English.
    document.querySelectorAll('[data-zh-visible]').forEach(el => { el.hidden = lang !== 'zh'; });
    document.querySelectorAll('[data-en-visible]').forEach(el => { el.hidden = lang !== 'en'; });
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

    // Disclaimer gate — must be acknowledged before the dashboard is usable.
    // We store an ack timestamp in localStorage; expires after 30 days so
    // users re-consent periodically without seeing the modal every visit.
    (function initDisclaimer() {
        const KEY = "tradingagents.disclaimerAckedAt";
        const TTL_MS = 30 * 24 * 60 * 60 * 1000;   // 30 days
        const modal   = document.getElementById("disclaimer-modal");
        const accept  = document.getElementById("disclaimer-accept");
        const decline = document.getElementById("disclaimer-decline");
        const check   = document.getElementById("disclaimer-ack");
        if (!modal) return;

        const acked = parseInt(localStorage.getItem(KEY) || "0", 10);
        if (acked && (Date.now() - acked) < TTL_MS) {
            modal.hidden = true;
            document.body.classList.remove("modal-open");
            return;
        }
        modal.hidden = false;
        document.body.classList.add("modal-open");

        check.addEventListener("change", () => { accept.disabled = !check.checked; });
        accept.addEventListener("click", () => {
            if (!check.checked) return;
            localStorage.setItem(KEY, String(Date.now()));
            modal.hidden = true;
            document.body.classList.remove("modal-open");
        });
        decline.addEventListener("click", () => {
            // Best-effort exit: navigate away. Some browsers block window.close()
            // for pages they didn't open, so send the user to about:blank as fallback.
            try { window.close(); } catch (_) { /* ignore */ }
            window.location.href = "about:blank";
        });
    })();

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

    // Account bar: current-user display, admin-panel link (admin only),
    // and logout — the dashboard is multi-user (see web/routes.py's login
    // system) but nothing surfaced a way to see who's logged in, reach
    // /admin, or log out until now.
    (function initAccountBar() {
        const usernameEl = document.getElementById('account-username');
        const adminLink = document.getElementById('account-admin-link');
        const logoutBtn = document.getElementById('account-logout-btn');
        if (!usernameEl || !logoutBtn) return;

        const versionEl = document.getElementById('account-version');
        fetch('/api/me')
            .then(r => r.ok ? r.json() : null)
            .then(me => {
                if (!me || !me.username) return;
                usernameEl.textContent = me.username;
                usernameEl.title = me.username;
                if (me.is_admin && adminLink) adminLink.style.display = '';
                const advPanel = document.getElementById('advanced-config-panel');
                if (advPanel && me.is_admin) {
                    advPanel.style.display = '';
                }
                if (me.version && versionEl) versionEl.textContent = 'v' + me.version;
                updateQuotaUI(me);
            })
            .catch(() => { /* account bar is a nicety, not critical path */ });

        logoutBtn.addEventListener('click', async () => {
            try { await fetch('/api/logout', { method: 'POST' }); } catch (e) { /* ignore */ }
            location.href = '/login';
        });
    })();

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
    let currentlySubmitting = false;
    let quotaExhausted = false;  // true when a non-admin has 0 reports left

    function showView(view) {
        [welcomeView, loadingView].forEach(v => v.classList.remove("active"));
        resultsView.classList.remove("active");
        if (view === "results") {
            resultsView.classList.add("active");
        } else {
            view.classList.add("active");
        }
        // On mobile, the Analyze tab stacks the ticker/date form above
        // whatever's below it — once there's a pipeline or a report to
        // show, collapse the form to a compact row so that content gets
        // the room instead of sharing space with a full-size idle form.
        configForm.classList.toggle("form-collapsed", view !== welcomeView);
    }

    function setSubmitting(isSubmitting) {
        currentlySubmitting = isSubmitting;
        // Stay disabled if the user is also out of report quota.
        submitBtn.disabled = isSubmitting || quotaExhausted;
        submitBtn.querySelector("span").textContent =
            currentLang === 'zh'
                ? (isSubmitting ? "分析中..." : "开始分析")
                : (isSubmitting ? "Analyzing..." : "Run Analysis");
    }

    // Reflect the user's remaining report quota (from /api/me) in the sidebar
    // indicator and gate the submit button when it hits zero. Admins have
    // quota === null (unlimited) and see no indicator.
    function updateQuotaUI(me) {
        const ind = document.getElementById('quota-indicator');
        const valEl = document.getElementById('quota-value');
        const warnEl = document.getElementById('quota-warning');
        if (!ind || !valEl || !warnEl) return;
        const q = me ? me.quota : undefined;
        if (q === null || q === undefined) {
            ind.style.display = 'none';
            quotaExhausted = false;
        } else {
            const threshold = (me && me.quota_low_threshold) || 3;
            ind.style.display = 'flex';
            valEl.textContent = q;
            ind.classList.remove('quota-low', 'quota-empty');
            if (q <= 0) {
                ind.classList.add('quota-empty');
                warnEl.textContent = currentLang === 'zh' ? '· 已用完，请联系管理员' : '· Depleted — contact admin';
            } else if (q <= threshold) {
                ind.classList.add('quota-low');
                warnEl.textContent = currentLang === 'zh' ? '· 次数偏低' : '· Running low';
            } else {
                warnEl.textContent = '';
            }
            quotaExhausted = q <= 0;
        }
        submitBtn.disabled = currentlySubmitting || quotaExhausted;
    }

    // Re-fetch /api/me and refresh the quota UI — called after a report
    // completes (the balance was just decremented server-side) and after a
    // quota-exceeded rejection.
    async function refreshQuota() {
        try {
            const me = await fetch('/api/me').then(r => r.ok ? r.json() : null);
            if (me) updateQuotaUI(me);
        } catch (e) { /* best effort */ }
    }
    window.__taRefreshQuota = refreshQuota;

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
            if (response.status === 403) {
                // Out of report quota — show the server's message and refresh
                // the indicator (it will flip to the empty/red state).
                const body = await response.json().catch(() => ({}));
                const msg = (body.detail && body.detail.message)
                    || (currentLang === 'zh' ? '报告生成次数已用完。' : 'Report quota exhausted.');
                setSubmitting(false);
                showView(welcomeView);
                refreshQuota();
                alert(msg);
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
        // Reflect the new "Running" entry in the History list immediately.
        if (typeof renderHistoryList === "function") renderHistoryList();

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
                refreshQuota();  // a report was produced — balance decremented
                if (typeof window.__taNotify === "function") {
                    const d = payload.data || {};
                    const t = d.company_of_interest || d.ticker || "";
                    const rating = (d.decision_summary && d.decision_summary.rating) || d.decision || "";
                    window.__taNotify(
                        currentLang === 'zh' ? `分析完成 · ${t}` : `Analysis done · ${t}`,
                        currentLang === 'zh' ? `建议：${rating}` : `Rating: ${rating}`
                    );
                }
                break;
            case "error":
                closeStream();
                localStorage.removeItem(JOB_STORAGE_KEY);
                setSubmitting(false);
                showInlineError((currentLang === 'zh' ? "分析出错：" : "Analysis failed: ") + payload.message);
                if (typeof window.__taNotify === "function") {
                    window.__taNotify(
                        currentLang === 'zh' ? "分析失败" : "Analysis failed",
                        payload.message || ""
                    );
                }
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
        // Drop the synthetic "Running" row and pull the final entry.
        if (typeof renderHistoryList === "function") renderHistoryList();
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
        // Re-submit with the exact same form (checkpoint_enabled is on by
        // default, so backend picks up from wherever it stopped).
        const form = document.getElementById("config-form");
        if (form) {
            if (form.requestSubmit) form.requestSubmit();
            else form.submit();
        } else {
            showView(welcomeView);
        }
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

        // Sync section status dots in the report view (if already showing)
        if (typeof window.__taSetSectionStatus === "function") {
            const elementToSectionKey = {
                "report-market":       "technical",
                "report-fundamentals": "fundamentals",
                "report-news":         "news",
                "report-sentiment":    "sentiment",
                "debate-bull":         "debate",
                "risk-judge-content":  "risk",
            };
            Object.keys(reports).forEach(elementId => {
                const sectionKey = elementToSectionKey[elementId];
                if (sectionKey) {
                    window.__taSetSectionStatus(sectionKey, status === "done" ? "completed" : status === "running" ? "running" : "pending");
                }
            });
            // When a stage is running, mark the most relevant section as running
            if (status === "running") {
                const stageIdLower = (stageId || "").toLowerCase();
                if (stageIdLower.includes("market") || stageIdLower.includes("technical")) {
                    window.__taSetSectionStatus("technical", "running");
                } else if (stageIdLower.includes("fundamental")) {
                    window.__taSetSectionStatus("fundamentals", "running");
                } else if (stageIdLower.includes("news")) {
                    window.__taSetSectionStatus("news", "running");
                } else if (stageIdLower.includes("sentiment") || stageIdLower.includes("social")) {
                    window.__taSetSectionStatus("sentiment", "running");
                } else if (stageIdLower.includes("debate") || stageIdLower.includes("bull") || stageIdLower.includes("bear")) {
                    window.__taSetSectionStatus("debate", "running");
                } else if (stageIdLower.includes("risk") || stageIdLower.includes("portfolio")) {
                    window.__taSetSectionStatus("risk", "running");
                }
            }
        }

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
        if (text.includes("✔"))       line.classList.add("log-ok");
        else if (text.includes("▶")) line.classList.add("log-run");
        else if (text.includes("ℹ")) line.classList.add("log-info");
        else if (text.includes("⟳")) line.classList.add("log-retry");
        else if (/\bWARN|WARNING|ERR|ERROR|Traceback/i.test(text)) line.classList.add("log-warn");
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

        // Now that the report is visible and scrolled to top, recompute the
        // active section so the nav (and the mobile pill) shows the first
        // section rather than whatever the hidden-layout init guessed.
        requestAnimationFrame(() => {
            if (typeof window.__taSyncActiveSection === "function") window.__taSyncActiveSection();
        });

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

        const tocBadge = document.getElementById("toc-decision-badge");
        if (tocBadge) {
            tocBadge.className = "toc-decision-badge";
            if (action.includes("BUY")) {
                tocBadge.classList.add("bull");
                tocBadge.textContent = currentLang === 'zh' ? "看多 BULL" : "BULL";
            } else if (action.includes("SELL")) {
                tocBadge.classList.add("bear");
                tocBadge.textContent = currentLang === 'zh' ? "看空 BEAR" : "BEAR";
            } else {
                tocBadge.classList.add("neutral");
                tocBadge.textContent = currentLang === 'zh' ? "中性 NEUTRAL" : "NEUTRAL";
            }
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

        // ── Confidence gauge ────────────────────────────────────────
        renderConfidence(summary.confidence);

        // ── Chart with decision-level overlays ──────────────────────
        renderPriceChart({
            ticker: ticker,
            date: date,
            entry: summary.entry_price,
            stop:  summary.stop_loss,
            target: summary.price_target,
            action: action,
        });

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

        // ── Raw Plan ─────────────────────────────────────────────────
        document.getElementById("raw-markdown").textContent = data.investment_plan || "";

        // ── Update section status indicators ─────────────────────────
        // Mark each agent section as completed once data arrives.
        const sectionStatusMap = {
            "technical":    "report-market",
            "fundamentals": "report-fundamentals",
            "news":         "report-news",
            "sentiment":    "report-sentiment",
            "debate":       "debate-bull",
            "risk":         "risk-judge-content",
        };
        Object.entries(sectionStatusMap).forEach(([section, contentId]) => {
            const el = document.getElementById(contentId);
            const hasContent = el && el.textContent.trim() && !el.querySelector('.empty-state');
            setSectionStatus(section, hasContent ? "completed" : "pending");
        });

        // History is now saved server-side the moment the job finishes
        // (see web/history.py via _run_job in routes.py) — just refresh
        // the list so a completed run shows up immediately without
        // requiring a reload.
        if (ticker !== "--" && date !== "--") {
            renderHistoryList();
        }

        // Trigger reading time calculations for the reader view
        if (typeof window.__taCalculateReadingTime === "function") {
            window.__taCalculateReadingTime();
        }
    }

    /**
     * Update the status dot and label for a report section.
     * @param {string} sectionKey - one of: technical, fundamentals, news, sentiment, debate, risk
     * @param {"pending"|"running"|"completed"|"failed"} status
     */
    function setSectionStatus(sectionKey, status) {
        const statusEl = document.getElementById(`status-${sectionKey}`);
        const tocDot   = document.getElementById(`toc-dot-${sectionKey}`);
        if (!statusEl) return;
        const dot   = statusEl.querySelector('.agent-status-dot');
        const label = statusEl.querySelector('.agent-status-label');

        const statusConfig = {
            pending:   { cls: 'status-pending',   zh: '待运行',   en: 'Pending'   },
            running:   { cls: 'status-running',   zh: '分析中',   en: 'Analyzing' },
            completed: { cls: 'status-completed', zh: '已完成',   en: 'Completed' },
            failed:    { cls: 'status-failed',    zh: '分析失败', en: 'Failed'    },
        };
        const cfg = statusConfig[status] || statusConfig.pending;

        if (dot) {
            dot.className = `agent-status-dot ${cfg.cls}`;
        }
        if (label) {
            label.dataset.zh = cfg.zh;
            label.dataset.en = cfg.en;
            label.textContent = currentLang === 'zh' ? cfg.zh : cfg.en;
        }
        if (tocDot) {
            tocDot.className = `toc-status-dot ${cfg.cls}`;
        }
    }

    // Expose setSectionStatus globally for use in SSE handlers
    window.__taSetSectionStatus = setSectionStatus;

    // Expose to global scope so the top-level loadHistoryItem() (which
    // lives outside this DOMContentLoaded closure) can call it.
    window.__taDisplayResults = displayResults;

    // ----------------------------------------------------------------
    // 9. Refresh recovery — reattach to a job still running after reload,
    // even from a different device/browser than the one that started it.
    // ----------------------------------------------------------------
    (async function recoverActiveJob() {
        let jobId = localStorage.getItem(JOB_STORAGE_KEY);
        if (!jobId) {
            // Nothing remembered on *this* device/browser — ask the server
            // whether this logged-in user has a job running elsewhere
            // (started on another device, or local storage was cleared).
            try {
                const me = await fetch("/api/me").then(r => r.ok ? r.json() : null);
                if (me && me.active_job_id) jobId = me.active_job_id;
            } catch (err) { /* best effort */ }
            if (!jobId) return;
        }
        try {
            const resp = await fetch(`/api/jobs/${jobId}`);
            if (!resp.ok) {
                localStorage.removeItem(JOB_STORAGE_KEY);
                return;
            }
            const snapshot = await resp.json();
            if (snapshot.status === "running") {
                resetLoadingView();
                attachToJob(jobId);
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
        // Wrapped in a horizontally-scrollable div: financial report tables
        // routinely have 5+ columns, and the results view's parent sets
        // overflow-x:hidden — without this wrapper, a wide table on a
        // ~375px phone screen just gets clipped/squeezed illegibly instead
        // of being scrollable.
        const out = ['<div class="table-scroll"><table>'];
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
        out.push("</tbody></table></div>");
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
        // IME (Chinese/Japanese/Korean) commits text on compositionend, and
        // some browser+IME combos don't fire a follow-up `input` event —
        // so refresh explicitly, otherwise typing 「云图」 leaves the box
        // showing stale suggestions from the partial pinyin.
        input.addEventListener("compositionend", refresh);
        input.addEventListener("focus", () => { if (input.value.trim()) refresh(); });
        input.addEventListener("blur",  () => setTimeout(close, 120));
        input.addEventListener("keydown", (e) => {
            if (list.hidden) return;
            if (e.key === "ArrowDown") { e.preventDefault(); activeIdx = Math.min(items.length - 1, activeIdx + 1); render(); }
            else if (e.key === "ArrowUp") { e.preventDefault(); activeIdx = Math.max(0, activeIdx - 1); render(); }
            else if (e.key === "Enter" && activeIdx >= 0) { e.preventDefault(); pick(activeIdx); }
            else if (e.key === "Escape") { close(); }
        });

        // On submit-time normalization:
        //   1. If the dropdown is open and has an active item, pick it first
        //      (handles the case where user types "云图", sees the suggestion,
        //      then hits "开始分析" without clicking the suggestion item).
        //   2. Turn bare "600519" into "600519.SS" etc.
        input.form && input.form.addEventListener("submit", () => {
            // Auto-pick the first suggestion if the input value looks like
            // a company name rather than a symbol (contains non-ASCII or
            // isn't already a valid ticker code).
            if (items.length > 0 && !list.hidden) {
                const idx = activeIdx >= 0 ? activeIdx : 0;
                pick(idx);   // sets input.value to the symbol
            }
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

    // ── 9. Editorial Reader Mode & Text-to-Speech Broadcaster ─────────
    (function initEditorialReaderMode() {
        const toggleBtn = document.getElementById("toggle-reader-mode-btn");
        const settingsPanel = document.getElementById("reader-settings-panel");
        const resultsView = document.getElementById("results-view");
        
        // Font buttons
        const decFontBtn = document.getElementById("reader-font-dec");
        const incFontBtn = document.getElementById("reader-font-inc");
        const serifFontBtn = document.getElementById("reader-font-serif");
        const sansFontBtn = document.getElementById("reader-font-sans");
        
        // Theme dots
        const themePaper = document.getElementById("reader-theme-paper");
        const themeWhite = document.getElementById("reader-theme-white");
        const themeDark = document.getElementById("reader-theme-dark");
        
        // TTS buttons
        const ttsPlayBtn = document.getElementById("tts-play-btn");
        const ttsStopBtn = document.getElementById("tts-stop-btn");
        const ttsSpeedSelect = document.getElementById("tts-speed-select");

        if (!toggleBtn || !resultsView) return;

        // --- Preferences and Settings ---
        let readerActive = localStorage.getItem("tradingagents.reader.active") === "true";
        let fontSize = parseInt(localStorage.getItem("tradingagents.reader.fontSize") || "17", 10);
        let fontFamily = localStorage.getItem("tradingagents.reader.fontFamily") || "serif";
        let currentTheme = localStorage.getItem("tradingagents.reader.theme") || "paper";

        // Limit font range
        const MIN_FONT = 14;
        const MAX_FONT = 24;

        function applySettings() {
            // Apply reader mode class
            resultsView.classList.toggle("editorial-reader-mode", readerActive);
            settingsPanel.style.display = readerActive ? "flex" : "none";

            // Update toggle button text based on state and current language
            const btnTextEl = toggleBtn.querySelector(".btn-text-lang");
            if (btnTextEl) {
                if (readerActive) {
                    btnTextEl.setAttribute("data-zh", "返回仪表盘模式");
                    btnTextEl.setAttribute("data-en", "Dashboard View");
                    btnTextEl.textContent = currentLang === "zh" ? "返回仪表盘模式" : "Dashboard View";
                } else {
                    btnTextEl.setAttribute("data-zh", "社论阅读模式");
                    btnTextEl.setAttribute("data-en", "Reader View");
                    btnTextEl.textContent = currentLang === "zh" ? "社论阅读模式" : "Reader View";
                }
            }

            if (readerActive) {
                // Apply font size
                document.documentElement.style.setProperty("--r-font-size", fontSize + "px");

                // Apply font family
                const fontVal = fontFamily === "serif" ? "Georgia, Cambria, 'Times New Roman', serif" : "var(--font-sans, system-ui, -apple-system, sans-serif)";
                document.documentElement.style.setProperty("--r-font-family", fontVal);

                // Update font family button states
                serifFontBtn.classList.toggle("active", fontFamily === "serif");
                sansFontBtn.classList.toggle("active", fontFamily === "sans");

                // Apply theme variables
                const styles = {
                    paper: {
                        "--r-bg": "var(--paper-bg)",
                        "--r-text": "var(--paper-text)",
                        "--r-card": "var(--paper-card)",
                        "--r-border": "var(--paper-border)",
                        "--r-header": "var(--paper-header)",
                        "--r-heading": "var(--paper-heading)",
                        "--r-muted": "var(--paper-muted)"
                    },
                    white: {
                        "--r-bg": "var(--white-bg)",
                        "--r-text": "var(--white-text)",
                        "--r-card": "var(--white-card)",
                        "--r-border": "var(--white-border)",
                        "--r-header": "var(--white-header)",
                        "--r-heading": "var(--white-heading)",
                        "--r-muted": "var(--white-muted)"
                    },
                    dark: {
                        "--r-bg": "var(--dark-bg)",
                        "--r-text": "var(--dark-text)",
                        "--r-card": "var(--dark-card)",
                        "--r-border": "var(--dark-border)",
                        "--r-header": "var(--dark-header)",
                        "--r-heading": "var(--dark-heading)",
                        "--r-muted": "var(--dark-muted)"
                    }
                };

                const currentThemeStyles = styles[currentTheme] || styles.paper;
                Object.entries(currentThemeStyles).forEach(([prop, val]) => {
                    document.documentElement.style.setProperty(prop, val);
                });

                // Update active dot in theme selector
                themePaper.classList.toggle("active", currentTheme === "paper");
                themeWhite.classList.toggle("active", currentTheme === "white");
                themeDark.classList.toggle("active", currentTheme === "dark");
            } else {
                // Clear inline css variables when reader is disabled
                const propsToClear = [
                    "--r-font-size", "--r-font-family", "--r-bg", "--r-text", 
                    "--r-card", "--r-border", "--r-header", "--r-heading", "--r-muted"
                ];
                propsToClear.forEach(prop => document.documentElement.style.removeProperty(prop));
                // Stop voice if user exits reader mode
                stopReading();
            }

            // Sync layout size/re-align sidebar
            if (typeof window.__taSyncActiveSection === "function") {
                setTimeout(window.__taSyncActiveSection, 50);
            }
        }

        // Toggle action
        toggleBtn.addEventListener("click", () => {
            readerActive = !readerActive;
            localStorage.setItem("tradingagents.reader.active", String(readerActive));
            applySettings();
        });

        // Font Adjustments
        decFontBtn.addEventListener("click", () => {
            if (fontSize > MIN_FONT) {
                fontSize -= 1;
                localStorage.setItem("tradingagents.reader.fontSize", String(fontSize));
                applySettings();
            }
        });
        incFontBtn.addEventListener("click", () => {
            if (fontSize < MAX_FONT) {
                fontSize += 1;
                localStorage.setItem("tradingagents.reader.fontSize", String(fontSize));
                applySettings();
            }
        });

        // Font Family Switchers
        serifFontBtn.addEventListener("click", () => {
            fontFamily = "serif";
            localStorage.setItem("tradingagents.reader.fontFamily", fontFamily);
            applySettings();
        });
        sansFontBtn.addEventListener("click", () => {
            fontFamily = "sans";
            localStorage.setItem("tradingagents.reader.fontFamily", fontFamily);
            applySettings();
        });

        // Theme Switchers
        themePaper.addEventListener("click", () => {
            currentTheme = "paper";
            localStorage.setItem("tradingagents.reader.theme", currentTheme);
            applySettings();
        });
        themeWhite.addEventListener("click", () => {
            currentTheme = "white";
            localStorage.setItem("tradingagents.reader.theme", currentTheme);
            applySettings();
        });
        themeDark.addEventListener("click", () => {
            currentTheme = "dark";
            localStorage.setItem("tradingagents.reader.theme", currentTheme);
            applySettings();
        });

        // --- Text-to-Speech (TTS) Voice Broadcaster ---
        let synth = window.speechSynthesis;
        let playQueue = [];
        let currentQueueIndex = -1;
        let isSpeaking = false;
        let isPaused = false;
        let currentUtterance = null;

        // Estimate reading time dynamically
        function calculateReadingTime() {
            const flowContainer = document.querySelector(".report-flow-container");
            if (!flowContainer) return;
            const sections = flowContainer.querySelectorAll(".report-section");
            
            sections.forEach(sec => {
                // Check if time badge already exists
                let badge = sec.querySelector(".read-time-badge");
                if (!badge) {
                    badge = document.createElement("span");
                    badge.className = "read-time-badge";
                    
                    const secMeta = sec.querySelector(".report-section-meta");
                    if (secMeta) {
                        secMeta.appendChild(badge);
                    } else {
                        // fallback to section header
                        const header = sec.querySelector(".report-section-header");
                        if (header) header.appendChild(badge);
                    }
                }
                
                const bodyText = sec.querySelector(".report-section-body")?.textContent || "";
                const cleanText = bodyText.trim().replace(/\s+/g, "");
                
                // Average speed: 400 characters per minute for Chinese, 200 words for English
                const isChinese = /[\u4e00-\u9fa5]/.test(cleanText);
                let minutes = 1;
                if (isChinese) {
                    minutes = Math.max(1, Math.ceil(cleanText.length / 400));
                } else {
                    const wordCount = bodyText.trim().split(/\s+/).length;
                    minutes = Math.max(1, Math.ceil(wordCount / 200));
                }
                
                badge.setAttribute("data-zh", `⏳ ${minutes} 分钟阅读`);
                badge.setAttribute("data-en", `⏳ ${minutes} min read`);
                badge.textContent = currentLang === "zh" ? `⏳ ${minutes} 分钟阅读` : `⏳ ${minutes} min read`;
            });
        }

        // Expose time calculation globally so we can refresh it after displaying results
        window.__taCalculateReadingTime = calculateReadingTime;

        // Parse section text for reading
        function getSectionPlayText(sectionEl) {
            // Get title
            const title = sectionEl.querySelector(".report-section-title")?.textContent || "";
            // Get body text, filter out raw codes
            const body = sectionEl.querySelector(".report-section-body")?.textContent || "";
            
            // Clean up the text a bit for natural speech
            return (title + "。\n" + body)
                .replace(/\|/g, " ") // replace table dividers
                .replace(/[-*#]/g, " ") // replace markdown markers
                .replace(/\s+/g, " ")
                .trim();
        }

        function populateQueue() {
            playQueue = [];
            const sections = document.querySelectorAll(".report-flow-container .report-section");
            sections.forEach((sec, idx) => {
                playQueue.push({
                    element: sec,
                    text: getSectionPlayText(sec),
                    index: idx
                });
            });
        }

        function speakSection(index) {
            if (!synth) return;
            synth.cancel(); // clear any pending speaking

            if (index < 0 || index >= playQueue.length) {
                stopReading();
                return;
            }

            currentQueueIndex = index;
            const item = playQueue[index];
            
            // Remove previous active highlights
            document.querySelectorAll(".report-section.reading-active").forEach(el => {
                el.classList.remove("reading-active");
            });

            // Highlight active section
            item.element.classList.add("reading-active");
            
            // Smoothly scroll to active section
            const scroller = document.getElementById("results-view");
            if (scroller) {
                scroller.scrollTo({
                    top: item.element.offsetTop - 12,
                    behavior: "smooth"
                });
            }

            // Create utterance
            const textToSpeak = item.text;
            if (!textToSpeak) {
                // skip empty section
                speakSection(index + 1);
                return;
            }

            currentUtterance = new SpeechSynthesisUtterance(textToSpeak);
            
            // Speed
            currentUtterance.rate = parseFloat(ttsSpeedSelect.value || "1");

            // Language auto-detection
            const isChinese = /[\u4e00-\u9fa5]/.test(textToSpeak);
            currentUtterance.lang = isChinese ? "zh-CN" : "en-US";

            // Find matching system voice if available
            if (synth.getVoices) {
                const voices = synth.getVoices();
                const matchedVoice = voices.find(v => v.lang.startsWith(isChinese ? "zh" : "en"));
                if (matchedVoice) currentUtterance.voice = matchedVoice;
            }

            // Utterance events
            currentUtterance.onstart = () => {
                isSpeaking = true;
                isPaused = false;
                ttsPlayBtn.textContent = currentLang === "zh" ? "⏸ 暂停" : "⏸ Pause";
                ttsPlayBtn.setAttribute("data-zh", "⏸ 暂停");
                ttsPlayBtn.setAttribute("data-en", "⏸ Pause");
                ttsStopBtn.disabled = false;
            };

            currentUtterance.onend = () => {
                // Autoplay next section
                if (isSpeaking && !isPaused) {
                    speakSection(currentQueueIndex + 1);
                }
            };

            currentUtterance.onerror = (e) => {
                console.error("TTS play error:", e);
                if (e.error !== "interrupted" && e.error !== "canceled") {
                    speakSection(currentQueueIndex + 1);
                }
            };

            synth.speak(currentUtterance);
        }

        function stopReading() {
            if (synth) {
                synth.cancel();
            }
            isSpeaking = false;
            isPaused = false;
            currentQueueIndex = -1;
            currentUtterance = null;
            
            // Remove active highlights
            document.querySelectorAll(".report-section.reading-active").forEach(el => {
                el.classList.remove("reading-active");
            });

            // Update UI buttons
            ttsPlayBtn.textContent = currentLang === "zh" ? "▶ 播放" : "▶ Play";
            ttsPlayBtn.setAttribute("data-zh", "▶ 播放");
            ttsPlayBtn.setAttribute("data-en", "▶ Play");
            ttsStopBtn.disabled = true;
        }

        // Toggle play/pause
        ttsPlayBtn.addEventListener("click", () => {
            if (!synth) {
                alert(currentLang === "zh" ? "您的浏览器不支持语音朗读。" : "TTS not supported in your browser.");
                return;
            }

            if (isSpeaking) {
                if (isPaused) {
                    // Resume
                    synth.resume();
                    isPaused = false;
                    ttsPlayBtn.textContent = currentLang === "zh" ? "⏸ 暂停" : "⏸ Pause";
                    ttsPlayBtn.setAttribute("data-zh", "⏸ 暂停");
                    ttsPlayBtn.setAttribute("data-en", "⏸ Pause");
                } else {
                    // Pause
                    synth.pause();
                    isPaused = true;
                    ttsPlayBtn.textContent = currentLang === "zh" ? "▶ 继续" : "▶ Resume";
                    ttsPlayBtn.setAttribute("data-zh", "▶ 继续");
                    ttsPlayBtn.setAttribute("data-en", "▶ Resume");
                }
            } else {
                // Start fresh play
                populateQueue();
                if (playQueue.length > 0) {
                    speakSection(0);
                }
            }
        });

        ttsStopBtn.addEventListener("click", stopReading);

        // Adjust speed on-the-fly
        ttsSpeedSelect.addEventListener("change", () => {
            if (isSpeaking && currentUtterance) {
                // Need to restart speaking current section to apply speed change
                const savedIndex = currentQueueIndex;
                synth.cancel();
                setTimeout(() => {
                    speakSection(savedIndex);
                }, 100);
            }
        });

        // Initialize and apply
        applySettings();

        // Calculate time on first load if results exist
        setTimeout(calculateReadingTime, 1000);
    })();
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

    // Shared by mouse (mousemove) and touch (touchstart/touchmove) — on a
    // phone/tablet nothing ever fires "mousemove", so a mouse-only listener
    // makes the whole tooltip permanently unreachable via touch.
    function updateHover(clientX, pageX, pageY) {
        const rect = svg.getBoundingClientRect();
        const mouseX = clientX - rect.left - margin.left;
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

        const tooltipX = pageX + 15;
        const tooltipY = pageY - 40;
        tooltip.style.left = tooltipX + "px";
        tooltip.style.top = tooltipY + "px";
        tooltip.style.display = "block";
        tooltip.innerHTML = `
            <div><strong>${p.date}</strong></div>
            <div>Price: $${p.close.toFixed(2)}</div>
        `;
    }

    function hideHover() {
        hoverLine.style.display = "none";
        hoverDot.style.display = "none";
        tooltip.style.display = "none";
    }

    hoverRect.addEventListener("mousemove", (e) => updateHover(e.clientX, e.pageX, e.pageY));
    hoverRect.addEventListener("mouseleave", hideHover);

    // Touch: preventDefault so dragging a finger across the chart scrubs
    // the tooltip instead of scrolling the page (the chart itself never
    // needs to scroll — it's a fixed-size element). Requires {passive:false}
    // since browsers default touch listeners to passive, which silently
    // ignores preventDefault().
    hoverRect.addEventListener("touchstart", (e) => {
        if (e.touches.length !== 1) return;
        e.preventDefault();
        const t = e.touches[0];
        updateHover(t.clientX, t.pageX, t.pageY);
    }, { passive: false });
    hoverRect.addEventListener("touchmove", (e) => {
        if (e.touches.length !== 1) return;
        e.preventDefault();
        const t = e.touches[0];
        updateHover(t.clientX, t.pageX, t.pageY);
    }, { passive: false });
    hoverRect.addEventListener("touchend", hideHover);
    hoverRect.addEventListener("touchcancel", hideHover);
}

// ── History Watchlist — server-side, per-user (web/history.py) ──────
// Persisted by the backend the moment a job finishes, so it's the same
// regardless of which device or browser opens the History tab — unlike
// the old localStorage version, which only existed in whichever browser
// happened to be open when the run completed.
const RATING_DISPLAY = {
    "Strong Buy":  { zh: "买入+", en: "BUY+",  cls: "buy"  },
    "Buy":         { zh: "买入",  en: "BUY",   cls: "buy"  },
    "Overweight":  { zh: "增持",  en: "OW",    cls: "buy"  },
    "Hold":        { zh: "持有",  en: "HOLD",  cls: "hold" },
    "Underweight": { zh: "减持",  en: "UW",    cls: "hold" },
    "Sell":        { zh: "卖出",  en: "SELL",  cls: "sell" },
    "Strong Sell": { zh: "卖出+", en: "SELL+", cls: "sell" },
};

// Client-side star + search state — starred keys live in localStorage so
// the "important" flag persists across sessions independently of the
// server-side history record. Filter mode toggles between "all" and
// "starred-only" via the ☆/★ toolbar button.
const HISTORY_STARRED_KEY = "tradingagents.starred";
function _getStarred() {
    try { return new Set(JSON.parse(localStorage.getItem(HISTORY_STARRED_KEY) || "[]")); }
    catch (_) { return new Set(); }
}
function _setStarred(set) {
    localStorage.setItem(HISTORY_STARRED_KEY, JSON.stringify([...set]));
}
function _histKey(t, d) { return `${t}@${d}`; }

let _historyState = { items: [], query: "", starredOnly: false };

function _applyHistoryFilters() {
    const q = _historyState.query.trim().toLowerCase();
    const starred = _getStarred();
    return _historyState.items.filter(it => {
        if (_historyState.starredOnly && !starred.has(_histKey(it.ticker, it.trade_date))) return false;
        if (!q) return true;
        return (it.ticker || "").toLowerCase().includes(q)
            || (it.trade_date || "").includes(q)
            || (it.decision || "").toLowerCase().includes(q);
    });
}

async function renderHistoryList() {
    const listEl = document.getElementById("history-list");
    if (!listEl) return;
    try {
        const resp = await fetch("/api/history");
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        const { items } = await resp.json();
        _historyState.items = items || [];
        _rerenderHistory();
    } catch (e) {
        console.error("Failed to render history list", e);
    }
}

function _rerenderHistory() {
    const listEl = document.getElementById("history-list");
    if (!listEl) return;
    const filtered = _applyHistoryFilters();
    listEl.innerHTML = "";
    if (filtered.length === 0) {
        const msg = _historyState.query
            ? (currentLang === 'zh' ? '无匹配记录' : 'No matches')
            : (currentLang === 'zh' ? '无历史记录' : 'No history yet');
        listEl.innerHTML = `<div class="empty-state">${msg}</div>`;
        return;
    }
    const starred = _getStarred();
    filtered.forEach(item => {
        const row = document.createElement("div");
        row.className = "history-item";
        const isRunning = item.status === "running";
        const key = _histKey(item.ticker, item.trade_date);
        const isStarred = starred.has(key);
        let badgeText, badgeClass;
        if (isRunning) {
            row.classList.add("running");
            badgeText = currentLang === 'zh' ? "分析中" : "Running";
            badgeClass = "running";
        } else {
            const ratingText = item.decision || "Hold";
            const display = RATING_DISPLAY[ratingText];
            badgeText = display ? display[currentLang] : ratingText;
            badgeClass = display ? display.cls : "hold";
        }

        row.innerHTML = `
            <button class="hist-star ${isStarred ? 'on' : ''}" type="button" title="${currentLang==='zh'?'星标':'Star'}">${isStarred ? '★' : '☆'}</button>
            <div class="hist-meta">
                <span class="hist-sym">${escapeHtml(item.ticker)}</span>
                <span class="hist-date">${escapeHtml(item.trade_date)}</span>
            </div>
            <span class="hist-badge ${badgeClass}">
                ${isRunning ? '<span class="hist-spinner"></span>' : ''}${badgeText}
            </span>
        `;

        // Star button — stopPropagation so it doesn't also open the report.
        row.querySelector(".hist-star").addEventListener("click", (e) => {
            e.stopPropagation();
            const s = _getStarred();
            if (s.has(key)) s.delete(key); else s.add(key);
            _setStarred(s);
            _rerenderHistory();
        });

        row.addEventListener("click", () => {
            if (isRunning) {
                if (typeof window.__taSwitchMobileTab === "function") {
                    window.__taSwitchMobileTab("analyze");
                }
                const loadingView = document.getElementById("loading-view");
                if (loadingView) {
                    document.querySelectorAll(".center-view").forEach(v => v.classList.remove("active"));
                    const results = document.getElementById("results-view");
                    if (results) results.classList.remove("active");
                    loadingView.classList.add("active");
                }
                return;
            }
            loadHistoryItem(item.ticker, item.trade_date);
            if (typeof window.__taSwitchMobileTab === "function") {
                window.__taSwitchMobileTab("analyze");
            }
        });
        listEl.appendChild(row);
    });
}

// Escape helper — lifted from inside the DOMContentLoaded closure so
// _rerenderHistory (defined at module scope) can use it too.
if (typeof escapeHtml !== "function") {
    // eslint-disable-next-line no-var
    var escapeHtml = function (s) {
        const d = document.createElement("div");
        d.textContent = s == null ? "" : String(s);
        return d.innerHTML;
    };
}

async function loadHistoryItem(ticker, date) {
    try {
        const resp = await fetch(`/api/history/${encodeURIComponent(ticker)}/${encodeURIComponent(date)}`);
        if (!resp.ok) {
            alert(currentLang === 'zh' ? "未找到该历史报告的数据" : "Report data not found.");
            return;
        }
        const data = await resp.json();
        if (typeof window.__taDisplayResults === "function") {
            window.__taDisplayResults(data);
        } else {
            console.error("displayResults not ready yet; is DOMContentLoaded fired?");
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

    function syncActiveSection() {
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

        // Let the mobile report-nav pill reflect where the reader is now
        // (the "定位" — always-visible current-section indicator).
        if (typeof window.__taOnSectionChange === "function") {
            window.__taOnSectionChange(activeId);
        }
    }

    scroller.addEventListener("scroll", syncActiveSection);
    // Exposed so displayResults() can re-sync once the report is actually
    // visible — running it while #results-view is still display:none gives
    // every section offsetTop 0, which wrongly selects the last one.
    window.__taSyncActiveSection = syncActiveSection;
    syncActiveSection(); // best-effort initial (corrected on first display)

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

// ── Report navigation ────────────────────────────────────────────────
(function initReportNav() {
    const panel = document.getElementById('toc-sidebar');
    if (!panel) return;

    const TABLET_BP = 1024;
    const isMobile = () => window.innerWidth <= TABLET_BP;

    // ── Mobile: floating pill + backdrop (created once; hidden on desktop via CSS). ──
    const fab = document.createElement('button');
    fab.className = 'toc-fab';
    fab.id = 'toc-fab';
    fab.type = 'button';
    fab.innerHTML = '<span class="fab-icon">☰</span>'
        + '<span class="fab-label" data-zh="报告导航" data-en="Contents">报告导航</span>';
    document.body.appendChild(fab);

    const backdrop = document.createElement('div');
    backdrop.className = 'toc-backdrop';
    backdrop.id = 'toc-backdrop';
    document.body.appendChild(backdrop);

    const appContainer = document.querySelector('.app-container');
    const resultsView = document.getElementById('results-view');

    function sheetIsOpen() { return panel.classList.contains('sheet-open'); }

    function updateFabVisibility() {
        const tab = appContainer ? appContainer.getAttribute('data-mobile-tab') : null;
        const onAnalyze = !tab || tab === 'analyze';
        const show = isMobile() && !sheetIsOpen() && onAnalyze
            && resultsView && resultsView.classList.contains('active');
        fab.classList.toggle('available', !!show);
    }

    function openSheet() {
        panel.classList.remove('collapsed');
        panel.classList.add('sheet-open');
        backdrop.classList.add('visible');
        updateFabVisibility();
    }
    function closeSheet() {
        panel.classList.remove('sheet-open');
        backdrop.classList.remove('visible');
        updateFabVisibility();
    }

    fab.addEventListener('click', openSheet);
    backdrop.addEventListener('click', closeSheet);
    
    // Tapping any nav link closes the sheet
    const navEl = panel.querySelector('.toc-nav');
    if (navEl) {
        navEl.addEventListener('click', (e) => {
            if (isMobile() && e.target.closest('.toc-link')) closeSheet();
        });
    }

    // Keep the pill label in sync with the current section
    window.__taOnSectionChange = (activeId) => {
        const label = fab.querySelector('.fab-label');
        if (!label) return;
        const link = panel.querySelector(`.toc-link[href="#${activeId}"]`);
        const name = link ? link.querySelector('.toc-name') : null;
        if (name) label.textContent = name.textContent;
    };

    if (resultsView) {
        new MutationObserver(updateFabVisibility)
            .observe(resultsView, { attributes: true, attributeFilter: ['class'] });
    }
    if (appContainer) {
        new MutationObserver(updateFabVisibility)
            .observe(appContainer, { attributes: true, attributeFilter: ['data-mobile-tab'] });
    }
    updateFabVisibility();

    let wasMobile = isMobile();
    window.addEventListener('resize', () => {
        const nowMobile = isMobile();
        if (wasMobile !== nowMobile) {
            wasMobile = nowMobile;
            if (nowMobile) {
                const toggleBtn = document.getElementById('toc-collapse-btn');
                if (toggleBtn) toggleBtn.textContent = '×';
            } else {
                closeSheet();
                panel.style.left = '';
                panel.style.top = '';
                panel.style.right = '';
            }
        }
        updateFabVisibility();
    });
})();

// ── Reading font-size control ────────────────────────────────────────
// Binds the A− / A / A+ / A++ chip in the sidebar header to
// --reading-scale on <html>, which all .markdown-content sizing keys
// off. Persisted in localStorage so the choice sticks across sessions.
(function initReadingScale() {
    const KEY = 'tradingagents.readingScale';
    // Scoped to .reading-controls: .rc-btn is shared with the screen-mode
    // buttons below, which don't carry data-scale — an unscoped query here
    // would parseFloat(undefined) -> NaN for those and forcibly clear their
    // active highlight every time a font-size button is clicked.
    const btns = document.querySelectorAll('.reading-controls .rc-btn');
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

// ── Screen Mode Control ──────────────────────────────────────────────
// Toggles between 'default', 'flat-led', and 'curved-led' screen modes.
// Persisted in localStorage.
(function initScreenMode() {
    const KEY = 'tradingagents.screenMode';
    const btns = document.querySelectorAll('.screen-controls .rc-btn');
    if (!btns.length) return;

    const saved = localStorage.getItem(KEY) || 'default';
    apply(saved);

    btns.forEach(btn => {
        btn.addEventListener('click', () => {
            const mode = btn.dataset.mode;
            if (!mode) return;
            apply(mode);
            try { localStorage.setItem(KEY, mode); } catch (e) { /* ignore */ }
        });
    });

    function apply(mode) {
        document.documentElement.classList.remove('screen-flat-led', 'screen-curved-led');
        
        if (mode === 'flat-led') {
            document.documentElement.classList.add('screen-flat-led');
        } else if (mode === 'curved-led') {
            document.documentElement.classList.add('screen-flat-led', 'screen-curved-led');
        }
        
        btns.forEach(b => {
            b.setAttribute('aria-current', String(b.dataset.mode === mode));
        });
    }
})();


// ── Mobile / Tablet Responsive UI ────────────────────────────────────────────
// Injects a compact topbar and a persistent bottom tab bar (Analyze /
// History / Profile) for small screens — a native-app-style navigation
// model rather than a hamburger-triggered slide-over drawer. Which
// section is visible is driven entirely by style.css via the
// .app-container[data-mobile-tab="..."] rules; this IIFE just owns the
// tab bar's markup and the attribute it writes.
(function initMobileUI() {

    const TABLET_BP = 1024;   // px

    function isTablet()  { return window.innerWidth <= TABLET_BP; }

    if (!isTablet()) return;   // desktop: nothing to do

    const appContainer = document.querySelector('.app-container');
    const mainContent = document.querySelector('.main-content');
    if (!appContainer || !mainContent) return;

    // ── 1. Inject mobile topbar ─────────────────────────────────────
    const topbar = document.createElement('div');
    topbar.className = 'mobile-topbar';
    topbar.style.display = 'flex';   // always show on tablet/mobile (JS only runs when isTablet())
    topbar.innerHTML = `
      <span class="logo-icon" style="color:var(--accent-pink);font-size:13px">■</span>
      <span class="logo-text">TRADING AGENTS</span>
      <span class="topbar-ticker" id="mob-ticker"></span>
    `;
    mainContent.insertBefore(topbar, mainContent.firstChild);

    // ── 1b. Relocate the ticker/date form so it reads top-to-bottom as
    // [brand topbar] → [form] → [welcome/loading/results] on the Analyze
    // tab, instead of the form's original sidebar position (before the
    // topbar in source order, which on mobile would render it above the
    // brand bar). Restored to its original spot if the viewport is
    // widened back past the tablet breakpoint.
    const configForm = document.getElementById('config-form');
    const formHome = configForm ? { parent: configForm.parentNode, next: configForm.nextSibling } : null;
    function placeConfigForm() {
        if (!configForm || !formHome) return;
        if (isTablet()) {
            if (configForm.previousElementSibling !== topbar) {
                mainContent.insertBefore(configForm, topbar.nextSibling);
            }
        } else if (configForm.parentNode !== formHome.parent) {
            formHome.parent.insertBefore(configForm, formHome.next);
        }
    }
    placeConfigForm();

    // ── 2. Bottom tab bar: Analyze / History / Profile ──────────────
    const TABS = [
        { key: 'analyze', icon: '▰', zh: '分析', en: 'Analyze' },
        { key: 'history', icon: '▤', zh: '历史', en: 'History' },
        { key: 'profile', icon: '◆', zh: '我的', en: 'Profile' },
    ];
    const tabbar = document.createElement('nav');
    tabbar.className = 'mobile-tabbar';
    tabbar.style.display = 'flex';   // always show on tablet/mobile (JS only runs when isTablet())
    tabbar.innerHTML = TABS.map((t, i) => `
      <button class="tab-btn-mobile${i === 0 ? ' active' : ''}" data-tab="${t.key}" type="button">
        <span class="tab-icon">${t.icon}</span>
        <span class="tab-label" data-zh="${t.zh}" data-en="${t.en}">${t.zh}</span>
      </button>
    `).join('');
    document.body.appendChild(tabbar);

    function setActiveTab(key) {
        appContainer.setAttribute('data-mobile-tab', key);
        tabbar.querySelectorAll('.tab-btn-mobile').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.tab === key);
        });
        // History is server-side and shared across devices now — refresh
        // on every visit rather than only once at page load, so a run
        // finished on another device shows up here too.
        if (key === 'history' && typeof renderHistoryList === 'function') {
            renderHistoryList();
        }
    }
    tabbar.querySelectorAll('.tab-btn-mobile').forEach(btn => {
        btn.addEventListener('click', () => setActiveTab(btn.dataset.tab));
    });
    setActiveTab('analyze');

    // Exposed so loadHistoryItem() can jump back to the Analyze tab after
    // loading a past report from the History tab — otherwise the loaded
    // result would render behind the still-active History screen.
    window.__taSwitchMobileTab = setActiveTab;

    // ── 3. Sync topbar ticker text with main form ───────────────────
    const tickerInput = document.getElementById('ticker');
    const mobTicker   = document.getElementById('mob-ticker');
    if (tickerInput && mobTicker) {
        function updateMobTicker() {
            const v = tickerInput.value.trim();
            mobTicker.textContent = v ? v : '';
        }
        tickerInput.addEventListener('input', updateMobTicker);
        updateMobTicker();
    }

    // ── 4. Resize handler: keep topbar/tabbar in sync with viewport ──
    // topbar/tabbar visibility is driven by an inline style set once
    // above (needed since isTablet() is only known at that moment) —
    // inline styles always beat the stylesheet's display:none, so
    // without this they'd stay visible forever once shown, even after
    // the window is later widened past the tablet breakpoint (e.g.
    // exiting Split View, dragging to an external display).
    window.addEventListener('resize', () => {
        if (!isTablet()) {
            topbar.style.display = 'none';
            tabbar.style.display = 'none';
        } else {
            topbar.style.display = 'flex';
            tabbar.style.display = 'flex';
        }
        placeConfigForm();
    });

})();

// ══════════════════════════════════════════════════════════════════════
//  Confidence gauge, price chart, theme, notifications, hotkeys, PWA.
//  All module-level so any handler can call them.
// ══════════════════════════════════════════════════════════════════════

// ---- Confidence 0-100 gauge -----------------------------------------
function renderConfidence(confidence) {
    const box = document.getElementById("confidence-gauge");
    if (!box) return;
    if (!confidence || typeof confidence.score !== "number") {
        box.hidden = true;
        return;
    }
    box.hidden = false;
    const score = Math.max(0, Math.min(100, confidence.score));
    const circumference = 163.36;          // 2π × 26 (matches SVG)
    const offset = circumference * (1 - score / 100);
    const fill = document.getElementById("conf-ring-fill");
    if (fill) fill.setAttribute("stroke-dashoffset", offset.toFixed(2));
    document.getElementById("conf-ring-num").textContent = score;

    box.dataset.band = confidence.band || "medium";
    const tag = document.getElementById("conf-tag");
    const BAND_LABELS = {
        high:   { zh: "高置信度", en: "High confidence" },
        medium: { zh: "中置信度", en: "Medium confidence" },
        low:    { zh: "低置信度", en: "Low confidence" },
    };
    const b = BAND_LABELS[confidence.band] || BAND_LABELS.medium;
    tag.textContent = b[currentLang] || b.zh;

    // Tooltip breakdown: hover to see per-component score.
    const c = confidence.components || {};
    const detail = document.getElementById("conf-detail");
    if (detail) {
        detail.textContent = currentLang === 'zh'
            ? `辩论深度 ${c.debate_depth||0}·多空平衡 ${c.debate_balance||0}·轮次 ${c.debate_rounds||0}·一致性 ${c.signal_consistency||0}·计划 ${c.plan_completeness||0}`
            : `depth ${c.debate_depth||0}·balance ${c.debate_balance||0}·rounds ${c.debate_rounds||0}·consistency ${c.signal_consistency||0}·plan ${c.plan_completeness||0}`;
    }
}

// ---- Price chart (Lightweight Charts) --------------------------------
let _chartApi = null, _chartSeries = null, _chartCtx = null, _lastCandles = null;
function renderPriceChart(ctx) {
    _chartCtx = ctx;
    const panel = document.getElementById("chart-panel");
    if (!panel || !ctx || !ctx.ticker || !ctx.date) return;
    panel.hidden = false;
    document.getElementById("chart-symbol").textContent = ctx.ticker;
    document.getElementById("legend-entry").textContent  = ctx.entry  != null ? ctx.entry.toFixed(2)  : '--';
    document.getElementById("legend-stop").textContent   = ctx.stop   != null ? ctx.stop.toFixed(2)   : '--';
    document.getElementById("legend-target").textContent = ctx.target != null ? ctx.target.toFixed(2) : '--';

    const active = document.querySelector('.chart-range-btn.active');
    _loadChart(active ? active.dataset.range : '3M');
}

async function _loadChart(range) {
    const container = document.getElementById("chart-container");
    const status = document.getElementById("chart-status");
    if (!container || !_chartCtx) return;
    if (typeof LightweightCharts === "undefined") {
        status.textContent = currentLang === 'zh' ? "图表库未加载（请检查网络）" : "Chart library not loaded";
        return;
    }
    status.textContent = currentLang === 'zh' ? "加载中…" : "loading…";

    try {
        const resp = await fetch(`/api/price/${encodeURIComponent(_chartCtx.ticker)}?date=${encodeURIComponent(_chartCtx.date)}&range=${range}`);
        const data = await resp.json();
        if (data.status !== "success" || !data.candles || data.candles.length === 0) {
            status.textContent = currentLang === 'zh' ? `无 ${_chartCtx.ticker} 行情数据` : `No price data for ${_chartCtx.ticker}`;
            container.innerHTML = "";
            _lastCandles = null;
            return;
        }
        _lastCandles = data.candles;
        _drawChart(container, data.candles);
        status.textContent = `${data.candles.length} bars · ${range}`;
    } catch (e) {
        console.error("chart load failed", e);
        status.textContent = currentLang === 'zh' ? "加载失败" : "load failed";
    }
}

function _drawChart(container, candles) {
    // Recreate on every draw so range / theme switches take effect cleanly.
    container.innerHTML = "";
    const isLight = document.body.classList.contains("theme-light");
    _chartApi = LightweightCharts.createChart(container, {
        width: container.clientWidth,
        height: 320,
        layout: {
            background: { type: 'solid', color: isLight ? '#ffffff' : '#0d1117' },
            textColor:  isLight ? '#1f2937' : '#c7cdd5',
            fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
        },
        grid: {
            vertLines: { color: isLight ? '#eef1f5' : '#212830' },
            horzLines: { color: isLight ? '#eef1f5' : '#212830' },
        },
        rightPriceScale: { borderColor: isLight ? '#d1d5db' : '#30363d' },
        timeScale: {
            borderColor: isLight ? '#d1d5db' : '#30363d',
            timeVisible: true, secondsVisible: false,
        },
        crosshair: { mode: 1 },
    });
    _chartSeries = _chartApi.addCandlestickSeries({
        upColor:   '#30D158', downColor: '#FF453A',
        wickUpColor: '#30D158', wickDownColor: '#FF453A',
        borderVisible: false,
    });
    _chartSeries.setData(candles);

    // Trade-date vertical marker via price-line workaround: add markers on
    // the actual bar so the trader can see "this is the day we analyzed".
    const dateBar = candles.find(c => c.time === _chartCtx.date)
                 || candles[candles.length - 1];
    if (dateBar) {
        _chartSeries.setMarkers([{
            time: dateBar.time, position: 'aboveBar', color: '#4f8cff',
            shape: 'arrowDown',
            text: currentLang === 'zh' ? '分析基准日' : 'Analysis date',
        }]);
    }

    // Entry / Stop / Target overlays.
    if (_chartCtx.entry != null) {
        _chartSeries.createPriceLine({
            price: _chartCtx.entry, color: '#4f8cff', lineWidth: 2, lineStyle: 0,
            axisLabelVisible: true, title: currentLang === 'zh' ? '入场' : 'Entry',
        });
    }
    if (_chartCtx.stop != null) {
        _chartSeries.createPriceLine({
            price: _chartCtx.stop, color: '#FF453A', lineWidth: 2, lineStyle: 2,
            axisLabelVisible: true, title: currentLang === 'zh' ? '止损' : 'Stop',
        });
    }
    if (_chartCtx.target != null) {
        _chartSeries.createPriceLine({
            price: _chartCtx.target, color: '#30D158', lineWidth: 2, lineStyle: 2,
            axisLabelVisible: true, title: currentLang === 'zh' ? '目标' : 'Target',
        });
    }

    _chartApi.timeScale().fitContent();

    // Resize on window changes.
    const ro = new ResizeObserver(() => {
        if (_chartApi) _chartApi.applyOptions({ width: container.clientWidth });
    });
    ro.observe(container);
}

// Range buttons.
document.addEventListener("click", (e) => {
    const b = e.target.closest(".chart-range-btn");
    if (!b) return;
    document.querySelectorAll(".chart-range-btn").forEach(x => x.classList.remove("active"));
    b.classList.add("active");
    if (_chartCtx) _loadChart(b.dataset.range);
});

// ---- Theme switcher --------------------------------------------------
const THEME_KEY = "tradingagents.theme";
function applyTheme(name) {
    document.body.classList.toggle("theme-light", name === "light");
    document.querySelectorAll('[data-theme]').forEach(b => {
        b.setAttribute("aria-current", b.dataset.theme === name ? "true" : "false");
    });

    // Update custom header settings panel theme indicator icons
    const darkIcon = document.getElementById("theme-dark-icon");
    const lightIcon = document.getElementById("theme-light-icon");
    if (darkIcon && lightIcon) {
        darkIcon.classList.toggle("active", name === "dark");
        lightIcon.classList.toggle("active", name === "light");
    }

    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", name === "light" ? "#f5f7fa" : "#080A0C");
    // If a chart is currently on-screen, redraw it in the new theme.
    // We only need to re-hit the API if _lastCandles is unavailable — the
    // cached candles from the last successful load let us reskin instantly.
    if (_chartCtx && _lastCandles) {
        const container = document.getElementById("chart-container");
        if (container) _drawChart(container, _lastCandles);
    }
    localStorage.setItem(THEME_KEY, name);
}
document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-theme]");
    if (b) applyTheme(b.dataset.theme);
});

// Bind pull-chain interaction
document.addEventListener("DOMContentLoaded", () => {
    const pullChain = document.getElementById("theme-pull-chain");
    if (pullChain) {
        pullChain.addEventListener("click", () => {
            // Prevent double triggers during animation
            if (pullChain.classList.contains("animating")) return;

            pullChain.classList.add("animating");
            setTimeout(() => {
                pullChain.classList.remove("animating");
            }, 600);

            // Play light switch click sound context or simply flip theme
            const currentTheme = localStorage.getItem("tradingagents.theme") || "dark";
            const nextTheme = currentTheme === "dark" ? "light" : "dark";
            applyTheme(nextTheme);
        });
    }
});

// ---- Desktop notifications ------------------------------------------
const NOTIF_KEY = "tradingagents.notifEnabled";
function notifEnabled() { return localStorage.getItem(NOTIF_KEY) === "1"; }
async function toggleNotifications() {
    if (!("Notification" in window)) {
        alert(currentLang === 'zh' ? "浏览器不支持桌面通知" : "This browser doesn't support notifications");
        return;
    }
    if (notifEnabled()) {
        localStorage.setItem(NOTIF_KEY, "0");
        _updateNotifButton();
        return;
    }
    let perm = Notification.permission;
    if (perm !== "granted") perm = await Notification.requestPermission();
    if (perm === "granted") {
        localStorage.setItem(NOTIF_KEY, "1");
        _updateNotifButton();
        new Notification("TradingAgents", {
            body: currentLang === 'zh' ? "通知已开启：分析完成时会提醒你。" : "Notifications on. You'll get pinged when analyses finish.",
            icon: "/icon.svg",
        });
    }
}
function _updateNotifButton() {
    const btn = document.getElementById("notif-toggle-btn");
    if (btn) btn.textContent = notifEnabled() ? "🔔" : "🔕";
}
function notifyIfEnabled(title, body) {
    if (!notifEnabled() || !("Notification" in window)) return;
    if (document.visibilityState === "visible") return;  // don't spam if user is already looking
    try { new Notification(title, { body, icon: "/icon.svg" }); }
    catch (e) { /* Safari quirks */ }
}
window.__taNotify = notifyIfEnabled;
document.addEventListener("click", (e) => {
    if (e.target.closest("#notif-toggle-btn")) toggleNotifications();
});

// ---- Analysis-depth preset ------------------------------------------
// Each preset writes into the (still-existing) advanced fields so the
// backend contract is unchanged and users can override in "Advanced".
const DEPTH_PRESETS = {
    fast:     { rounds: 1, deep: "deepseek-chat",     quick: "deepseek-chat" },
    balanced: { rounds: 1, deep: "deepseek-reasoner", quick: "deepseek-chat" },
    deep:     { rounds: 3, deep: "deepseek-reasoner", quick: "deepseek-chat" },
};
function applyDepthPreset(name) {
    const p = DEPTH_PRESETS[name] || DEPTH_PRESETS.balanced;
    const r = document.getElementById("max_debate_rounds");
    const d = document.getElementById("deep_think_llm");
    const q = document.getElementById("quick_think_llm");
    if (r) r.value = p.rounds;
    if (d) d.value = p.deep;
    if (q) q.value = p.quick;
    document.querySelectorAll(".depth-btn").forEach(b => b.classList.toggle("active", b.dataset.depth === name));
    localStorage.setItem("tradingagents.depth", name);
}
document.addEventListener("click", (e) => {
    const b = e.target.closest(".depth-btn");
    if (b) applyDepthPreset(b.dataset.depth);
});

// ---- Global keyboard shortcuts --------------------------------------
document.addEventListener("keydown", (e) => {
    const active = document.activeElement;
    const inField = active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA" || active.isContentEditable);

    // Esc — close any open modal / dismiss suggest list.
    if (e.key === "Escape") {
        const modal = document.getElementById("disclaimer-modal");
        // Only auto-close if user has already accepted (avoid bypassing the gate).
        const list = document.getElementById("ticker-suggest-list");
        if (list && !list.hidden) { list.hidden = true; return; }
        const details = document.querySelectorAll("details[open]");
        if (details.length) { /* leave open */ }
    }

    // "/" — focus search when not typing elsewhere.
    if (e.key === "/" && !inField && !e.metaKey && !e.ctrlKey) {
        e.preventDefault();
        const s = document.getElementById("history-search");
        if (s) s.focus();
    }

    // Cmd/Ctrl + Enter — submit the analyze form from anywhere.
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
        const form = document.getElementById("config-form");
        if (form) {
            e.preventDefault();
            form.requestSubmit ? form.requestSubmit() : form.submit();
        }
    }
});

// ---- History search wiring ------------------------------------------
document.addEventListener("input", (e) => {
    if (e.target && e.target.id === "history-search") {
        _historyState.query = e.target.value;
        _rerenderHistory();
    }
});
document.addEventListener("click", (e) => {
    const btn = e.target.closest("#history-filter-star");
    if (!btn) return;
    _historyState.starredOnly = !_historyState.starredOnly;
    btn.textContent = _historyState.starredOnly ? "★" : "☆";
    btn.classList.toggle("on", _historyState.starredOnly);
    _rerenderHistory();
});

// ---- PWA service worker registration --------------------------------
if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
        navigator.serviceWorker.register("/sw.js").catch(() => { /* ignore */ });
    });
}

// ---- Boot: restore preferences --------------------------------------
document.addEventListener("DOMContentLoaded", () => {
    // Theme
    applyTheme(localStorage.getItem(THEME_KEY) || "dark");
    // Depth preset
    applyDepthPreset(localStorage.getItem("tradingagents.depth") || "balanced");
    // Notif button icon
    _updateNotifButton();
});
