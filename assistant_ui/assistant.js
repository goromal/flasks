// Assistant UI: full-page console for repo-rooted agent sessions (second-brain).
// Terminal controls are budget_ui's plain-JS port of Folio/Agent UI's.
(function () {
  const app = document.getElementById('app');
  const base = app.dataset.base;
  const side = document.getElementById('side');
  const newBox = document.getElementById('new');
  const list = document.getElementById('list');
  const main = document.getElementById('main');
  const repoChip = document.getElementById('repo');
  const SESSION_KEY = 'assistant.session';
  let csrf = '';
  let agents = [];
  let sessions = [];
  let current = null;
  let timers = [];

  // localStorage can throw (private mode, locked-down webview): best effort only.
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* non-fatal */ } },
    del(k) { try { localStorage.removeItem(k); } catch { /* non-fatal */ } },
  };

  const el = (tag, props = {}, ...children) => {
    const node = Object.assign(document.createElement(tag), props);
    children.forEach((c) => node.append(c));
    return node;
  };
  // Terminal controls for the current view; torn down on any view change.
  let teardown = null;
  const show = (...nodes) => {
    if (teardown) { teardown(); teardown = null; }
    main.replaceChildren(...nodes);
  };

  async function api(path, opts = {}) {
    const headers = { 'Content-Type': 'application/json' };
    if (opts.method && opts.method !== 'GET') headers['X-CSRF-Token'] = csrf;
    const res = await fetch(base + path, { credentials: 'same-origin', ...opts, headers });
    if (!res.ok) throw Object.assign(new Error(res.statusText), { status: res.status });
    return res.status === 204 ? null : res.json();
  }

  // Key bar + touch controls for the same-origin ttyd iframe (port of Folio's
  // terminalControls.ts, which mirrors Agent UI): touch-drag scrolls tmux
  // history; Select turns the next drag into an xterm selection.
  function installTerminalControls(iframe, onSelectingChange) {
    const STEP = 12;
    let selecting = false;
    let selectionStart = null;
    let scrollY = null;
    let scrollMode = false;
    let scrollOffset = 0;
    let doc = null;

    function terminal() {
      try { return (iframe.contentWindow && iframe.contentWindow.term) || null; } catch { return null; }
    }
    function helperTextarea() {
      try {
        const d = iframe.contentDocument;
        return d && (d.querySelector('.xterm-helper-textarea') || d.querySelector('textarea'));
      } catch { return null; }
    }
    function sendKey(key, code, ctrl, shift) {
      const ta = helperTextarea();
      const win = iframe.contentWindow;
      if (!ta || !win) return;
      ta.focus();
      const event = new win.KeyboardEvent('keydown', { key, ctrlKey: !!ctrl, shiftKey: !!shift, bubbles: true, cancelable: true });
      Object.defineProperty(event, 'keyCode', { get: () => code });
      Object.defineProperty(event, 'which', { get: () => code });
      ta.dispatchEvent(event);
    }
    // Sticky Ctrl/Shift: the next key is sent with the armed modifiers held.
    // Ctrl also applies to letters typed on the phone keyboard, which arrive as
    // input/composition events rather than keydowns, so those are intercepted
    // ahead of xterm. Shift applies to bar keys (Shift+Tab, Shift+arrows); typed
    // text clears it, since the phone keyboard has its own shift.
    const CTRL_CODES = { ' ': 32, '[': 219, '\\': 220, ']': 221 };
    let ctrlArmed = false;
    let shiftArmed = false;
    let swallowComposition = false;
    const controls = {};
    function setCtrl(armed) {
      ctrlArmed = armed;
      if (controls.onCtrlChange) controls.onCtrlChange(armed);
    }
    function setShift(armed) {
      shiftArmed = armed;
      if (controls.onShiftChange) controls.onShiftChange(armed);
    }
    function ctrlCode(ch) {
      return /^[a-z]$/i.test(ch) ? ch.toUpperCase().charCodeAt(0) : CTRL_CODES[ch];
    }
    function pressKey(key, code, ctrl) {
      const withCtrl = !!ctrl || ctrlArmed;
      const withShift = shiftArmed;
      setCtrl(false);
      setShift(false);
      sendKey(key, code, withCtrl, withShift);
    }
    function sendCtrlChar(data) {
      const ch = data.slice(-1);
      const code = ctrlCode(ch);
      const withShift = shiftArmed;
      setCtrl(false);
      setShift(false);
      if (code) sendKey(ch.toLowerCase(), code, true, withShift);
    }
    function clearHelper() {
      const ta = helperTextarea();
      if (ta) ta.value = '';
    }
    function onCtrlKeydown(e) {
      if (shiftArmed && !ctrlArmed && e.isTrusted && e.key.length === 1) setShift(false);
      // Our own synthetic keydowns pass; 'Unidentified'/'Process' fall to onCtrlInput.
      if (!ctrlArmed || !e.isTrusted || e.ctrlKey || e.key.length !== 1 || !ctrlCode(e.key)) return;
      e.preventDefault();
      e.stopImmediatePropagation();
      sendCtrlChar(e.key);
    }
    function onCtrlComposition(e) {
      if (e.type === 'compositionstart' && ctrlArmed) swallowComposition = true;
      if (!swallowComposition) return;
      e.stopImmediatePropagation();
      if (e.type === 'compositionend') {
        swallowComposition = false;
        clearHelper();
      }
      if (ctrlArmed && e.data) {
        sendCtrlChar(e.data);
        // Blur/refocus makes the IME commit, so its compositionend is swallowed.
        const ta = helperTextarea();
        if (ta && swallowComposition) { ta.blur(); ta.focus(); }
      }
    }
    function onCtrlInput(e) {
      if (shiftArmed && !ctrlArmed && e.data) setShift(false);
      if (!ctrlArmed && !swallowComposition) return;
      e.stopImmediatePropagation();
      if (ctrlArmed && e.data && !e.isComposing) {
        sendCtrlChar(e.data);
        clearHelper();
      }
    }
    function setSelecting(enabled) {
      selecting = enabled;
      selectionStart = null;
      onSelectingChange(enabled);
    }
    function touchCell(term, touch) {
      const screen = term.element && term.element.querySelector('.xterm-screen');
      if (!screen) return null;
      const rect = screen.getBoundingClientRect();
      if (!rect.width || !rect.height) return null;
      const column = Math.max(0, Math.min(term.cols - 1, Math.floor((touch.clientX - rect.left) * term.cols / rect.width)));
      const viewportRow = Math.max(0, Math.min(term.rows - 1, Math.floor((touch.clientY - rect.top) * term.rows / rect.height)));
      return { column, row: term.buffer.active.viewportY + viewportRow };
    }
    function updateTouchSelection(term, end) {
      if (!selectionStart) return;
      const a = selectionStart.row * term.cols + selectionStart.column;
      const b = end.row * term.cols + end.column;
      const first = Math.min(a, b);
      const last = Math.max(a, b);
      term.select(first % term.cols, Math.floor(first / term.cols), last - first + 1);
    }
    function handleSelectionTouch(event, ending) {
      const term = terminal();
      const touches = ending ? event.changedTouches : event.touches;
      const cell = term && touches.length ? touchCell(term, touches[0]) : null;
      if (!term || !cell) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      if (!selectionStart) selectionStart = cell;
      updateTouchSelection(term, cell);
      if (ending) setSelecting(false);
    }
    // tmux copy-mode (prefix C-b, then [) gives line-by-line history scrolling.
    function scrollLine(up) {
      if (up) {
        if (!scrollMode) {
          sendKey('b', 66, true);
          sendKey('[', 219);
          scrollMode = true;
          scrollOffset = 0;
        }
        sendKey('ArrowUp', 38);
        scrollOffset += 1;
      } else if (scrollMode) {
        sendKey('ArrowDown', 40);
        scrollOffset -= 1;
        if (scrollOffset <= 0) {
          sendKey('q', 81);
          scrollMode = false;
          scrollOffset = 0;
        }
      }
    }
    function handleScrollTouch(event) {
      if (scrollY === null || event.touches.length !== 1) return;
      const y = event.touches[0].clientY;
      let delta = y - scrollY;
      let steps = 0;
      while (delta >= STEP) { delta -= STEP; steps += 1; }
      while (delta <= -STEP) { delta += STEP; steps -= 1; }
      if (steps === 0) return;
      scrollY = y - delta;
      if (steps < 0 && !scrollMode) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      for (let i = 0; i < Math.abs(steps); i += 1) scrollLine(steps > 0);
    }
    function onStart(e) {
      if (selecting) handleSelectionTouch(e, false);
      else scrollY = e.touches.length === 1 ? e.touches[0].clientY : null;
    }
    function onMove(e) { if (selecting) handleSelectionTouch(e, false); else handleScrollTouch(e); }
    function onEnd(e) { if (selecting) handleSelectionTouch(e, true); else scrollY = null; }
    function onCancel() { if (selecting) setSelecting(false); else scrollY = null; }

    function detach() {
      if (!doc) return;
      doc.removeEventListener('touchstart', onStart, true);
      doc.removeEventListener('touchmove', onMove, true);
      doc.removeEventListener('touchend', onEnd, true);
      doc.removeEventListener('touchcancel', onCancel, true);
      doc.removeEventListener('keydown', onCtrlKeydown, true);
      doc.removeEventListener('input', onCtrlInput, true);
      ['compositionstart', 'compositionupdate', 'compositionend'].forEach((t) => doc.removeEventListener(t, onCtrlComposition, true));
      doc = null;
    }
    function attach() {
      try {
        const next = iframe.contentDocument;
        if (!next || next === doc) return;
        detach();
        doc = next;
        const opts = { capture: true, passive: false };
        doc.addEventListener('touchstart', onStart, opts);
        doc.addEventListener('touchmove', onMove, opts);
        doc.addEventListener('touchend', onEnd, opts);
        doc.addEventListener('touchcancel', onCancel, { capture: true });
        doc.addEventListener('keydown', onCtrlKeydown, true);
        doc.addEventListener('input', onCtrlInput, true);
        ['compositionstart', 'compositionupdate', 'compositionend'].forEach((t) => doc.addEventListener(t, onCtrlComposition, true));
      } catch { /* iframe not ready */ }
    }

    const URL_START = /^\s*https?:\/\//i;
    function terminalText(term) {
      const selected = term.getSelection();
      if (selected) {
        // A URL selected across terminal rows can contain line breaks.
        return URL_START.test(selected) ? selected.replace(/\s+/g, '') : selected;
      }
      const buffer = term.buffer.active;
      const lines = [];
      const end = Math.min(buffer.length, buffer.viewportY + term.rows);
      for (let row = buffer.viewportY; row < end; row += 1) {
        const line = buffer.getLine(row);
        lines.push(line ? line.translateToString(true) : '');
      }
      while (lines.length && !lines[lines.length - 1]) lines.pop();
      return lines.join('\n');
    }
    // The selected URL, else the last URL anywhere in the buffer (Agent UI's
    // latestUrl). tmux redraws a long URL as separate full-width rows whose
    // isWrapped flag is false, so a URL reaching the row edge is joined too.
    function latestUrl(term) {
      const selected = term.getSelection();
      if (selected && URL_START.test(selected)) return selected.replace(/\s+/g, '');
      const buffer = term.buffer.active;
      let latest = null;
      for (let row = 0; row < buffer.length; row += 1) {
        const line = buffer.getLine(row);
        if (!line) continue;
        const text = line.translateToString(true);
        const urls = /https?:\/\/[^\s<>"']+/gi;
        let match;
        while ((match = urls.exec(text))) {
          let url = match[0];
          let part = text;
          let nextRow = row + 1;
          const atEnd = match.index + url.length === text.length;
          while (atEnd && nextRow < buffer.length) {
            const nextLine = buffer.getLine(nextRow);
            if (!nextLine) break;
            const nextText = nextLine.translateToString(true);
            if (!(nextLine.isWrapped || part.length >= term.cols) ||
                !/^[^\s<>"']+$/.test(nextText) || URL_START.test(nextText)) break;
            url += nextText;
            part = nextText;
            nextRow += 1;
          }
          latest = url.replace(/[.,;:!?)]*$/, '');
        }
      }
      return latest;
    }
    // Synchronous so window.open stays inside the click's user activation.
    function openUrl() {
      const term = terminal();
      if (!term) return 'Not ready';
      const url = latestUrl(term);
      if (!url) return 'No URL';
      window.open(url, '_blank', 'noopener,noreferrer');
      return null;
    }
    function legacyCopy(text) {
      const field = el('textarea', { value: text, readOnly: false });
      Object.assign(field.style, { position: 'fixed', top: '0', left: '0', opacity: '0' });
      document.body.appendChild(field);
      field.contentEditable = 'true';
      const range = document.createRange();
      range.selectNodeContents(field);
      const selection = window.getSelection();
      if (selection) { selection.removeAllRanges(); selection.addRange(range); }
      field.setSelectionRange(0, text.length);
      let copied = false;
      try { copied = document.execCommand('copy'); } catch { copied = false; }
      if (selection) selection.removeAllRanges();
      field.remove();
      return copied;
    }
    async function copy() {
      const term = terminal();
      if (!term) return 'Not ready';
      const text = terminalText(term);
      if (!text) { term.focus(); return 'Empty'; }
      try {
        if (window.isSecureContext && navigator.clipboard && navigator.clipboard.writeText) {
          await navigator.clipboard.writeText(text);
        } else if (!legacyCopy(text)) {
          window.prompt('Copy the terminal text:', text);
        }
      } catch {
        if (!legacyCopy(text)) window.prompt('Copy the terminal text:', text);
      }
      term.focus();
      return 'Copied!';
    }
    async function paste() {
      const term = terminal();
      if (!term) return 'Not ready';
      let text;
      try {
        text = window.isSecureContext && navigator.clipboard && navigator.clipboard.readText
          ? await navigator.clipboard.readText()
          : window.prompt('Paste clipboard text into the terminal:');
      } catch {
        text = window.prompt('Paste clipboard text into the terminal:');
      }
      if (text === null) { term.focus(); return 'Cancelled'; }
      term.paste(text);
      term.focus();
      return 'Pasted!';
    }

    iframe.addEventListener('load', attach);
    attach();
    return {
      controls: Object.assign(controls, {
        copy, openUrl, paste, pressKey,
        toggleCtrl() { setCtrl(!ctrlArmed); },
        toggleShift() { setShift(!shiftArmed); },
        toggleSelection() {
          const term = terminal();
          if (!term) return;
          term.clearSelection();
          setSelecting(!selecting);
        },
      }),
      cleanup() {
        iframe.removeEventListener('load', attach);
        detach();
        if (selecting) setSelecting(false);
      },
    };
  }

  function keyBar(controls) {
    const btn = (label, onClick, aria) => {
      const b = el('button', { type: 'button', textContent: label });
      if (aria) b.setAttribute('aria-label', aria);
      b.addEventListener('click', onClick);
      return b;
    };
    const flash = async (b, action, original) => {
      b.textContent = await action();
      window.setTimeout(() => { b.textContent = original; }, 1200);
    };
    const select = btn('Select', () => controls.toggleSelection());
    select.title = 'Drag across terminal text to select it';
    select.setAttribute('aria-pressed', 'false');
    const copyBtn = btn('Copy', () => flash(copyBtn, controls.copy, 'Copy'));
    const openBtn = btn('Open URL', () => {
      const result = controls.openUrl();
      if (result) flash(openBtn, () => result, 'Open URL');
    });
    openBtn.title = 'Open the selected URL, or the latest URL in the terminal';
    const pasteBtn = btn('Paste', () => flash(pasteBtn, controls.paste, 'Paste'));
    const nav = el('nav', { className: 'keys' }, select, copyBtn, openBtn, pasteBtn);
    nav.setAttribute('aria-label', 'Terminal keys');
    const modifier = (label, toggle, aria) => {
      const b = btn(label, toggle, aria);
      b.setAttribute('aria-pressed', 'false');
      nav.append(b);
      return (on) => {
        b.classList.toggle('active', on);
        b.setAttribute('aria-pressed', String(on));
      };
    };
    controls.onCtrlChange = modifier('Ctrl', () => controls.toggleCtrl(), 'Control (applies to next key)');
    controls.onShiftChange = modifier('Shift', () => controls.toggleShift(), 'Shift (applies to next key)');
    [['Esc', 'Escape', 27], ['Tab', 'Tab', 9], ['↑', 'ArrowUp', 38], ['↓', 'ArrowDown', 40],
      ['←', 'ArrowLeft', 37], ['→', 'ArrowRight', 39], ['↵', 'Enter', 13]]
      .forEach(([label, key, code]) => nav.append(btn(label, () => controls.pressKey(key, code), key)));
    nav.append(btn('^C', () => controls.pressKey('c', 67, true), 'Control C'));
    return { nav, select };
  }

  const AGENT_LABELS = { claude: 'Claude', codex: 'Codex' };
  const label = (agent) => AGENT_LABELS[agent] || agent;
  const shortId = (name) => name.split('--').pop();
  function age(created) {
    const s = Math.max(0, Math.floor(Date.now() / 1000) - created);
    if (s < 60) return 'just now';
    if (s < 3600) return Math.floor(s / 60) + ' min ago';
    if (s < 86400) return Math.floor(s / 3600) + ' h ago';
    return Math.floor(s / 86400) + ' d ago';
  }

  // ---- repo status chip -------------------------------------------------
  async function refreshRepo() {
    let st;
    try { st = await api('/status'); } catch { return; }
    let cls = 'ok';
    let text;
    if (!st.present) {
      cls = 'warn';
      text = 'second-brain: not cloned yet (a new session clones it)';
    } else if (st.error) {
      cls = 'bad';
      text = 'second-brain: ' + st.error;
    } else {
      const parts = [st.branch || '?'];
      if (!st.upstream) { cls = 'bad'; parts.push('no upstream'); }
      if (st.ahead) { cls = 'bad'; parts.push(st.ahead + ' unpushed'); }
      if (st.behind) { if (cls === 'ok') cls = 'warn'; parts.push(st.behind + ' behind'); }
      if (st.dirty) { if (cls === 'ok') cls = 'warn'; parts.push(st.dirty + ' changed'); }
      if (cls === 'ok') parts.push('synced');
      text = parts.join(' · ');
      if (st.last) repoChip.title = `Last commit ${st.last.hash} (${st.last.when}): ${st.last.subject}`;
    }
    repoChip.className = 'sb-status ' + cls;
    // The prefix is dropped on narrow screens (CSS) so the state stays visible.
    repoChip.replaceChildren(el('span', { className: 'pre', textContent: 'second-brain · ' }),
      text.replace(/^second-brain: /, ''));
  }
  repoChip.addEventListener('click', refreshRepo);

  // ---- session list -----------------------------------------------------
  function renderNew() {
    newBox.replaceChildren(...agents.map((agent) => {
      const b = el('button', { type: 'button', textContent: '+ ' + label(agent) });
      b.title = 'Start a new ' + label(agent) + ' session in second-brain';
      b.addEventListener('click', async () => {
        b.disabled = true;
        try {
          const { name } = await api('/sessions', { method: 'POST', body: JSON.stringify({ agent }) });
          await refreshSessions();
          open(name);
        } catch {
          show(el('p', { className: 'msg error', textContent: 'Could not start a session.' }));
        } finally { b.disabled = false; }
      });
      return b;
    }));
  }

  function renderList() {
    if (!sessions.length) {
      list.replaceChildren(el('li', { className: 'empty', textContent: 'No sessions yet.' }));
      return;
    }
    list.replaceChildren(...sessions.map((s) => {
      const openBtn = el('button', { type: 'button', className: 'open' },
        el('b', { textContent: label(s.agent) + ' · ' + shortId(s.name) }),
        el('small', { textContent: age(s.created) + (s.attached ? ' · attached' : '') }));
      openBtn.addEventListener('click', () => open(s.name));
      const kill = el('button', { type: 'button', className: 'kill', textContent: '✕' });
      kill.title = 'End this session';
      kill.setAttribute('aria-label', 'End session ' + shortId(s.name));
      kill.addEventListener('click', async () => {
        if (!window.confirm('End this ' + label(s.agent) + ' session? Its conversation is lost; pushed second-brain work is safe.')) return;
        kill.disabled = true;
        try { await api('/sessions/' + encodeURIComponent(s.name), { method: 'DELETE' }); } catch { /* refresh anyway */ }
        if (current === s.name) { current = null; store.del(SESSION_KEY); }
        await refreshSessions();
        if (!current) idle();
        refreshRepo();
      });
      return el('li', { className: s.name === current ? 'active' : '' }, openBtn, kill);
    }));
  }

  async function refreshSessions() {
    try { sessions = await api('/sessions'); } catch (err) {
      if (err.status === 401) { stop(); loginView(); }
      return;
    }
    if (current && !sessions.some((s) => s.name === current)) {
      current = null;
      store.del(SESSION_KEY);
      idle();
    }
    renderList();
  }

  // ---- main pane views --------------------------------------------------
  function idle() {
    document.body.classList.remove('in-session');
    const hints = el('div', { className: 'center' },
      el('p', { textContent: sessions.length ? 'Pick a session, or start a new one.' : 'Start a session to work in second-brain.' }),
      el('p', { textContent: 'Try: "weekly triage", "migrate church", "triage faith", "kathleen: ask about the trip", "help me draft an essay on …".' }));
    show(hints);
  }

  function open(name) {
    current = name;
    store.set(SESSION_KEY, name);
    renderList();
    document.body.classList.add('in-session');
    const frame = el('iframe', { className: 'frame', title: 'Agent terminal', src: base + '/terminal/?arg=' + encodeURIComponent(name) });
    let select = null;
    const installed = installTerminalControls(frame, (on) => {
      if (!select) return;
      select.classList.toggle('active', on);
      select.setAttribute('aria-pressed', String(on));
      select.textContent = on ? 'Drag text' : 'Select';
    });
    const bar = keyBar(installed.controls);
    select = bar.select;
    show(frame, bar.nav);
    teardown = installed.cleanup;
  }

  function httpsView() {
    const url = 'https://' + location.host + location.pathname + location.search;
    show(el('div', { className: 'center' },
      el('p', { textContent: 'The Assistant needs HTTPS.' }),
      el('a', { href: url, textContent: 'Open this page over HTTPS' })));
  }

  function loginView(error) {
    side.hidden = true;
    document.body.classList.remove('in-session');
    const input = el('input', { type: 'password', autocomplete: 'current-password', placeholder: 'Agent password' });
    const button = el('button', { type: 'submit', textContent: 'Unlock' });
    const form = el('form', {}, input, button);
    if (error) form.append(el('p', { className: 'error', textContent: error }));
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      button.disabled = true;
      try {
        const cfg = await api('/login', { method: 'POST', body: JSON.stringify({ password: input.value }) });
        csrf = cfg.csrf; agents = cfg.agents;
        await start();
      } catch (err) {
        loginView(err.status === 401 ? 'Invalid password' : 'Login failed. Try again.');
      }
    });
    show(form);
    input.focus();
  }

  function stop() { timers.forEach(clearInterval); timers = []; }

  async function start() {
    side.hidden = false;
    renderNew();
    await refreshSessions();
    refreshRepo();
    stop();
    timers = [setInterval(refreshSessions, 10000), setInterval(refreshRepo, 15000)];
    const stored = store.get(SESSION_KEY);
    if (stored && sessions.some((s) => s.name === stored)) open(stored);
    else { store.del(SESSION_KEY); idle(); }
  }

  async function init() {
    if (location.protocol !== 'https:' && !['localhost', '127.0.0.1'].includes(location.hostname)) {
      httpsView(); return;
    }
    try {
      const cfg = await api('/config');
      csrf = cfg.csrf; agents = cfg.agents;
    } catch { loginView(); return; }
    await start();
  }
  init();
})();
