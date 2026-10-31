// cozy Video tab: pick a video, describe the frames to keep, review the
// matching segments the server found, export one trimmed video.
//
// Loaded after index.html's inline script and relies on its globals: `root`
// (URL prefix), `openBrowser` (the shared local/remote browse modal) and
// `KNOWN_HOSTS`. All job state lives on the server (/api/video/status), so a
// reload -- or another browser -- picks up exactly where the job is.
(function () {
    const $ = (id) => document.getElementById(id);
    const sel = $("v-select");
    const uploadBtn = $("v-upload-btn");
    const uploadInput = $("v-upload-input");
    const remoteBtn = $("v-remote-btn");
    const sourceLabel = $("v-source-label");
    const preview = $("v-preview");
    const promptEl = $("v-prompt");
    const analyzeBtn = $("v-analyze");
    const estimateEl = $("v-estimate");
    const modelSel = $("v-model");
    const cancelBtn = $("v-cancel");
    const progress = $("v-progress");
    const bar = $("v-bar");
    const statusEl = $("v-status");
    const errorEl = $("v-error");
    const review = $("v-review");
    const timeline = $("v-timeline");
    const segList = $("v-segments");
    const totalEl = $("v-total");
    const basenameEl = $("v-basename");
    const exportBtn = $("v-export");
    const resultWrap = $("v-result");
    const resultPlayer = $("v-result-player");
    const savedEl = $("v-saved");
    const clearBtn = $("v-clear");
    const flushBtn = $("v-flush");

    const RUNNING = ["staging", "probing", "analyzing", "exporting"];

    // The chosen source: {value} for a local picker entry, {host, path} for
    // a remote one, or null.
    let source = null;
    let remoteSrc = { host: "", path: "" };   // where the browser reopens
    let job = null;                           // last /status payload
    let keep = [];                            // client-side keep flags
    let keepFor = null;                       // job.started_at they belong to
    let renderedFor = null;                   // review render signature
    let poll = null;
    let tabShown = false;

    // Two error sources share one box: a client-side complaint (no video
    // chosen, upload refused) and the job's own error from the server. The
    // client one wins until the next action clears it, so a 1 s status poll
    // never wipes a message the user has not read yet.
    let clientError = "";
    function paintError() {
        const serverError = job && job.error && job.state !== "analyzing" ? job.error : "";
        const msg = clientError || serverError;
        errorEl.textContent = msg;
        errorEl.style.display = msg ? "block" : "none";
    }
    function showError(msg) { clientError = msg || ""; paintError(); }

    // Whether the picker still shows the current job's video: only then may
    // the job's file replace the preview.
    function sameAsJob() {
        const src = job && job.source;
        if (!src || !source) return !source;
        return src.value ? src.value === source.value
            : src.host === source.host && src.path === source.path;
    }

    function fmt(secs) {
        if (secs == null || !isFinite(secs)) return "";
        const m = Math.floor(secs / 60);
        const s = secs - m * 60;
        return m + ":" + (s < 10 ? "0" : "") + s.toFixed(1);
    }

    function fmtEta(secs) {
        if (secs == null) return "";
        if (secs < 60) return Math.round(secs) + " s";
        const m = Math.round(secs / 60);
        return m < 60 ? m + " min" : Math.floor(m / 60) + " h " + (m % 60) + " min";
    }

    // --- cost estimate -------------------------------------------------------

    // Shown before Analyze so a long video's cost is a choice, not a
    // surprise. The server does the arithmetic with the same code the search
    // uses, plus the per-frame speed it measured on the last run.
    let estimateTimer = null;
    let estimateSeq = 0;

    function setEstimate(text) {
        estimateEl.textContent = text || "";
        estimateEl.style.display = text ? "block" : "none";
    }

    function scheduleEstimate() {
        clearTimeout(estimateTimer);
        estimateTimer = setTimeout(refreshEstimate, 300);
    }

    async function refreshEstimate() {
        if (!source) { setEstimate(""); return; }
        if (!source.value) {
            setEstimate("Frame count is known once the remote video has been copied here.");
            return;
        }
        const q = new URLSearchParams({ video: source.value });
        const stride = $("v-stride").value.trim();
        const precision = $("v-precision").value.trim();
        if (stride) q.set("stride", stride);
        if (precision) q.set("precision", precision);
        if (modelSel.value) q.set("model", modelSel.value);
        const seq = ++estimateSeq;
        let d;
        try {
            const r = await fetch(root + "api/video/estimate?" + q);
            d = await r.json();
            if (seq !== estimateSeq) return;    // a newer request superseded this
            if (!r.ok) { setEstimate(d.error ? "Cannot estimate: " + d.error : ""); return; }
        } catch (e) { return; }
        let text = "~" + d.frames + " frames: every " + d.stride.toFixed(1).replace(/\.0$/, "") +
            " s, then edges to " + d.precision + " s";
        text += d.eta_s != null
            ? " · about " + fmtEta(d.eta_s) + " at " + d.secs_per_frame.toFixed(1) + " s/frame"
            : " · time unknown until a first run measures this model";
        setEstimate(text + ". Each extra matching segment adds a few frames.");
    }

    // --- model choice ----------------------------------------------------------

    // Every installed Ollama model (what `anix-llm models` lists). Only ones
    // that accept images can be picked; the rest are shown disabled so a
    // missing model is visibly "installed but blind", not just absent.
    let wantModel = null;    // a model to select once the list arrives

    async function loadModels() {
        let d;
        try {
            const r = await fetch(root + "api/video/models");
            d = await r.json();
            if (!r.ok) throw new Error(d.error || "HTTP " + r.status);
        } catch (e) {
            modelSel.innerHTML = "";
            const o = document.createElement("option");
            o.value = "";
            o.textContent = "default model (list unavailable: " + e.message + ")";
            modelSel.appendChild(o);
            return;
        }
        const keep = wantModel || modelSel.value;
        modelSel.innerHTML = "";
        let fallback = "";
        for (const m of d.models) {
            const o = document.createElement("option");
            o.value = m.name;
            o.disabled = !m.vision;
            let label = m.name;
            if (m.default) { label += " (default)"; fallback = m.name; }
            if (!m.vision) label += " — no vision";
            else if (m.secs_per_frame != null) label += " — " + m.secs_per_frame.toFixed(1) + " s/frame";
            o.textContent = label;
            modelSel.appendChild(o);
        }
        const usable = [...modelSel.options].filter(o => !o.disabled).map(o => o.value);
        modelSel.value = usable.includes(keep) ? keep
            : usable.includes(fallback) ? fallback : (usable[0] || "");
        wantModel = null;
    }

    modelSel.addEventListener("change", scheduleEstimate);

    $("v-stride").addEventListener("input", scheduleEstimate);
    $("v-precision").addEventListener("input", scheduleEstimate);

    // --- source picking ----------------------------------------------------

    async function loadList(selectValue) {
        let d;
        try {
            const r = await fetch(root + "api/video/list");
            d = await r.json();
        } catch (e) { showError("cannot list videos: " + e.message); return; }
        sel.innerHTML = "";
        const none = document.createElement("option");
        none.value = "";
        none.textContent = "— choose a video —";
        sel.appendChild(none);
        const groups = { input: "Input", output: "Output" };
        for (const [key, label] of Object.entries(groups)) {
            const items = (d.videos || []).filter(v => v.source === key);
            if (!items.length) continue;
            const og = document.createElement("optgroup");
            og.label = label;
            for (const v of items) {
                const o = document.createElement("option");
                o.value = v.value;
                o.textContent = v.label;
                og.appendChild(o);
            }
            sel.appendChild(og);
        }
        if (selectValue !== undefined) {
            sel.value = selectValue;
            if (sel.value === selectValue && selectValue) pickLocal(selectValue);
        }
    }

    function setPreview(url) {
        if (!url) {
            preview.removeAttribute("src");
            preview.style.display = "none";
            return;
        }
        if (preview.getAttribute("src") !== url) preview.src = url;
        preview.style.display = "block";
    }

    function pickLocal(value) {
        source = value ? { value: value } : null;
        sourceLabel.style.display = "none";
        setPreview(value ? root + "api/video/file?name=" + encodeURIComponent(value) : null);
        scheduleEstimate();
    }

    function pickRemote(host, path) {
        source = { host: host, path: path };
        remoteSrc = { host: host, path: path.replace(/\/[^/]*$/, "") || "/" };
        sel.value = "";
        sourceLabel.textContent = "Remote: " + (host || "local") + ":" + path +
            " (copied here when analysis starts)";
        sourceLabel.style.display = "block";
        setPreview(null);
        scheduleEstimate();
    }

    sel.addEventListener("change", () => pickLocal(sel.value));

    uploadBtn.addEventListener("click", () => uploadInput.click());
    uploadInput.addEventListener("change", () => {
        const f = uploadInput.files[0];
        uploadInput.value = "";
        if (!f) return;
        // XHR rather than fetch: only XHR reports upload progress, and a
        // phone video can take minutes to send.
        const xhr = new XMLHttpRequest();
        const fd = new FormData();
        fd.append("file", f);
        showError("");
        progress.style.display = "block";
        bar.style.width = "0%";
        statusEl.textContent = "Uploading " + f.name + "…";
        xhr.upload.onprogress = (e) => {
            if (e.lengthComputable) bar.style.width = (100 * e.loaded / e.total) + "%";
        };
        xhr.onloadend = () => {
            progress.style.display = "none";
            statusEl.textContent = "";
            let d = {};
            try { d = JSON.parse(xhr.responseText); } catch (e) { /* non-JSON */ }
            if (xhr.status !== 200) {
                showError(d.error || "upload failed (HTTP " + xhr.status + ")");
                return;
            }
            loadList(d.value);
        };
        xhr.open("POST", root + "api/video/upload");
        xhr.send(fd);
    });

    remoteBtn.addEventListener("click", () => {
        openBrowser(remoteSrc.host || (KNOWN_HOSTS[0] || ""), "video",
                    remoteSrc.path, (host, path) => pickRemote(host, path),
                    "", showError);
    });

    // --- analysis ------------------------------------------------------------

    function numOrNull(id) {
        const v = $(id).value.trim();
        return v === "" ? null : parseFloat(v);
    }

    analyzeBtn.addEventListener("click", async () => {
        showError("");
        if (!source) { showError("Choose a video first."); return; }
        const prompt = promptEl.value.trim();
        if (!prompt) { showError("Describe the frames to keep."); return; }
        const body = { prompt: prompt, model: modelSel.value || null,
                       stride: numOrNull("v-stride"),
                       precision: numOrNull("v-precision"),
                       merge_gap: numOrNull("v-merge") };
        if (source.value) body.video = source.value;
        else body.remote_video = { host: source.host, path: source.path };
        const r = await fetch(root + "api/video/analyze", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
        if (!r.ok) {
            const d = await r.json().catch(() => ({}));
            showError(d.error || "analyze failed");
            return;
        }
        renderedFor = null;
        refresh();
        startPolling();
    });

    cancelBtn.addEventListener("click", async () => {
        await fetch(root + "api/video/cancel", { method: "POST" });
        refresh();
    });

    // --- review --------------------------------------------------------------

    function nearestSampleMs(t) {
        let best = null;
        for (const s of job.samples) {
            if (best === null || Math.abs(s.t - t) < Math.abs(best - t)) best = s.t;
        }
        return best === null ? null : Math.round(best * 1000);
    }

    function thumb(t) {
        const img = document.createElement("img");
        const ms = nearestSampleMs(t);
        if (ms !== null) img.src = root + "api/video/frame?ms=" + ms;
        img.alt = fmt(t);
        img.loading = "lazy";
        return img;
    }

    function seek(t) {
        if (preview.style.display === "none") return;
        preview.currentTime = t;
    }

    function updateTotals() {
        let n = 0, secs = 0;
        job.segments.forEach((s, i) => {
            if (keep[i]) { n += 1; secs += s.end - s.start; }
        });
        totalEl.textContent = job.segments.length
            ? "Keeping " + n + " of " + job.segments.length + " segment" +
              (job.segments.length === 1 ? "" : "s") + " — " + fmt(secs) + " total"
            : "Nothing matched. Try a broader description or a smaller stride.";
        exportBtn.disabled = n === 0 || RUNNING.includes(job.state);
        [...timeline.querySelectorAll(".v-seg")].forEach((el, i) =>
            el.classList.toggle("dropped", !keep[i]));
    }

    function renderReview() {
        const dur = (job.probe && job.probe.duration) || 1;
        timeline.innerHTML = "";
        for (const s of job.samples) {
            const tick = document.createElement("div");
            tick.className = "v-tick " + (s.match ? "yes" : "no");
            tick.style.left = (100 * s.t / dur) + "%";
            timeline.appendChild(tick);
        }
        job.segments.forEach((s) => {
            const seg = document.createElement("div");
            seg.className = "v-seg";
            seg.style.left = (100 * s.start / dur) + "%";
            seg.style.width = Math.max(0.3, 100 * (s.end - s.start) / dur) + "%";
            timeline.appendChild(seg);
        });

        segList.innerHTML = "";
        job.segments.forEach((s, i) => {
            const li = document.createElement("li");
            const cb = document.createElement("input");
            cb.type = "checkbox";
            cb.checked = keep[i];
            cb.addEventListener("click", (e) => e.stopPropagation());
            cb.addEventListener("change", () => { keep[i] = cb.checked; updateTotals(); });
            const meta = document.createElement("span");
            meta.className = "q-meta";
            meta.textContent = "#" + (i + 1) + "  " + fmt(s.start) + " – " + fmt(s.end) +
                "  (" + (s.end - s.start).toFixed(1) + " s)";
            li.append(cb, thumb(s.start), thumb(s.end), meta);
            li.addEventListener("click", () => { seek(s.start); preview.play().catch(() => {}); });
            segList.appendChild(li);
        });
        updateTotals();
    }

    timeline.addEventListener("click", (e) => {
        if (!job || !job.probe) return;
        const r = timeline.getBoundingClientRect();
        seek(job.probe.duration * (e.clientX - r.left) / r.width);
    });

    exportBtn.addEventListener("click", async () => {
        showError("");
        const r = await fetch(root + "api/video/export", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ keep: keep, basename: basenameEl.value.trim() }),
        });
        if (!r.ok) {
            const d = await r.json().catch(() => ({}));
            showError(d.error || "export failed");
            return;
        }
        refresh();
        startPolling();
    });

    // --- status ----------------------------------------------------------------

    const STATE_TEXT = {
        staging: "Copying the video from the remote host…",
        probing: "Reading the video…",
        exporting: "Encoding the trimmed video…",
        cancelled: "Cancelled. Re-running reuses every frame already checked.",
    };

    let lastState = null;

    function render() {
        const running = RUNNING.includes(job.state);
        // A finished run has just measured the model's speed.
        if (lastState === "analyzing" && job.state !== "analyzing") {
            loadModels().then(scheduleEstimate);
        }
        lastState = job.state;
        analyzeBtn.disabled = running;
        cancelBtn.style.display = running ? "inline-block" : "none";
        clearBtn.disabled = running;
        flushBtn.disabled = running;
        paintError();

        progress.style.display = running ? "block" : "none";
        if (job.state === "analyzing" && job.calls_est) {
            bar.style.width = (100 * job.calls_done / job.calls_est) + "%";
            statusEl.textContent = "Checked " + job.calls_done + " of ~" + job.calls_est +
                " frames" + (job.model ? " with " + job.model : "") +
                (job.eta_s != null ? " · about " + fmtEta(job.eta_s) + " left" : "");
        } else {
            bar.style.width = running ? "100%" : "0%";
            statusEl.textContent = STATE_TEXT[job.state] || "";
        }

        // Once a job exists its file is previewable, remote or not.
        if (job.state !== "idle" && job.staged_path && source && sameAsJob()) {
            setPreview(root + "api/video/file?job=1");
        }

        const reviewable = ["review", "exporting", "done"].includes(job.state);
        review.style.display = reviewable ? "block" : "none";
        if (reviewable) {
            if (keepFor !== job.started_at || keep.length !== job.segments.length) {
                keep = job.segments.map(s => s.keep !== false);
                keepFor = job.started_at;
            }
            const sig = job.started_at + "|" + job.samples.length + "|" + job.segments.length;
            if (sig !== renderedFor) { renderReview(); renderedFor = sig; }
            else updateTotals();
        }

        if (job.state === "done" && job.output) {
            const url = root + "api/video/result?v=" + encodeURIComponent(job.finished_at || "");
            if (resultPlayer.getAttribute("src") !== url) resultPlayer.src = url;
            // The export stays where it was written; the output dir's
            // .flush.sh picks it up with everything else.
            savedEl.textContent = "Saved to the output folder as " + job.output +
                " — Flush moves it off this machine with the rest of the output.";
            resultWrap.style.display = "block";
        } else {
            resultWrap.style.display = "none";
        }
    }

    async function refresh() {
        try {
            const r = await fetch(root + "api/video/status");
            if (!r.ok) return;
            job = await r.json();
        } catch (e) { return; }
        render();
        if (!RUNNING.includes(job.state) && !tabShown) stopPolling();
    }

    function startPolling() {
        if (!poll) poll = setInterval(refresh, 1000);
    }

    function stopPolling() {
        if (poll) { clearInterval(poll); poll = null; }
    }

    // Restore the last job's inputs so a reload lands where the user left off.
    async function init() {
        await refresh();
        if (job && job.model) wantModel = job.model;
        await loadModels();
        const src = job && job.source;
        if (src && src.value) await loadList(src.value);
        else await loadList();
        if (src && src.path) pickRemote(src.host, src.path);
        if (job) render();
        if (job && job.prompt) promptEl.value = job.prompt;
        if (job && RUNNING.includes(job.state)) startPolling();
    }

    // --- prompt tools, Clear, Flush ----------------------------------------------

    // Same Copy / Paste / Clear text and prompt library (local or remote) as
    // the Single tab; a loaded prompt names the exported video.
    window.wirePromptTools("v-", promptEl, basenameEl, showError);

    function resetView() {
        promptEl.value = "";
        basenameEl.value = "";
        keep = []; keepFor = null; renderedFor = null;
        review.style.display = "none";
        resultWrap.style.display = "none";
        resultPlayer.removeAttribute("src");
        timeline.innerHTML = ""; segList.innerHTML = "";
        showError("");
    }

    // Clear forgets the job (and its thumbnails); no file is touched.
    clearBtn.addEventListener("click", async () => {
        const r = await fetch(root + "api/video/clear", { method: "POST" });
        if (!r.ok) {
            const d = await r.json().catch(() => ({}));
            showError(d.error || "clear failed");
            return;
        }
        resetView();
        sel.value = ""; pickLocal("");
        refresh();
    });

    // Flush is the same as the Single tab's: the input/output .flush.sh
    // scripts run, and input videos (uploads, top-level files) are deleted.
    flushBtn.addEventListener("click", async () => {
        if (!confirm("Flush the input and output directories? Input videos are deleted.")) return;
        showError("");
        const orig = flushBtn.textContent;
        flushBtn.disabled = true;
        flushBtn.textContent = "Flushing…";
        try {
            const r = await fetch(root + "api/flush", { method: "POST" });
            const d = await r.json().catch(() => ({}));
            if (!r.ok) showError(d.error || "flush failed");
            else {
                flushBtn.textContent = "✓ Flushed";
                resetView();
                await loadList("");
                pickLocal("");
                refresh();
            }
        } catch (e) {
            showError("flush failed: " + e.message);
        } finally {
            setTimeout(() => { flushBtn.textContent = orig; flushBtn.disabled = false; }, 1500);
        }
    });

    window.cozyVideo = {
        shown(on) {
            tabShown = on;
            if (on) { refresh(); startPolling(); loadModels(); }
            else if (!job || !RUNNING.includes(job.state)) stopPolling();
        },
    };
    init();
})();
