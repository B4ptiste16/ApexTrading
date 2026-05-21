"""
APEX Auth Server — Web dashboard  (V7.1+, scaffold)
─────────────────────────────────────────────────────────────────────
Browser-accessible dashboard so a user can check status from a phone
or any machine without launching the desktop app. Pure HTML+JS — no
build step, no framework, intentionally lightweight so it serves fast
even on the Always-Free ARM/AMD VM.

Endpoints (mounted by server/app.py):
  GET  /                  → landing (redirects to /dashboard if logged in,
                            else /login)
  GET  /login             → HTML login form (posts to /auth/login)
  GET  /dashboard         → portfolio overview (requires auth cookie)
  GET  /static/app.css    → styles

Sessions on the web side use a signed cookie holding the same JWT
the desktop app uses, so a single signup works for both.
"""

from fastapi import Request
from fastapi.responses import HTMLResponse

from . import auth, database


# ── HTML templates ────────────────────────────────────────────────────

_BASE_CSS = """
:root {
  --bg:    #0c0f16; --bg2: #131a2a; --panel: #111622;
  --panel2:#181f2e; --border:#232d40; --text:#d8dde8;
  --muted: #5c6b82; --green:#3fb89a; --red:#c75c6b;
  --purple:#8a93c9;
}
* { box-sizing: border-box; }
html, body {
  margin: 0; padding: 0;
  background: linear-gradient(135deg, var(--bg), var(--bg2));
  color: var(--text);
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  min-height: 100vh;
}
.wrap   { max-width: 720px; margin: 0 auto; padding: 24px 16px; }
.brand  { font-family: 'Syne', sans-serif; font-weight: 800;
          color: var(--green); letter-spacing: 6px; font-size: 22px; }
.sub    { color: var(--muted); font-size: 9px; letter-spacing: 5px;
          margin-bottom: 28px; }
.card   { background: var(--panel); border: 1px solid var(--border);
          border-radius: 12px; padding: 24px; margin-bottom: 16px; }
h2      { margin: 0 0 12px; font-size: 14px; letter-spacing: 3px;
          color: var(--text); font-family: 'Syne', sans-serif; }
label   { display:block; color: var(--muted); font-size:10px;
          letter-spacing:1px; margin: 12px 0 4px; }
input   { width: 100%; height: 40px; padding: 0 14px;
          background: var(--panel2); color: var(--text);
          border: 1px solid var(--border); border-radius: 6px;
          font-family: inherit; font-size: 12px; }
input:focus { outline: none; border-color: var(--green); }
button  { width: 100%; height: 44px; background: var(--green);
          color: var(--bg); border: none; border-radius: 7px;
          font-family: inherit; font-weight: 700; letter-spacing: 3px;
          font-size: 11px; cursor: pointer; margin-top: 16px; }
button:hover { background: #4dcca8; }
.err    { color: var(--red); font-size: 11px; padding: 10px;
          background: rgba(199,92,107,0.10);
          border: 1px solid rgba(199,92,107,0.30);
          border-radius: 5px; margin-bottom: 12px; }
.stat   { display:flex; justify-content:space-between;
          padding: 10px 0; border-bottom: 1px solid var(--border); }
.stat:last-child { border-bottom: none; }
.stat .k { color: var(--muted); font-size:10px; letter-spacing:2px; }
.stat .v { font-weight: 700; }
.pos    { color: var(--green); }
.neg    { color: var(--red); }
a       { color: var(--purple); }
@media (max-width: 480px) {
  .wrap { padding: 16px 12px; }
  .card { padding: 18px; }
}
"""


def login_page(error: str | None = None) -> HTMLResponse:
    err_html = f'<div class="err">{error}</div>' if error else ""
    body = f"""<!doctype html>
<html><head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>APEX — Sign in</title>
  <style>{_BASE_CSS}</style>
</head><body>
  <div class="wrap">
    <div class="brand">◈  APEX</div>
    <div class="sub">TRADING PLATFORM</div>
    <div class="card">
      <h2>SIGN IN</h2>
      {err_html}
      <form method="post" action="/web/login">
        <label>Email or username</label>
        <input name="identifier" autocomplete="username" required>
        <label>Password</label>
        <input name="password" type="password"
               autocomplete="current-password" required>
        <button type="submit">LOG IN</button>
      </form>
      <p style="color:var(--muted);font-size:11px;margin-top:18px;">
        No account?  <a href="/web/signup">Create one →</a>
      </p>
    </div>
  </div>
</body></html>"""
    return HTMLResponse(body, status_code=401 if error else 200)


