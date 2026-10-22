// Budget agent companion panel (plain-JS port of Folio's AgentPanel).
(function () {
  const panel = document.getElementById('agent-panel');
  const toggle = document.getElementById('agent-toggle');
  if (!panel || !toggle) return;
  const base = panel.dataset.base;
  const body = document.getElementById('agent-body');
  const handle = document.getElementById('agent-resize');
  const WIDTH_KEY = 'budget.agentWidth';
  const SESSION_KEY = 'budget.agentSession';
  let csrf = '';
  let agents = [];
  let initialized = false;

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
  // Terminal controls for the current session view; torn down on any view change.
  let teardown = null;
  const show = (...nodes) => {
    if (teardown) { teardown(); teardown = null; }
    body.replaceChildren(...nodes);
  };

  async function api(path, opts = {}) {
    const headers = { 'Content-Type': 'application/json' };
    if (opts.method && opts.method !== 'GET') headers['X-CSRF-Token'] = csrf;
    const res = await fetch(base + path, { credentials: 'same-origin', ...opts, headers });
    if (!res.ok) throw Object.assign(new Error(res.statusText), { status: res.status });
    return res.status === 204 ? null : res.json();
  }

  function httpsView() {
    const url = 'https://' + location.host + location.pathname + location.search;
    show(el('div', { className: 'agent-center' },
      el('p', { textContent: 'The agent companion needs HTTPS.' }),
      el('a', { href: url, textContent: 'Open this page over HTTPS' })));
  }

  function loginView(error) {
    const input = el('input', { type: 'password', autocomplete: 'current-password', placeholder: 'Companion password' });
    const button = el('button', { type: 'submit', textContent: 'Unlock' });
    const form = el('form', {}, input, button);
    if (error) form.append(el('p', { className: 'agent-error', textContent: error }));
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      button.disabled = true;
      try {
        const cfg = await api('/login', { method: 'POST', body: JSON.stringify({ password: input.value }) });
        csrf = cfg.csrf; agents = cfg.agents;
        await pickerView();
      } catch (err) {
        loginView(err.status === 401 ? 'Invalid password' : 'Login failed. Try again.');
      }
    });
    show(form);
    input.focus();
  }

  async function pickerView(error) {
    let live = [];
    try { live = await api('/sessions'); } catch { live = []; }
    const start = el('div', { className: 'agent-row' }, el('span', { textContent: 'New session:' }));
    agents.forEach((agent) => {
      const b = el('button', { type: 'button', textContent: agent });
      b.addEventListener('click', async () => {
        b.disabled = true;
        try {
          const { name } = await api('/sessions', { method: 'POST', body: JSON.stringify({ agent }) });
          sessionView(name);
        } catch { pickerView('Could not start session'); }
      });
      start.append(b);
    });
    const list = el('div', { className: 'agent-center' }, start);
    live.forEach((s) => {
      const open = el('button', { type: 'button', textContent: 'Open' });
      open.addEventListener('click', () => sessionView(s.name));
      const kill = el('button', { type: 'button', textContent: 'Kill' });
      kill.addEventListener('click', async () => {
        kill.disabled = true;
        try { await api('/sessions/' + encodeURIComponent(s.name), { method: 'DELETE' }); } catch { /* refresh anyway */ }
        if (store.get(SESSION_KEY) === s.name) store.del(SESSION_KEY);
        pickerView();
      });
      list.append(el('div', { className: 'agent-row' }, el('code', { textContent: s.name }), open, kill));
    });
    if (error) list.append(el('p', { className: 'agent-error', textContent: error }));
    show(list);
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
    function sendKey(key, code, ctrl) {
      const ta = helperTextarea();
      const win = iframe.contentWindow;
      if (!ta || !win) return;
      ta.focus();
      const event = new win.KeyboardEvent('keydown', { key, ctrlKey: !!ctrl, bubbles: true, cancelable: true });
      Object.defineProperty(event, 'keyCode', { get: () => code });
      Object.defineProperty(event, 'which', { get: () => code });
      ta.dispatchEvent(event);
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
      } catch { /* iframe not ready */ }
    }

    function terminalText(term) {
      const selected = term.getSelection();
      if (selected) return selected;
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
      controls: {
        copy, paste, sendKey,
        toggleSelection() {
          const term = terminal();
          if (!term) return;
          term.clearSelection();
          setSelecting(!selecting);
        },
      },
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
    const pasteBtn = btn('Paste', () => flash(pasteBtn, controls.paste, 'Paste'));
    const nav = el('nav', { className: 'agent-keys' }, select, copyBtn, pasteBtn);
    nav.setAttribute('aria-label', 'Terminal keys');
    [['Esc', 'Escape', 27], ['Tab', 'Tab', 9], ['↑', 'ArrowUp', 38], ['↓', 'ArrowDown', 40],
      ['←', 'ArrowLeft', 37], ['→', 'ArrowRight', 39], ['↵', 'Enter', 13]]
      .forEach(([label, key, code]) => nav.append(btn(label, () => controls.sendKey(key, code), key)));
    nav.append(btn('^C', () => controls.sendKey('c', 67, true), 'Control C'));
    return { nav, select };
  }

  function sessionView(name) {
    store.set(SESSION_KEY, name);
    const back = el('button', { type: 'button', textContent: 'Sessions' });
    back.addEventListener('click', () => pickerView());
    const close = el('button', { type: 'button', textContent: 'Close session' });
    close.addEventListener('click', async () => {
      close.disabled = true;
      try { await api('/sessions/' + encodeURIComponent(name), { method: 'DELETE' }); } catch { /* fall through */ }
      store.del(SESSION_KEY);
      pickerView();
    });
    const frame = el('iframe', { className: 'agent-frame', title: 'Agent terminal', src: base + '/terminal/?arg=' + encodeURIComponent(name) });
    let select = null;
    const installed = installTerminalControls(frame, (on) => {
      if (!select) return;
      select.classList.toggle('active', on);
      select.setAttribute('aria-pressed', String(on));
      select.textContent = on ? 'Drag text' : 'Select';
    });
    const bar = keyBar(installed.controls);
    select = bar.select;
    show(
      el('div', { className: 'agent-bar' }, el('span', { className: 'agent-name', textContent: name }), back, close),
      frame,
      bar.nav,
    );
    teardown = installed.cleanup;
  }

  async function init() {
    if (location.protocol !== 'https:') { httpsView(); return; }
    try {
      const cfg = await api('/config');
      csrf = cfg.csrf; agents = cfg.agents;
    } catch { loginView(); return; }
    const stored = store.get(SESSION_KEY);
    let live = [];
    try { live = await api('/sessions'); } catch { live = []; }
    if (stored && live.some((s) => s.name === stored)) sessionView(stored);
    else { store.del(SESSION_KEY); pickerView(); }
  }

  function setOpen(open) {
    panel.hidden = !open;
    document.body.classList.toggle('agent-open', open);
    toggle.setAttribute('aria-expanded', String(open));
    if (open && !initialized) { initialized = true; init(); }
  }
  toggle.addEventListener('click', () => setOpen(panel.hidden));

  const savedWidth = store.get(WIDTH_KEY);
  if (savedWidth) document.documentElement.style.setProperty('--agent-w', savedWidth);
  handle.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    handle.setPointerCapture(e.pointerId);
    // The iframe would swallow pointer events mid-drag.
    const frame = body.querySelector('iframe');
    if (frame) frame.style.pointerEvents = 'none';
    const move = (ev) => {
      const px = Math.min(Math.max(window.innerWidth - ev.clientX, 320), window.innerWidth * 0.85);
      document.documentElement.style.setProperty('--agent-w', px + 'px');
    };
    const up = () => {
      handle.removeEventListener('pointermove', move);
      handle.removeEventListener('pointerup', up);
      if (frame) frame.style.pointerEvents = '';
      store.set(WIDTH_KEY, getComputedStyle(document.documentElement).getPropertyValue('--agent-w').trim());
    };
    handle.addEventListener('pointermove', move);
    handle.addEventListener('pointerup', up);
  });
})();
