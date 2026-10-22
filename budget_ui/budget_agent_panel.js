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
  const show = (...nodes) => body.replaceChildren(...nodes);

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
    show(
      el('div', { className: 'agent-bar' }, el('span', { className: 'agent-name', textContent: name }), back, close),
      el('iframe', { className: 'agent-frame', title: 'Agent terminal', src: base + '/terminal/?arg=' + encodeURIComponent(name) }),
    );
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