def signup_page(error: str | None = None,
                prefill: dict | None = None) -> HTMLResponse:
    err_html = f'<div class="err">{error}</div>' if error else ""
    pf = prefill or {}
    body = f"""<!doctype html>
<html><head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>APEX — Create account</title>
  <style>{_BASE_CSS}</style>
</head><body>
  <div class="wrap">
    <div class="brand">◈  APEX</div>
    <div class="sub">TRADING PLATFORM</div>
    <div class="card">
      <h2>CREATE ACCOUNT</h2>
      {err_html}
      <form method="post" action="/web/signup">
        <label>Display name</label>
        <input name="display_name" autocomplete="name"
               value="{pf.get('display_name','')}">
        <label>Email address</label>
        <input name="email" type="email" autocomplete="email"
               value="{pf.get('email','')}" required>
        <label>Username  (optional)</label>
        <input name="username" autocomplete="username"
               value="{pf.get('username','')}">
        <label>Password  (min. 8 characters)</label>
        <input name="password" type="password"
               autocomplete="new-password" required>
        <label>Confirm password</label>
        <input name="password2" type="password"
               autocomplete="new-password" required>
        <button type="submit">CREATE ACCOUNT</button>
      </form>
      <p style="color:var(--muted);font-size:11px;margin-top:18px;">
        Already have an account?  <a href="/web/login">Log in →</a>
      </p>
    </div>
  </div>
</body></html>"""
    return HTMLResponse(body, status_code=400 if error else 200)


