/* ============================================================================
 * chitragupta_desk.js — the Chitragupta chatbox, INSIDE ERPNext.
 *
 * This injects a floating assistant into the ERPNext Desk. It:
 *   - appears on every Desk page, after the user logs into ERPNext
 *   - inherits the ERP login (frappe.session.user) — no second sign-in
 *   - reads the CURRENT SCREEN (frappe.get_route) so "this" means the document
 *     the user is looking at
 *   - talks to the Chitragupta backend, which applies the governance gate:
 *     reads answer instantly; changes come back as an approval card
 *
 * INSTALL (two options, see docs/07-embed-in-erpnext.md):
 *   A) Quick (no app build): paste this file's contents into
 *      Customize > Client Script? No — use the "Website Script" / hooks route
 *      described in the doc, or simply serve it and add to app_include_js.
 *   B) Proper: ship inside a small Frappe custom app via hooks.py:
 *        app_include_js = ["/assets/chitragupta/js/chitragupta_desk.js"]
 *
 * CONFIG: set CHITRAGUPTA_URL to wherever the backend runs. It must be
 * reachable from the BROWSER (not from the ERPNext server).
 * ==========================================================================*/

(function () {
  "use strict";

  // ---- config -------------------------------------------------------------
  const CHITRAGUPTA_URL =
    window.CHITRAGUPTA_URL || "http://127.0.0.1:8000"; // backend base URL
  // Demo auth: the backend still has its own users. In production this becomes
  // an SSO/OAuth exchange where the ERP session is traded for a backend token.
  const DEMO_USER = window.CHITRAGUPTA_USER || "ravi";
  const DEMO_PASS = window.CHITRAGUPTA_PASS || "demo";

  if (window.__chitragupta_loaded) return;
  window.__chitragupta_loaded = true;

  let TOKEN = null;
  let open = false;
  let PENDING_FILE = null;

  // ---- identity from the ERP session -------------------------------------
  function erpUser() {
    try {
      return (window.frappe && frappe.session && frappe.session.user) || "unknown";
    } catch (e) {
      return "unknown";
    }
  }

  // ---- screen context: what document is the user looking at? -------------
  function screenContext() {
    try {
      const r = frappe.get_route(); // e.g. ["Form", "Purchase Order", "PUR-ORD-..."]
      if (r && r[0] === "Form" && r[1] && r[2]) {
        return { doctype: r[1], name: r[2] };
      }
      if (r && r[0] === "List" && r[1]) {
        return { doctype: r[1], name: null };
      }
    } catch (e) {}
    return null;
  }

  // ---- styles -------------------------------------------------------------
  const css = `
  #cg-panel,#cg-fab{--cg-bg:#fff;--cg-fg:#1b2330;--cg-line:#e0e5ea;
    --cg-soft:#f6f8fa;--cg-mut:#8a94a1;--cg-teal:#0e8c7f}
  [data-theme="dark"] #cg-panel,[data-theme="dark"] #cg-fab,
  .dark #cg-panel,.dark #cg-fab{--cg-bg:#1d2229;--cg-fg:#e5e9ef;
    --cg-line:#39414c;--cg-soft:#262c35;--cg-mut:#9aa4b1}
  #cg-fab{position:fixed;right:22px;bottom:22px;width:54px;height:54px;border-radius:50%;
    background:#0e8c7f;color:#fff;display:grid;place-items:center;cursor:pointer;z-index:99998;
    box-shadow:0 6px 20px rgba(0,0,0,.22);font:600 18px/1 system-ui;border:0}
  #cg-fab:hover{filter:brightness(1.08)}
  #cg-panel{position:fixed;right:22px;bottom:86px;width:min(460px,calc(100vw - 44px));max-height:min(78vh,720px);background:var(--cg-bg);
    border:1px solid var(--cg-line);border-radius:14px;display:none;flex-direction:column;z-index:99999;
    box-shadow:0 14px 44px rgba(0,0,0,.18);font:14px/1.5 system-ui;color:var(--cg-fg)}
  #cg-panel.on{display:flex}
  @media (max-width:520px){
    #cg-panel{right:8px;left:8px;bottom:74px;width:auto;max-height:82vh}
    #cg-panel.big{width:auto}
    #cg-fab{right:14px;bottom:14px}
  }
  #cg-panel.big{width:min(880px,calc(100vw - 44px));max-height:min(88vh,900px)}
  .cg-h .x{margin-left:6px;position:relative}
  #cg-badge{position:absolute;top:-4px;right:-6px;background:#d2453c;color:#fff;
    font:700 9px system-ui;border-radius:8px;padding:1px 4px;display:none}
  #cg-badge.on{display:inline-block}
  .cg-h .x:first-of-type{margin-left:auto}
  .cg-h{display:flex;align-items:center;gap:9px;padding:11px 13px;border-bottom:1px solid var(--cg-line)}
  .cg-h .g{width:26px;height:26px;border-radius:7px;background:#0e8c7f;color:#fff;display:grid;
    place-items:center;font-weight:700;font-size:13px}
  .cg-h b{font-size:13.5px}
  .cg-h small{display:block;color:var(--cg-mut);font-size:10.5px}
  .cg-h .x{margin-left:auto;cursor:pointer;border:0;background:none;color:var(--cg-mut);font-size:18px}
  .cg-ctx{margin:9px 12px 0;background:#eef6f5;border:1px solid #cfe6e2;border-radius:9px;
    padding:6px 9px;font-size:11.5px;color:#0a6f64}
  .cg-ctx b{font-family:ui-monospace,Menlo,monospace}
  .cg-body{flex:1;overflow-y:auto;overflow-x:hidden;padding:8px 12px 12px;min-height:120px;scroll-behavior:smooth}
  .cg-m{margin:9px 0;max-width:94%;min-width:0}
  .cg-m.u{margin-left:auto;text-align:right}
  .cg-b{display:inline-block;max-width:100%;padding:8px 11px;border-radius:10px;text-align:left;font-size:13px;overflow-wrap:anywhere;word-break:break-word}
  .cg-m.u .cg-b{background:#0e8c7f;color:#fff}
  .cg-m.a .cg-b{background:var(--cg-soft);border:1px solid var(--cg-line);position:relative}
  .cg-cp{position:absolute;top:4px;right:4px;opacity:0;cursor:pointer;border:0;background:var(--cg-bg);border:1px solid var(--cg-line);border-radius:5px;font-size:10px;padding:2px 6px;color:#5f6b7a;transition:opacity .15s}
  .cg-m.a:hover .cg-cp{opacity:1}
  .cg-card{border:1px solid #c77d11;border-radius:10px;margin:10px 0;overflow:hidden;font-size:12.5px}
  .cg-card .h{display:flex;padding:9px 11px;border-bottom:1px solid var(--cg-line);font-weight:600}
  .cg-badge{margin-left:auto;font-size:10px;font-weight:600;padding:2px 8px;border-radius:14px;
    background:#fbecce;color:#825107}
  .cg-badge.ok{background:#e2f3e9;color:#15662f}
  .cg-card .b{padding:9px 11px;display:grid;grid-template-columns:auto 1fr;gap:4px 12px}
  .cg-card .b .k{color:#5f6b7a}
  .cg-card .b .v{font-family:ui-monospace,Menlo,monospace}
  .cg-card .a{display:flex;gap:6px;padding:0 11px 10px}
  .cg-btn{cursor:pointer;border-radius:7px;padding:6px 11px;font:600 12px system-ui;
    border:1px solid #cfd6de;background:#fff}
  .cg-btn.ok{background:#1f9d57;color:#fff;border-color:transparent}
  .cg-res{padding:0 11px 9px;font-size:11.5px;font-weight:600;color:#15662f}
  .cg-in{border-top:1px solid var(--cg-line);padding:9px 11px;display:flex;gap:7px}
  .cg-in input{flex:1;border:1px solid var(--cg-line);border-radius:8px;padding:8px 10px;
    font:13px system-ui;background:var(--cg-soft);min-width:0}
  .cg-icon{cursor:pointer;border:1px solid var(--cg-line);background:var(--cg-bg);border-radius:8px;
    padding:0 9px;font-size:15px;line-height:1}
  .cg-icon:hover{background:#f0f2f4}
  .cg-icon.rec{background:#fbe3e1;border-color:#d2453c;animation:cgpulse 1.2s infinite}
  @keyframes cgpulse{0%,100%{opacity:1}50%{opacity:.45}}
  .cg-file{display:none;margin:0 12px 6px;padding:6px 9px;background:#eef6f5;
    border:1px solid #cfe6e2;border-radius:8px;font-size:11.5px;color:#0a6f64;
    align-items:center;gap:6px}
  .cg-file.on{display:flex}
  .cg-file button{margin-left:auto;border:0;background:none;cursor:pointer;color:#5f6b7a}
  #cg-mention{position:absolute;bottom:100%;left:0;right:0;background:var(--cg-bg);
    border:1px solid var(--cg-line);border-radius:8px;margin:0 12px 4px;display:none;
    max-height:150px;overflow-y:auto;box-shadow:0 -4px 14px rgba(0,0,0,.12)}
  #cg-mention div{padding:6px 10px;font-size:12px;cursor:pointer}
  #cg-mention div:hover{background:var(--cg-soft)}
  .cg-in button{cursor:pointer;background:#0e8c7f;color:#fff;border:0;border-radius:8px;
    padding:0 13px;font:600 13px system-ui}
  .cg-chart{margin:8px 0;padding:8px;border:1px solid var(--cg-line);
    border-radius:10px;background:var(--cg-soft)}
  .cg-chart b{font-size:11px;color:var(--cg-mut)}
  .cg-note{padding:6px 12px 10px;color:var(--cg-mut);font-size:11px}
  .cg-sug{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0 2px}
  .cg-sug button{cursor:pointer;border:1px solid #cfe6e2;background:#eef6f5;
    color:#0a6f64;border-radius:14px;padding:5px 11px;font:500 11.5px system-ui}
  .cg-sug button:hover{background:#dcefec}
  .cg-think{display:flex;gap:4px;align-items:center;padding:8px 11px}
  .cg-think span{width:6px;height:6px;border-radius:50%;background:#8a94a1;animation:cgb 1.2s infinite}
  .cg-think span:nth-child(2){animation-delay:.2s}
  .cg-think span:nth-child(3){animation-delay:.4s}
  @keyframes cgb{0%,60%,100%{opacity:.25}30%{opacity:1}}
  .cg-p{margin:0 0 6px}
  .cg-doc{color:#0e8c7f;font-family:ui-monospace,Menlo,monospace;
    text-decoration:underline;cursor:pointer}
  .cg-p:last-child{margin:0}
  .cg-hd{font-weight:700;margin:8px 0 4px;font-size:13px}
  .cg-l{margin:4px 0 6px;padding-left:18px}
  .cg-l li{margin:2px 0}
  .cg-tw{max-width:100%;overflow-x:auto;margin:6px 0;-webkit-overflow-scrolling:touch;border-radius:6px}
  .cg-t{border-collapse:collapse;width:100%;font-size:12px;min-width:max-content}
  .cg-t th,.cg-t td{border:1px solid var(--cg-line);padding:4px 7px;text-align:left}
  .cg-t th{background:var(--cg-soft);font-weight:600}
  .cg-t td:nth-child(n+2){font-family:ui-monospace,Menlo,monospace;
    text-align:right;white-space:nowrap}
  .cg-b pre{white-space:pre-wrap;overflow-wrap:anywhere;margin:4px 0}
  .cg-b code{background:#f0f2f4;padding:1px 4px;border-radius:4px;overflow-wrap:anywhere;
    font-family:ui-monospace,Menlo,monospace;font-size:12px}
  .cg-dl{display:flex;align-items:center;gap:8px;font-size:12.5px}
  .cg-dlbtn{margin-left:auto;cursor:pointer;background:#0e8c7f;color:#fff;border:0;
    border-radius:7px;padding:6px 12px;font:600 12px system-ui}
  .cg-dlbtn:hover{filter:brightness(1.08)}
  `;

  // ---- DOM ----------------------------------------------------------------
  function mount() {
    if (document.getElementById("cg-fab")) return;
    const style = document.createElement("style");
    style.textContent = css;
    document.head.appendChild(style);

    const fab = document.createElement("button");
    fab.id = "cg-fab";
    fab.title = "Chitragupta";
    fab.textContent = "चि";
    document.body.appendChild(fab);

    const panel = document.createElement("div");
    panel.id = "cg-panel";
    panel.innerHTML = `
      <div class="cg-h">
        <div class="g">चि</div>
        <div><b>Chitragupta</b><small id="cg-who">acting as you</small></div>
        <button class="x" id="cg-inbox" title="Pending approvals">✓<span id="cg-badge"></span></button>
        <button class="x" id="cg-exp" title="Expand">⤢</button>
        <button class="x" id="cg-clear" title="Clear conversation">⟲</button>
        <button class="x" id="cg-x" title="Close">×</button>
      </div>
      <div class="cg-ctx" id="cg-ctx"></div>
      <div class="cg-body" id="cg-body"></div>
      <div id="cg-file" class="cg-file"></div>
      <div id="cg-mention"></div>
      <div class="cg-in" style="position:relative">
        <button id="cg-mic" class="cg-icon" title="Speak">&#127908;</button>
        <button id="cg-clip" class="cg-icon" title="Attach a file">&#128206;</button>
        <input id="cg-input" placeholder="Ask, speak, or attach a file…  (Ctrl+K)">
        <button id="cg-send">Send</button>
      </div>
      <input type="file" id="cg-fileinput" style="display:none"
             accept=".csv,.xlsx,.xls,.pdf,.txt,.png,.jpg,.jpeg">
      <div class="cg-note">Changes are drafted and need your approval.</div>`;
    document.body.appendChild(panel);

    document.getElementById("cg-who").textContent = "acting as " + erpUser();

    fab.onclick = () => {
      open = !open;
      panel.classList.toggle("on", open);
      if (open) {
        refreshCtx();
        document.getElementById("cg-input").focus();
      }
    };
    document.getElementById("cg-inbox").onclick = async () => {
      try {
        const props = await api("/api/proposals");
        const pend = (props || []).filter((p) => p.status === "pending");
        say(pend.length
          ? "<b>Pending approvals (" + pend.length + ")</b> — review below:"
          : "No pending approvals. All clear ✅");
        pend.forEach(card);
        refreshInboxBadge();
      } catch (e) { say("Couldn't load approvals."); }
    };
    document.getElementById("cg-exp").onclick = (e) => {
      const p = document.getElementById("cg-panel");
      p.classList.toggle("big");
      e.target.textContent = p.classList.contains("big") ? "⤡" : "⤢";
      e.target.title = p.classList.contains("big") ? "Shrink" : "Expand";
    };
    document.getElementById("cg-clear").onclick = () => {
      document.getElementById("cg-body").innerHTML = "";
      try { sessionStorage.removeItem("cg_thread"); } catch (e) {}
      say("Conversation cleared. What would you like to do?");
    };
    document.getElementById("cg-x").onclick = () => {
      open = false;
      panel.classList.remove("on");
    };
    document.getElementById("cg-send").onclick = send;

    // ---- voice dictation (Web Speech API; Chrome/Edge) ----
    const mic = document.getElementById("cg-mic");
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) {
      mic.title = "Voice needs Chrome or Edge";
      mic.style.opacity = 0.4;
    } else {
      let rec = null, listening = false;
      mic.onclick = () => {
        if (listening && rec) { rec.stop(); return; }
        rec = new SR();
        rec.lang = window.CHITRAGUPTA_LANG || "en-IN";
        rec.interimResults = true;
        rec.continuous = false;
        const inp = document.getElementById("cg-input");
        rec.onstart = () => { listening = true; mic.classList.add("rec");
                              inp.placeholder = "Listening…"; };
        rec.onresult = (e) => {
          let t = "";
          for (let i = 0; i < e.results.length; i++) t += e.results[i][0].transcript;
          inp.value = t;
        };
        rec.onerror = () => { listening = false; mic.classList.remove("rec");
                              inp.placeholder = "Ask, speak, or attach a file…"; };
        rec.onend = () => {
          listening = false; mic.classList.remove("rec");
          inp.placeholder = "Ask, speak, or attach a file…";
          if (inp.value.trim()) send();      // speak -> auto-send
        };
        rec.start();
      };
    }

    // ---- file attach ----
    const clip = document.getElementById("cg-clip");
    const fileInput = document.getElementById("cg-fileinput");
    clip.onclick = () => fileInput.click();
    fileInput.onchange = () => {
      const f = fileInput.files[0];
      if (!f) return;
      PENDING_FILE = f;
      const bar = document.getElementById("cg-file");
      bar.className = "cg-file on";
      bar.innerHTML = "&#128206; " + esc(f.name) + " <button>&times;</button>";
      bar.querySelector("button").onclick = () => {
        PENDING_FILE = null; fileInput.value = ""; bar.className = "cg-file";
      };
      document.getElementById("cg-input").focus();
    };
    document.getElementById("cg-input").addEventListener("keydown", (e) => {
      if (e.key === "Enter" &&
          document.getElementById("cg-mention").style.display !== "block")
        send();
    });
    document.getElementById("cg-input").addEventListener("input", async (e) => {
      const v = e.target.value;
      const m = v.match(/@([\w.@-]*)$/);
      const box = document.getElementById("cg-mention");
      if (!m) { box.style.display = "none"; return; }
      try {
        const d = await api("/api/erp-users?q=" + encodeURIComponent(m[1]));
        if (!d.users || !d.users.length) { box.style.display = "none"; return; }
        box.innerHTML = "";
        d.users.forEach((u) => {
          const it = document.createElement("div");
          it.textContent = u;
          it.onclick = () => {
            e.target.value = v.replace(/@([\w.@-]*)$/, "@" + u + " ");
            box.style.display = "none";
            e.target.focus();
          };
          box.appendChild(it);
        });
        box.style.display = "block";
      } catch (err) { box.style.display = "none"; }
    });

    const briefKey = "cg_brief_" + new Date().toISOString().slice(0, 10);
    if (restoreThread()) {
      const b = document.getElementById("cg-body");
      b.scrollTop = b.scrollHeight;
      refreshInboxBadge();
    } else say(
      "Hi — I'm docked inside your ERP, signed in as you. Ask about the document " +
        "you're viewing, or tell me what to do. Anything that changes data comes " +
        "back for your approval."
    );

    // one-time onboarding tour
    try {
      if (!localStorage.getItem("cg_tour_done")) {
        localStorage.setItem("cg_tour_done", "1");
        say("<b>Four things to try:</b><br>" +
            "• Ask about data — <i>\"total purchases this month by supplier\"</i><br>" +
            "• Open any document and ask <i>\"what's the status of this?\"</i><br>" +
            "• Tell me to do something — <i>\"raise a PO for 100 units of X\"</i> (I draft, you approve)<br>" +
            "• 🎤 speak, 📎 attach a file, ✓ pending approvals, Ctrl+K anytime");
        chips(["total purchase orders this month",
               "what can my role do?", "show my pending approvals"]);
      }
    } catch (e) {}

    // proactive daily brief (once per day per tab-session)
    try {
      if (!sessionStorage.getItem(briefKey)) {
        sessionStorage.setItem(briefKey, "1");
        api("/api/brief").then((b) => {
          if (b && (b.greeting || (b.items && b.items.length))) {
            let html = "<b>" + esc(b.greeting || "Your day") + "</b>";
            (b.items || []).forEach((it) => { html += "<br>• " + esc(it); });
            say(html);
          }
          refreshInboxBadge();
        }).catch(() => {});
      }
    } catch (e) {}

    document.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        if (!open) fab.click();
        document.getElementById("cg-input").focus();
      }
      if (e.key === "Escape" && open) fab.click();
    });

    // clicking a document ID opens it in the ERP
    document.getElementById("cg-body").addEventListener("click", (e) => {
      const a = e.target.closest(".cg-doc");
      if (!a) return;
      e.preventDefault();
      const id = a.dataset.id;
      const c = screenContext();
      const dt = (c && c.doctype) || guessDoctype(id);
      if (window.frappe && frappe.set_route && dt) {
        frappe.set_route("Form", dt, id);
      }
    });

    // keep the context banner in sync as the user navigates the Desk
    if (window.frappe && frappe.router && frappe.router.on) {
      frappe.router.on("change", refreshCtx);
    } else {
      setInterval(refreshCtx, 1500);
    }
  }

  function guessDoctype(id) {
    const map = {"PUR-ORD": "Purchase Order", "SAL-ORD": "Sales Order",
                 "ACC-SINV": "Sales Invoice", "ACC-PINV": "Purchase Invoice",
                 "MAT-MR": "Material Request", "PUR-RFQ": "Request for Quotation"};
    for (const p in map) if (id.startsWith(p)) return map[p];
    if (/SINV|\/26-27\//.test(id)) return "Sales Invoice";
    return null;
  }

  function refreshCtx() {
    const el = document.getElementById("cg-ctx");
    if (!el) return;
    const c = screenContext();
    el.innerHTML = c
      ? `👁 I can see you're on <b>${esc(c.doctype)}${c.name ? " " + esc(c.name) : ""}</b> — ask me about "this".`
      : "👁 No document open — ask me anything.";
    if (c && !c.name) {
      const a = document.createElement("a");
      a.href = "#"; a.textContent = " analyse this list →";
      a.style.cssText = "color:#0a6f64;font-weight:600";
      a.onclick = (ev) => {
        ev.preventDefault();
        document.getElementById("cg-input").value =
          "give me a summary and totals of all " + c.doctype + " records";
        send();
      };
      el.appendChild(a);
    }
  }

  // ---- chat ---------------------------------------------------------------
  const esc = (s) =>
    String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;");

  /* Minimal markdown renderer: headings, bold, code, bullets, numbered lists
     and TABLES. Without this, model tables render as a wall of | and - .   */
  function fmt(src) {
    const lines = esc(src).split("\n");
    let out = [], i = 0;
    const docLink = (t) =>
      t.replace(/\b([A-Z]{2,}[A-Z0-9\/-]*-\d{2,}-?\d{0,}[0-9]{3,})\b/g,
        (m) => '<a class="cg-doc" href="#" data-id="' + m + '">' + m + "</a>");
    const inline = (t) =>
      docLink(t)
        .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
       .replace(/(^|[^*])\*([^*]+)\*/g, "$1<i>$2</i>")
       .replace(/`([^`]+)`/g, "<code>$1</code>");

    while (i < lines.length) {
      let ln = lines[i];

      // table: | a | b |  /  |---|---|
      if (/^\s*\|.*\|\s*$/.test(ln) && i + 1 < lines.length &&
          /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
        const cells = (r) =>
          r.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
        const head = cells(ln);
        i += 2;
        let body = [];
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
          body.push(cells(lines[i])); i++;
        }
        out.push(
          '<div class="cg-tw"><table class="cg-t"><thead><tr>' +
            head.map((h) => "<th>" + inline(h) + "</th>").join("") +
            "</tr></thead><tbody>" +
            body.map((r) => "<tr>" + r.map((c) => "<td>" + inline(c) + "</td>").join("") + "</tr>").join("") +
            "</tbody></table></div>"
        );
        continue;
      }

      // headings
      let h = ln.match(/^\s*(#{1,4})\s+(.*)$/);
      if (h) { out.push("<div class='cg-hd'>" + inline(h[2]) + "</div>"); i++; continue; }

      // bullets
      if (/^\s*[-*·]\s+/.test(ln)) {
        let items = [];
        while (i < lines.length && /^\s*[-*·]\s+/.test(lines[i])) {
          items.push("<li>" + inline(lines[i].replace(/^\s*[-*·]\s+/, "")) + "</li>");
          i++;
        }
        out.push("<ul class='cg-l'>" + items.join("") + "</ul>");
        continue;
      }

      // numbered
      if (/^\s*\d+[.)]\s+/.test(ln)) {
        let items = [];
        while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) {
          items.push("<li>" + inline(lines[i].replace(/^\s*\d+[.)]\s+/, "")) + "</li>");
          i++;
        }
        out.push("<ol class='cg-l'>" + items.join("") + "</ol>");
        continue;
      }

      if (ln.trim() === "") { i++; continue; }
      out.push("<p class='cg-p'>" + inline(ln) + "</p>");
      i++;
    }
    return out.join("");
  }

  function saveThread() {
    try {
      sessionStorage.setItem("cg_thread",
        document.getElementById("cg-body").innerHTML);
    } catch (e) {}
  }

  function restoreThread() {
    try {
      const h = sessionStorage.getItem("cg_thread");
      if (h) { document.getElementById("cg-body").innerHTML = h; return true; }
    } catch (e) {}
    return false;
  }

  function bubble(cls, html) {
    const b = document.getElementById("cg-body");
    const d = document.createElement("div");
    d.className = "cg-m " + cls;
    d.innerHTML = `<div class="cg-b">${html}</div>`;
    if (cls === "a") {
      const btn = document.createElement("button");
      btn.className = "cg-cp";
      btn.textContent = "copy";
      btn.onclick = () => {
        const t = d.querySelector(".cg-b").innerText.replace(/^copy\n?/, "");
        navigator.clipboard.writeText(t);
        btn.textContent = "copied";
        setTimeout(() => (btn.textContent = "copy"), 1200);
      };
      d.querySelector(".cg-b").appendChild(btn);
      const up = document.createElement("button");
      up.className = "cg-cp"; up.style.right = "44px"; up.textContent = "👍";
      const dn = document.createElement("button");
      dn.className = "cg-cp"; dn.style.right = "76px"; dn.textContent = "👎";
      const send_fb = (rating, reason) => api("/api/feedback", {
        method: "POST",
        body: JSON.stringify({
          reply_excerpt: d.querySelector(".cg-b").innerText.slice(0, 200),
          rating: rating, reason: reason || null })}).catch(() => {});
      up.onclick = () => { send_fb("up"); up.textContent = "✓"; };
      dn.onclick = () => {
        const why = window.prompt("What was wrong? (helps Chitragupta learn)");
        send_fb("down", why); dn.textContent = "✓";
      };
      d.querySelector(".cg-b").appendChild(up);
      d.querySelector(".cg-b").appendChild(dn);
    }
    b.appendChild(d);
    b.scrollTop = b.scrollHeight;
  }
  const say = (h) => bubble("a", h);

  function thinking(on) {
    const b = document.getElementById("cg-body");
    const old = document.getElementById("cg-thinking");
    if (old) old.remove();
    if (!on) return;
    const d = document.createElement("div");
    d.id = "cg-thinking";
    d.className = "cg-m a";
    d.innerHTML = '<div class="cg-b"><div class="cg-think">' +
                  '<span></span><span></span><span></span></div></div>';
    b.appendChild(d);
    b.scrollTop = b.scrollHeight;
  }
  const me = (t) => {
    const b = document.getElementById("cg-body");
    const d = document.createElement("div");
    d.className = "cg-m u";
    d.innerHTML = '<div class="cg-b">' + esc(t) + "</div>";
    d.title = "Click to edit & resend";
    d.style.cursor = "pointer";
    d.onclick = () => {
      const inp = document.getElementById("cg-input");
      inp.value = t; inp.focus();
    };
    b.appendChild(d); b.scrollTop = b.scrollHeight; saveThread();
  };

  async function refreshInboxBadge() {
    try {
      const props = await api("/api/proposals");
      const n = (props || []).filter((p) => p.status === "pending").length;
      const b = document.getElementById("cg-badge");
      if (b) { b.textContent = n; b.className = n ? "on" : ""; }
    } catch (e) {}
  }

  async function login() {
    // ACT-AS-USER (Phase A): ask the ERP to vouch for the logged-in user via
    // the chitragupta_desk app's whitelisted method (runs server-side AS the
    // session user), then exchange the signed assertion at the backend.
    try {
      const a = await fetch(
        "/api/method/chitragupta_desk.api.get_session_assertion",
        { headers: { "X-Frappe-CSRF-Token":
            (window.frappe && frappe.csrf_token) || "" } });
      if (a.ok) {
        const assertion = (await a.json()).message;
        // Phase B: pass the ERP session so the backend can act AS this user
        // (the backend verifies the sid really belongs to the asserted user).
        try {
          // server-provided sid/csrf (works even when cookies are HttpOnly);
          // cookie read is only the fallback for older api.py versions
          assertion.sid = assertion.sid ||
            (window.frappe && frappe.get_cookie &&
             frappe.get_cookie("sid")) || null;
          assertion.csrf = assertion.csrf ||
            (window.frappe && frappe.csrf_token) || null;
        } catch (e) { /* Phase A only */ }
        const r = await fetch(CHITRAGUPTA_URL + "/api/sso", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(assertion),
        });
        if (r.ok) {
          const d = await r.json();
          TOKEN = d.token;
          if (d.acting_mode !== "user")
            console.info("[chitragupta] acting via bot:", d.acting_reason);
          const who = document.getElementById("cg-who");
          if (who) who.textContent = "acting as " + (d.full_name || d.user) +
            (d.roles && d.roles.length ? " · " + d.roles.slice(0, 2).join(", ") : "") +
            (d.acting_mode === "user" ? "" : " · via bot");
          return;
        }
      }
    } catch (e) { /* SSO unavailable -> demo fallback below */ }

    // Fallback (pilot/demo only): backend demo user.
    const r = await fetch(CHITRAGUPTA_URL + "/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user: DEMO_USER, password: DEMO_PASS }),
    });
    if (!r.ok) throw new Error("backend login failed");
    TOKEN = (await r.json()).token;
    const who = document.getElementById("cg-who");
    if (who) who.textContent = "acting as " + DEMO_USER + " (demo fallback)";
  }

  async function api(path, opts) {
    if (!TOKEN) await login();
    opts = opts || {};
    opts.headers = Object.assign(
      { "Content-Type": "application/json", Authorization: "Bearer " + TOKEN },
      opts.headers || {}
    );
    const r = await fetch(CHITRAGUPTA_URL + path, opts);
    return r.json();
  }

  function card(p) {
    const b = document.getElementById("cg-body");
    const el = document.createElement("div");
    el.className = "cg-card";
    // rich diff for update proposals: old → new per field
    if (p.action === "update" && p.fields && p.fields.patch) {
      const cur = p.fields.current || {};
      const rows2 = Object.entries(p.fields.patch).map(([k, v]) =>
        '<div class="k">' + esc(k) + '</div><div class="v">' +
        '<s style="opacity:.55">' + esc(cur[k]) + "</s> → <b>" + esc(v) +
        "</b></div>").join("");
      el.className = "cg-card";
      el.innerHTML =
        '<div class="h">' + esc(p.summary) +
        '<span class="cg-badge">Approve?</span></div>' +
        '<div class="b">' + rows2 + "</div>" +
        '<div class="a"><button class="cg-btn ok">Approve</button>' +
        '<button class="cg-btn">Reject</button></div>';
      wireCard(el, p);
      b.appendChild(el); b.scrollTop = b.scrollHeight; saveThread();
      return;
    }
    const rows = Object.entries(p.fields || {})
      .filter(([k]) => !["name", "docstatus"].includes(k))
      .map(
        ([k, v]) =>
          `<div class="k">${esc(k)}</div><div class="v">${esc(v)}</div>`
      )
      .join("");
    el.innerHTML = `
      <div class="h">${esc(p.summary)}<span class="cg-badge">Approve?</span></div>
      <div class="b">${rows}</div>
      <div class="a"><button class="cg-btn ok">Approve</button>
      <button class="cg-btn">Reject</button></div>`;
    b.appendChild(el);
    b.scrollTop = b.scrollHeight;

    wireCard(el, p);
    saveThread();
  }

  function wireCard(el, p) {
    el.querySelector(".cg-btn.ok").onclick = async () => {
      const r = await api(`/api/proposals/${p.id}/approve`, {
        method: "POST",
        body: JSON.stringify({}),
      });
      if (r.error) {
        say("<b>Cannot approve:</b> " + esc(r.error));
        return;
      }
      const badge = el.querySelector(".cg-badge");
      badge.className = "cg-badge ok";
      badge.textContent = "Done";
      el.querySelector(".a").style.display = "none";
      el.insertAdjacentHTML(
        "beforeend",
        `<div class="cg-res">✓ ${esc(r.submitted)} — read-back ${
          r.readback_ok ? "verified" : "FAILED"
        }</div>`
      );
      // refresh the Desk so the user sees the new document immediately
      if (window.frappe && frappe.set_route && r.submitted) {
        say(
          `Submitted as <b>${esc(r.submitted)}</b>. ` +
            `<a href="/app/${p.doctype.toLowerCase().replace(/ /g, "-")}/${encodeURIComponent(
              r.submitted
            )}">Open it →</a>`
        );
      }
    };
    el.querySelector(".a .cg-btn:not(.ok)").onclick = async () => {
      await api(`/api/proposals/${p.id}/reject`, {
        method: "POST",
        body: JSON.stringify({}),
      });
      const badge = el.querySelector(".cg-badge");
      badge.textContent = "Rejected";
      el.querySelector(".a").style.display = "none";
      el.insertAdjacentHTML(
        "beforeend",
        `<div class="cg-res" style="color:#5f6b7a">Rejected — nothing written.</div>`
      );
      refreshInboxBadge();
    };
  }

  function readFile(f) {
    return new Promise((res, rej) => {
      const r = new FileReader();
      r.onload = () => res(String(r.result).split(",")[1]);   // base64 payload
      r.onerror = rej;
      r.readAsDataURL(f);
    });
  }

  function chips(list) {
    const b = document.getElementById("cg-body");
    const wrap = document.createElement("div");
    wrap.className = "cg-sug";
    list.slice(0, 3).forEach((t) => {
      const btn = document.createElement("button");
      btn.textContent = t;
      btn.onclick = () => {
        wrap.remove();
        document.getElementById("cg-input").value = t;
        send();
      };
      wrap.appendChild(btn);
    });
    b.appendChild(wrap);
    b.scrollTop = b.scrollHeight;
  }

  function chartCard(ch) {
    if (!ch || !ch.labels || !ch.labels.length) return;
    const max = Math.max.apply(null, ch.values) || 1;
    const W = 380, BH = 16, GAP = 6, LW = 120;
    const H = ch.labels.length * (BH + GAP) + 4;
    let bars = "";
    ch.labels.forEach((lb, i) => {
      const y = i * (BH + GAP);
      const w = Math.max(2, (ch.values[i] / max) * (W - LW - 60));
      bars += '<text x="' + (LW - 6) + '" y="' + (y + BH - 4) +
        '" font-size="10" text-anchor="end" fill="currentColor">' + esc(lb) +
        '</text><rect x="' + LW + '" y="' + y + '" width="' + w +
        '" height="' + BH + '" rx="3" fill="#0e8c7f" opacity="0.85"></rect>' +
        '<text x="' + (LW + w + 4) + '" y="' + (y + BH - 4) +
        '" font-size="10" fill="currentColor">' +
        Number(ch.values[i]).toLocaleString("en-IN") + "</text>";
    });
    const b = document.getElementById("cg-body");
    const d = document.createElement("div");
    d.className = "cg-chart";
    d.innerHTML = "<b>" + esc(ch.title || "") + "</b>" +
      '<svg viewBox="0 0 ' + W + " " + H + '" width="100%" height="' + H +
      '" style="display:block;color:var(--cg-fg)">' + bars + "</svg>";
    b.appendChild(d);
    b.scrollTop = b.scrollHeight;
    saveThread();
  }

  function downloadCard(dl) {
    const b = document.getElementById("cg-body");
    const wrap = document.createElement("div");
    wrap.className = "cg-m a";
    wrap.innerHTML =
      '<div class="cg-b"><div class="cg-dl">&#128196; ' + esc(dl.filename) +
      '<button class="cg-dlbtn">Download</button></div></div>';
    b.appendChild(wrap);
    b.scrollTop = b.scrollHeight;
    wrap.querySelector(".cg-dlbtn").onclick = () => {
      const bytes = atob(dl.b64);
      const arr = new Uint8Array(bytes.length);
      for (let i = 0; i < bytes.length; i++) arr[i] = bytes.charCodeAt(i);
      const blob = new Blob([arr], { type: dl.mimetype });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = dl.filename; a.click();
      URL.revokeObjectURL(url);
    };
  }

  async function send() {
    const inp = document.getElementById("cg-input");
    const text = inp.value.trim();
    if (!text && !PENDING_FILE) return;
    inp.value = "";
    let filePart = null;
    if (PENDING_FILE) {
      me((text || "(attached)") + "  📎 " + PENDING_FILE.name);
      try {
        filePart = { name: PENDING_FILE.name, type: PENDING_FILE.type,
                     data: await readFile(PENDING_FILE) };
      } catch (e) { say("I couldn't read that file."); }
      PENDING_FILE = null;
      document.getElementById("cg-fileinput").value = "";
      document.getElementById("cg-file").className = "cg-file";
    } else {
      me(text);
    }
    thinking(true);
    try {
      if (!TOKEN) await login();
      const resp = await fetch(CHITRAGUPTA_URL + "/api/command/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json",
                   Authorization: "Bearer " + TOKEN },
        body: JSON.stringify({ text, context: screenContext(), file: filePart }),
      });
      if (!resp.ok || !resp.body) throw new Error("stream unavailable");
      const reader = resp.body.getReader();
      const dec = new TextDecoder();
      let buf = "", full = "", meta = null, liveEl = null;
      const liveBubble = () => {
        if (liveEl) return liveEl;
        const b = document.getElementById("cg-body");
        const d = document.createElement("div");
        d.className = "cg-m a";
        d.innerHTML = '<div class="cg-b"></div>';
        b.appendChild(d);
        liveEl = d.querySelector(".cg-b");
        return liveEl;
      };
      thinking(false); thinking(true);
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const line = buf.slice(0, idx).trim();
          buf = buf.slice(idx + 2);
          if (!line.startsWith("data:")) continue;
          let ev;
          try { ev = JSON.parse(line.slice(5)); } catch (e) { continue; }
          if (ev.type === "meta") meta = ev;
          else if (ev.type === "delta") {
            thinking(false);
            full += ev.text;
            liveBubble().innerHTML = fmt(full);
            const bb = document.getElementById("cg-body");
            bb.scrollTop = bb.scrollHeight;
          }
        }
      }
      thinking(false);
      if (liveEl) { liveEl.innerHTML = fmt(full); saveThread(); }
      else if (full) say(fmt(full));
      const r = meta || {};
      if (r.chart) chartCard(r.chart);
      if (r.suggestions && r.suggestions.length) chips(r.suggestions);
      if (r.download) downloadCard(r.download);
      (r.proposals || []).forEach(card);
      refreshInboxBadge();
    } catch (e) {
      thinking(false);
      say(
        "I can't reach the Chitragupta backend at " +
          esc(CHITRAGUPTA_URL) +
          ". Is it running, and is CORS allowed for this ERP origin?"
      );
    }
  }

  // ---- boot ---------------------------------------------------------------
  if (document.readyState === "complete" || document.readyState === "interactive") {
    setTimeout(mount, 800);
  } else {
    document.addEventListener("DOMContentLoaded", () => setTimeout(mount, 800));
  }
})();
