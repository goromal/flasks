// Canonical copy. Agent UI, budget and Assistant use it through symlinks that
// anixpkgs replaces with copies at build time; Folio's frontend/src/agent/composer.ts
// is a port. Keep them in sync.
//
// Native composer: phone keyboards (autocorrect, swipe, dictation) work in a
// real textarea. Typed text is handed to the caller's `send` function, which
// is responsible for getting it to the agent (e.g. posting it to a backend
// that pastes it into a tmux pane).
(function () {
  var STYLE_ATTR = "data-agent-composer";

  function injectStyle() {
    if (document.querySelector("style[" + STYLE_ATTR + "]")) return;
    var style = document.createElement("style");
    style.setAttribute(STYLE_ATTR, "");
    style.textContent = [
      // Host pages (budget_ui, Assistant) carry generic rules like
      // "#agent-body form" or ".main form" for their own login/new-session
      // forms; those are higher-specificity than a plain ".agent-composer"
      // class (one has an id, both have a type selector) and were observed
      // overriding display, flex-direction, margin, padding, gap and width
      // (one of them sets a max-width that squeezes this row short of the
      // page edge). The "form.agent-composer" selector plus !important on
      // exactly those contested properties is the only way to reliably beat
      // an id-qualified host selector without knowing its name in advance.
      // flex-wrap, align-items and box-sizing aren't contested by any rule
      // seen so far; they're !important here defensively, for the same
      // reason. Cosmetic properties (background/colors/fonts) don't need it,
      // since host rules here never target this class specifically.
      ".agent-composer {",
      "  display: flex;",
      "  flex-direction: row;",
      "  flex-wrap: nowrap;",
      "  align-items: stretch;",
      "  gap: 6px;",
      "  width: 100%;",
      "  box-sizing: border-box;",
      "  margin: 0;",
      "  padding: 6px 6px 0;",
      "  background: #10151d;",
      "  border-top: 1px solid #2d3b4c;",
      "}",
      "form.agent-composer {",
      "  display: flex !important;",
      "  flex-direction: row !important;",
      "  flex-wrap: nowrap !important;",
      "  align-items: stretch !important;",
      "  margin: 0 !important;",
      "  padding: 6px 6px 0 !important;",
      "  gap: 6px !important;",
      "  width: 100% !important;",
      "  box-sizing: border-box !important;",
      "}",
      ".agent-composer .agent-composer__text {",
      "  display: block;",
      "  flex: 1 1 auto;",
      "  min-width: 0;",
      "  width: auto;",
      "  height: auto;",
      "  min-height: 0;",
      "  max-height: 152px;",
      "  margin: 0;",
      "  box-sizing: border-box;",
      "  resize: none;",
      "  overflow-y: auto;",
      "  padding: 10px;",
      "  border: 1px solid #3a4b5e;",
      "  border-radius: 8px;",
      "  background: #0b1017;",
      "  color: #eef4f8;",
      "  font: 16px/1.35 system-ui, sans-serif;",
      "  line-height: 1.35;",
      "}",
      ".agent-composer .agent-composer__send {",
      "  display: flex;",
      "  align-items: center;",
      "  justify-content: center;",
      "  flex: 0 0 auto;",
      "  width: auto;",
      "  height: auto;",
      "  min-height: 0;",
      "  margin: 0;",
      "  box-sizing: border-box;",
      "  padding: 0 16px;",
      "  border: 1px solid #2f81f7;",
      "  border-radius: 8px;",
      "  background: #2f81f7;",
      "  color: #fff;",
      "  font: 650 16px/1 system-ui, sans-serif;",
      "  line-height: 1;",
      "  cursor: pointer;",
      "  touch-action: manipulation;",
      "}",
      ".agent-composer .agent-composer__send:disabled { opacity: 0.7; }",
    ].join("\n");
    document.head.appendChild(style);
  }

  function flash(button, message) {
    var label = button.textContent;
    button.textContent = message;
    button.disabled = true;
    window.setTimeout(function () {
      button.textContent = label;
      button.disabled = false;
    }, 1200);
  }

  function mount(container, options) {
    injectStyle();

    var draftKey = options.draftKey;
    var send = options.send;

    var form = document.createElement("form");
    form.className = "agent-composer";

    var textarea = document.createElement("textarea");
    textarea.className = "agent-composer__text";
    textarea.rows = 1;
    textarea.setAttribute("enterkeyhint", "enter");
    textarea.setAttribute("autocapitalize", "sentences");
    textarea.setAttribute("placeholder", "Message");
    textarea.setAttribute("aria-label", "Message to send");

    var button = document.createElement("button");
    button.type = "submit";
    button.className = "agent-composer__send";
    button.textContent = "Send";

    form.appendChild(textarea);
    form.appendChild(button);
    container.appendChild(form);

    function loadDraft() {
      try { return window.localStorage.getItem(draftKey) || ""; } catch (err) { return ""; }
    }
    function saveDraft(text) {
      try {
        if (text) window.localStorage.setItem(draftKey, text);
        else window.localStorage.removeItem(draftKey);
      } catch (err) {}
    }
    function fit() {
      textarea.style.height = "auto";
      textarea.style.height = textarea.scrollHeight + 2 + "px";
    }
    function submit() {
      if (button.disabled) return;
      var sent = textarea.value;
      button.disabled = true;
      send(sent)
        .then(function () {
          button.disabled = false;
          // Keep anything typed while the request was in flight.
          if (textarea.value === sent) {
            textarea.value = "";
            saveDraft("");
            fit();
          }
        })
        .catch(function () {
          button.disabled = false;
          flash(button, "Failed");
        });
    }

    textarea.value = loadDraft();
    fit();
    textarea.addEventListener("input", function () {
      saveDraft(textarea.value);
      fit();
    });
    textarea.addEventListener("keydown", function (evt) {
      if (evt.key === "Enter" && (evt.ctrlKey || evt.metaKey)) {
        evt.preventDefault();
        submit();
      }
    });
    // Keep focus (and the phone keyboard) on the field when tapping Send.
    button.addEventListener("pointerdown", function (evt) { evt.preventDefault(); });
    form.addEventListener("submit", function (evt) {
      evt.preventDefault();
      submit();
    });

    return { form: form, textarea: textarea, button: button };
  }

  window.AgentComposer = { mount: mount };
})();