def dashboard_page(user: dict) -> HTMLResponse:
    name = user.get("display_name") or user.get("username") or "user"
    # V7.1.1: ship a richer dashboard with per-bot stats, start/stop,
    # and emergency liquidate. Numbers and actions are wired to
    # /web/api/* endpoints; the JS polls every 30s. When no broker keys
    # are linked yet, every stat shows "—" and actions show a friendly
    # hint to head to the desktop app's Tools tab.
    body = f"""<!doctype html>
<html><head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>APEX — Dashboard</title>
  <style>{_BASE_CSS}
    .botcard {{ margin-bottom: 14px; }}
    .botcard .head {{ display:flex; align-items:center; gap:8px;
                       margin-bottom: 10px; }}
    .botcard .dot {{ width:8px; height:8px; border-radius:4px;
                      background: var(--muted); display:inline-block; }}
    .dot.run    {{ background: var(--green); animation: pulse 1.4s ease-in-out infinite; }}
    .dot.stop   {{ background: var(--muted); }}
    .actions    {{ display: flex; gap: 8px; margin-top: 12px;
                    flex-wrap: wrap; }}
    .btn        {{ flex: 1; min-width: 110px; height: 38px;
                    background: var(--panel2); color: var(--text);
                    border: 1px solid var(--border); border-radius: 6px;
                    font-family: inherit; font-size: 10px;
                    letter-spacing: 2px; cursor: pointer; }}
    .btn:hover  {{ border-color: var(--muted); }}
    .btn.go     {{ color: var(--green);
                    border-color: rgba(63,184,154,0.45); }}
    .btn.stop   {{ color: var(--red);
                    border-color: rgba(199,92,107,0.45); }}
    .btn.liq    {{ color: var(--purple);
                    border-color: rgba(138,147,201,0.45); }}
    .btn:disabled {{ color: var(--muted); cursor: not-allowed;
                      border-color: var(--border); }}
    .hint       {{ color: var(--muted); font-size: 10px;
                    margin-top: 10px; line-height: 1.5; }}
    @keyframes pulse {{
      0%   {{ opacity: 1; }}
      50%  {{ opacity: 0.25; }}
      100% {{ opacity: 1; }}
    }}
  </style>
</head><body>
  <div class="wrap">
    <div class="brand">◈  APEX</div>
    <div class="sub">TRADING PLATFORM</div>

    <div class="card">
      <h2>WELCOME, {name.upper()}</h2>
      <div class="stat"><span class="k">TOTAL EQUITY</span>
        <span class="v" id="total">—</span></div>
      <div class="stat"><span class="k">TODAY'S P/L</span>
        <span class="v" id="today">—</span></div>
      <div class="stat"><span class="k">SERVER</span>
        <span class="v" id="srv">checking…</span></div>
    </div>

    <div id="bots"></div>

    <div class="card">
      <h2>ACCOUNT</h2>
      <div class="stat"><span class="k">USERNAME</span>
        <span class="v">{user.get("username","")}</span></div>
      <div class="stat"><span class="k">EMAIL</span>
        <span class="v">{user.get("email","")}</span></div>
      <p style="margin-top:18px;">
        <a href="/web/logout">Sign out</a>
      </p>
    </div>
  </div>

  <script>
    const SIDES = ["LONG", "SHORT", "DAY"];
    const fmt$ = n =>
      n == null ? "—"
        : (n >= 0 ? "+$" : "-$") +
          Math.abs(n).toLocaleString(undefined,
            {{minimumFractionDigits: 2, maximumFractionDigits: 2}});
    const fmtPct = n => n == null ? "—" :
      (n >= 0 ? "+" : "") + n.toFixed(2) + "%";

    function renderBots(state) {{
      const c = document.getElementById("bots");
      c.innerHTML = "";
      let total = 0, today = 0, haveAny = false;
      for (const side of SIDES) {{
        const s = state.bots[side] || {{}};
        if (s.equity != null) {{ total += s.equity; haveAny = true; }}
        if (s.today_pl != null) {{ today += s.today_pl; }}
        const running = s.running === true;
        const dotCls = running ? "dot run" : "dot stop";
        const dis = (state.linked === false) ? "disabled" : "";
        c.insertAdjacentHTML("beforeend", `
          <div class="card botcard">
            <div class="head">
              <span class="${{dotCls}}"></span>
              <h2 style="margin:0;flex:1;">${{side}}</h2>
              <span class="v" style="font-size:12px;">${{fmt$(s.equity)}}</span>
            </div>
            <div class="stat"><span class="k">TODAY P/L</span>
              <span class="v ${{(s.today_pl||0) >= 0 ? 'pos' : 'neg'}}">
                ${{fmt$(s.today_pl)}} (${{fmtPct(s.today_pct)}})</span></div>
            <div class="stat"><span class="k">POSITIONS</span>
              <span class="v">${{s.positions == null ? '—' : s.positions}}</span></div>
            <div class="stat"><span class="k">STATUS</span>
              <span class="v">${{running ? 'RUNNING' : 'STOPPED'}}</span></div>
            <div class="actions">
              <button class="btn go" ${{running || dis ? 'disabled' : ''}}
                      onclick="act('${{side}}','start')">▶  START</button>
              <button class="btn stop" ${{!running || dis ? 'disabled' : ''}}
                      onclick="act('${{side}}','stop')">■  STOP</button>
              <button class="btn liq" ${{dis ? 'disabled' : ''}}
                      onclick="liq('${{side}}')">⚠  LIQUIDATE</button>
            </div>
          </div>
        `);
      }}
      document.getElementById("total").textContent = haveAny ? fmt$(total) : "—";
      const t = document.getElementById("today");
      t.textContent = haveAny ? fmt$(today) : "—";
      t.className = "v " + (today >= 0 ? "pos" : "neg");
      if (state.linked === false) {{
        c.insertAdjacentHTML("afterbegin", `
          <div class="card">
            <h2>NO BROKER LINKED</h2>
            <p class="hint">Open the desktop app → Tools → API Keys, fill
            in your Alpaca keys, then click <b>Sync keys to APEX server</b>
            under ACCOUNT LINKING. Refresh this page once that's done.</p>
          </div>`);
      }}
    }}

    async function refresh() {{
      try {{
        const r = await fetch("/web/api/status");
        const j = await r.json();
        document.getElementById("srv").textContent = "OK";
        document.getElementById("srv").className = "v pos";
        renderBots(j);
      }} catch (e) {{
        document.getElementById("srv").textContent = "OFFLINE";
        document.getElementById("srv").className = "v neg";
      }}
    }}

    async function act(side, cmd) {{
      if (!confirm(`${{cmd.toUpperCase()}} ${{side}} bot?`)) return;
      try {{
        const r = await fetch(`/web/api/bots/${{side}}/${{cmd}}`,
                              {{ method: "POST" }});
        const j = await r.json();
        if (!r.ok) alert(j.detail || "Action failed.");
      }} catch (e) {{ alert(e.message); }}
      refresh();
    }}

    async function liq(side) {{
      if (!confirm(
        `LIQUIDATE all positions held by ${{side}}?\\n\\n` +
        `This sells every open position immediately at market. ` +
        `It cannot be undone.`)) return;
      try {{
        const r = await fetch(`/web/api/bots/${{side}}/liquidate`,
                              {{ method: "POST" }});
        const j = await r.json();
        alert(j.detail || (r.ok ? "Liquidate request sent."
                                 : "Liquidate failed."));
      }} catch (e) {{ alert(e.message); }}
      refresh();
    }}

    refresh();
    setInterval(refresh, 30000);
  </script>
</body></html>"""
    return HTMLResponse(body)


def user_from_cookie(request: Request) -> dict | None:
    """Look up the current web session user from the apex_token cookie."""
    tok = request.cookies.get("apex_token")
    if not tok:
        return None
    payload = auth.verify_token(tok)
    if not payload:
        return None
    user = database.get_user_by_id(int(payload["sub"]))
    if not user:
        return None
    return {k: v for k, v in user.items() if k != "hashed_password"}
