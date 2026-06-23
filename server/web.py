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
html {
  background: var(--bg);
}
body {
  margin: 0;
  padding: 0;
  padding-top: env(safe-area-inset-top);
  padding-bottom: env(safe-area-inset-bottom);
  background: var(--bg);
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


# V4.0.1 — footer injected on every page. Single auth cookie banner +
# T&C link + copyright. The banner dismisses with a localStorage flag.
_FOOTER = """
<div id="cookie-banner" style="
     position:fixed;left:0;right:0;bottom:0;background:rgba(10,13,18,0.96);
     border-top:1px solid #2a3447;color:#d8dde8;
     font-family:'JetBrains Mono',monospace;font-size:11px;line-height:1.5;
     padding:12px 18px;display:none;z-index:9999;">
  <span>This site uses a single auth cookie (<code>apex_token</code>) to keep you signed in. No tracking.
        See our <a href="/web/tos" style="color:#3eb8a4;">Terms &amp; Conditions</a>.</span>
  <button onclick="document.getElementById('cookie-banner').style.display='none';
                   localStorage.setItem('baptou_cookies_ack','1');"
          style="float:right;background:#3eb8a4;color:#0c0f16;border:none;
                 padding:5px 14px;border-radius:4px;font-weight:700;
                 cursor:pointer;letter-spacing:2px;font-size:10px;">GOT IT</button>
</div>
<footer style="text-align:center;padding:30px 16px 60px;color:#5a6478;
        font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:2px;">
  BAPTOU TRADING PLATFORM  ·  <a href="/web/tos" style="color:#5a6478;">TERMS &amp; CONDITIONS</a>  ·  © 2026
</footer>
<script>
  if (!localStorage.getItem('baptou_cookies_ack')) {
    document.getElementById('cookie-banner').style.display = 'block';
  }
</script>
"""


_TOS_BODY = '''BAPTOU TRADING PLATFORM - TERMS & CONDITIONS  (v1.0)

By using BAPTOU Trading Platform (the "App"), you agree to the following:

1. AS-IS SERVICE
The App is provided as-is, without warranty of any kind. Trading bot
performance is not a guarantee of future results. Markets are volatile;
past performance is not indicative of future returns.

2. ASSUMPTION OF RISK
You assume all risk for trades executed by bots running through your
linked broker accounts. You authorise the App to place orders on your
behalf in accordance with each bot's logic. You are solely responsible
for monitoring your bots and your broker account.

3. NO LIABILITY FOR LOSSES (general rule)
Neither the App, its creators, contributors, hosts, nor any affiliated
parties shall be liable for any monetary losses, missed gains, slippage,
broker downtime, or other financial damages incurred while using the
App or any bot purchased / downloaded through it.

4. MODERATION REFUND POLICY (the one exception)
If a published bot is flagged and removed by App moderators for
misrepresenting its behaviour, every buyer of that bot is refunded
the credits they paid for it. This refund covers the purchase price
only. The App is NOT liable for trading losses incurred while running
such a bot before its removal -- only for the sale itself.

5. CREDITS
APEX / BAPTOU credits are a virtual currency used internally for
marketplace transactions. 1 credit conventionally = US $0.01. Cash-out
becomes available when the App connects to a real banking provider
in a future release.

6. PUBLISHER OBLIGATIONS
If you publish a bot, you warrant that you own or have the right to
distribute the code, that your published description matches the bot's
actual behaviour, and that you do not knowingly distribute malicious
code. Misrepresentation may result in moderation removal, refunds to
buyers, a publish ban, and a fixed credit fine.

7. ACCOUNT TERMINATION
The App reserves the right to suspend or terminate accounts that
violate these terms or applicable laws.

8. PRIVACY
The App stores email, username, hashed password, optional Google sub
identifier, optional phone, and your encrypted broker API keys. Your
bot code, AI generation prompts, and trade logs are stored on your
local machine and (when cloud-running) on the App's server.

9. GOVERNING LAW
These terms are governed by the laws of the user's home jurisdiction
where mandatory, otherwise by the laws of the App operator's home
country.'''


def tos_page() -> HTMLResponse:
    """V4.0.1 — public T&C page. Linked from every footer + the cookie
    banner. Plain str.format so we don't hit Python 3.11's
    f-string-no-backslash rule."""
    import html as _html
    body = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<title>BAPTOU - Terms &amp; Conditions</title>
<style>
  :root { --bg:#0c0f16; --panel:#10141b; --border:#2a3447; --text:#d6dce6;
           --muted:#5a6478; --green:#3eb8a4; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
          font-family:'JetBrains Mono',monospace; font-size:13px; line-height:1.8; }
  nav { display:flex; align-items:center; padding:18px 32px;
         border-bottom:1px solid var(--border); }
  .brand { font-family:'Syne',sans-serif; font-weight:800;
            color:var(--green); letter-spacing:4px; font-size:14px;
            text-decoration:none; }
  main { max-width:760px; margin:40px auto; padding:0 32px; }
  h1 { font-family:'Syne',sans-serif; letter-spacing:4px; font-size:24px;
        color:var(--text); margin-bottom:6px; }
  pre { white-space:pre-wrap; word-wrap:break-word;
         background:var(--panel); border:1px solid var(--border);
         padding:24px; border-radius:8px; color:var(--text); }
</style>
</head><body>
  <nav><a class="brand" href="/">""" + "◈" + """ BAPTOU</a></nav>
  <main>
    <h1>TERMS &amp; CONDITIONS</h1>
    <p style="color:var(--muted);">v1.0  &middot;  Effective immediately for every account</p>
    <pre>""" + _html.escape(_TOS_BODY) + """</pre>
    <p style="color:var(--muted);margin-top:24px;">
      By using the desktop app you accept these terms via a modal on
      first launch. By using the web dashboard you accept them implicitly
      while signed in. <a href="/" style="color:var(--green);">Back to home</a>.
    </p>
  </main>
  """ + _FOOTER + """
</body></html>"""
    return HTMLResponse(body)


def landing_page(signed_in_user: dict | None = None) -> HTMLResponse:
    """
    V7.1.14: public marketing homepage at /. Pre-V7.1.14 the bare URL
    redirected straight to /web/login, which dead-ended new visitors
    who didn't already have credentials. Now hits land on this page,
    which has:
      - APEX branding + a short pitch
      - a big DOWNLOAD button linking to the latest GitHub release's
        APEX_Setup.exe (GitHub auto-redirects /releases/latest/...)
      - SIGN IN / SIGN UP buttons in the top-right (or the user's
        display name + 'Open dashboard' if they're already
        authenticated via the apex_token cookie)
    """
    # Top-right CTA changes depending on session
    if signed_in_user:
        name = (signed_in_user.get("display_name")
                or signed_in_user.get("username") or "you")
        topright = f"""
          <span style="color:var(--muted);font-size:11px;">
            Signed in as <b style="color:var(--text);">{name}</b>
          </span>
          <a class="btn btn-sm primary" href="/web/dashboard">Open dashboard</a>
        """
    else:
        topright = """
          <a class="btn btn-sm ghost"   href="/web/login">Sign in</a>
          <a class="btn btn-sm primary" href="/web/signup">Sign up</a>
        """

    body = f"""<!doctype html>
<html><head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="theme-color" content="#0c0f16">
  <title>BAPTOU — Trading platform</title>
  <style>{_BASE_CSS}
    nav      {{ display:flex; align-items:center; gap:10px;
                padding: 14px 20px; }}
    nav .sp  {{ flex:1; }}
    .btn-sm  {{ height: 34px; padding: 0 16px; font-size: 10px;
                letter-spacing: 2px; font-weight: 600;
                border-radius: 6px; cursor: pointer;
                font-family: inherit; display:inline-flex;
                align-items:center; text-decoration:none; }}
    .ghost   {{ color: var(--muted); border: 1px solid var(--border);
                background: transparent; }}
    .ghost:hover   {{ color: var(--text); border-color: var(--muted); }}
    .primary {{ color: var(--bg); background: var(--green);
                border: 1px solid var(--green); }}
    .primary:hover {{ background: #6db89e; }}

    .hero    {{ max-width: 760px; margin: 60px auto 0;
                padding: 0 16px; text-align: center; }}
    .hero .brand {{ font-family: 'Syne', sans-serif; font-weight: 800;
                    color: var(--green); letter-spacing: 8px;
                    font-size: 44px; margin-bottom: 4px; }}
    .hero .sub   {{ color: var(--muted); font-size: 11px;
                    letter-spacing: 6px; margin-bottom: 30px; }}
    .hero h1     {{ font-size: 28px; line-height: 1.3;
                    margin: 0 0 14px; font-family: 'Syne', sans-serif; }}
    .hero p      {{ color: var(--muted); font-size: 13px;
                    line-height: 1.7; max-width: 540px;
                    margin: 0 auto 36px; }}
    .download {{ display: inline-block; height: 64px;
                  padding: 0 36px; line-height: 64px;
                  font-family: inherit; font-size: 13px;
                  letter-spacing: 4px; font-weight: 700;
                  color: var(--bg); background: var(--green);
                  border: none; border-radius: 10px;
                  cursor: pointer; text-decoration: none; }}
    .download:hover {{ background: #6db89e; }}
    .platform {{ color: var(--muted); font-size: 10px;
                  letter-spacing: 2px; margin-top: 14px; }}

    .features {{ max-width: 920px; margin: 80px auto 60px;
                  padding: 0 16px; display: grid;
                  grid-template-columns: repeat(3, 1fr); gap: 18px; }}
    .feat    {{ background: var(--panel); border: 1px solid var(--border);
                 border-radius: 10px; padding: 20px; }}
    .feat h3 {{ font-size: 11px; letter-spacing: 3px;
                 color: var(--green); margin: 0 0 10px;
                 font-family: 'Syne', sans-serif; }}
    .feat p  {{ font-size: 12px; color: var(--muted);
                 line-height: 1.6; margin: 0; }}

    footer   {{ text-align: center; padding: 30px 16px 50px;
                 color: var(--muted); font-size: 10px;
                 letter-spacing: 1px; }}
    footer a {{ color: var(--purple); }}

    @media (max-width: 640px) {{
      .features {{ grid-template-columns: 1fr; }}
      .hero h1  {{ font-size: 22px; }}
      .hero .brand {{ font-size: 34px; }}
    }}
  </style>
</head><body>
  <nav>
    <a href="/" style="font-family:'Syne',sans-serif;font-weight:800;
                color:var(--green);letter-spacing:4px;font-size:14px;
                text-decoration:none;">
      ◈ BAPTOU
    </a>
    <div class="sp"></div>
    <!-- V4.6.22 — top-bar quick-download button (compact) -->
    <a href="https://github.com/B4ptiste16/ApexTrading/releases/latest/download/APEX_Setup.exe"
       style="background:rgba(122,181,162,0.18);color:var(--green);
              padding:7px 14px;border-radius:5px;font-size:11px;
              font-weight:600;letter-spacing:1px;text-decoration:none;
              margin-right:14px;">
      ⬇ DOWNLOAD
    </a>
    {topright}
  </nav>

  <section class="hero">
    <a class="brand" href="/" style="text-decoration:none;color:inherit;">◈ BAPTOU</a>
    <div class="sub">TRADING PLATFORM</div>

    <h1>AI-driven automated trading,<br>on your desktop and the cloud.</h1>
    <p>BAPTOU bundles Claude-powered long, short and day strategies
       into one desktop app. Bots can run locally on your machine
       or on the BAPTOU cloud server — 24/7, even with your laptop off.
       Bring your own Alpaca keys, your own AI keys, your own bots.</p>
  </section>

  <!-- V4.6.22 — AI return charts above the main download button.
       Two SVG line charts: one per role (CREATOR / RUNNER).
       Data from /web/api/ai-returns aggregates marketplace bot
       lifetime-snapshots grouped by their META.ai_used / runner_ai. -->
  <section style="max-width:920px;margin:24px auto 8px;padding:0 20px;">
    <h2 style="font-family:'Syne',sans-serif;letter-spacing:3px;
               font-size:13px;color:var(--muted);text-align:center;
               margin:0 0 14px;">AI PERFORMANCE — RETURN OVER TIME</h2>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:18px;">
      <div>
        <div style="color:var(--muted);font-size:11px;text-align:center;
                    letter-spacing:1.5px;margin-bottom:4px;">
          BOT CREATION
        </div>
        <div id="aiReturnCreator" style="min-height:160px;"></div>
      </div>
      <div>
        <div style="color:var(--muted);font-size:11px;text-align:center;
                    letter-spacing:1.5px;margin-bottom:4px;">
          BOT EXECUTION
        </div>
        <div id="aiReturnRunner" style="min-height:160px;"></div>
      </div>
    </div>
    <div style="color:var(--muted);font-size:10px;text-align:center;
                margin-top:8px;">
      Cumulative % return of marketplace bots, grouped by which AI
      created (left) or runs (right) them. Data populates as users
      publish + run bots.
    </div>
  </section>

  <!-- Main download button moved BELOW the AI charts (v4.6.22) -->
  <section style="max-width:920px;margin:0 auto;padding:0 20px;
                  text-align:center;">
    <a class="download"
       href="https://github.com/B4ptiste16/ApexTrading/releases/latest/download/APEX_Setup.exe">
      ⬇  DOWNLOAD FOR WINDOWS
    </a>
    <div class="platform">Free · Windows 10/11 · ~186 MB</div>
  </section>

  <section class="features">
    <div class="feat">
      <h3>AI-DRIVEN</h3>
      <p>Three built-in bots (LONG / SHORT / DAY) score candidates
         with Claude Vision and execute through Alpaca's paper or
         live API. Roll your own from a plain-English description
         via the MAKE BOT tab.</p>
    </div>
    <div class="feat">
      <h3>CLOUD OR LOCAL</h3>
      <p>Tick "Run on Oracle 24/7" per bot. The BAPTOU server runs
         them with your encrypted broker credentials and keeps them
         alive even when the desktop app is closed.</p>
    </div>
    <div class="feat">
      <h3>FULL CONTROL</h3>
      <p>Mobile-accessible dashboard, in-app updater, drag-and-drop
         bot import, custom universes, automation toggles per bot,
         emergency LIQUIDATE button from anywhere.</p>
    </div>
  </section>

  <footer>
    <a href="https://github.com/B4ptiste16/ApexTrading">GitHub</a>
    · <a href="/web/login">Sign in</a>
    · <a href="/web/signup">Sign up</a>
  </footer>

  <script>
    // V4.6.22 — fetch + render AI return-over-time line charts.
    // Pure SVG, no external chart library.
    async function loadAiReturns() {{
      try {{
        const r = await fetch('/web/api/ai-returns');
        const j = await r.json();
        renderLineChart('aiReturnCreator', j.creator || {{}});
        renderLineChart('aiReturnRunner',  j.runner  || {{}});
      }} catch (e) {{
        console.error('ai-returns fetch failed', e);
      }}
    }}

    function renderLineChart(targetId, series) {{
      const el = document.getElementById(targetId);
      if (!el) return;
      const names = Object.keys(series).sort();
      if (names.length === 0) {{
        el.innerHTML = '<div style="color:var(--muted);font-size:11px;'
          + 'padding:50px 0;text-align:center;">'
          + '(no data yet — publish a bot to seed the chart)</div>';
        return;
      }}
      const palette = ['#3eb8a4', '#8a93c9', '#c8a070', '#c28e97',
                       '#cdc578', '#9aa6c4'];
      const W = 420, H = 160, pad = 28;
      // Gather all (ts, pct) points to compute axis ranges
      let allTs = [], allPct = [];
      names.forEach(n => {{
        (series[n] || []).forEach(p => {{
          allTs.push(p.ts); allPct.push(p.pct);
        }});
      }});
      if (allTs.length === 0) {{
        el.innerHTML = '<div style="color:var(--muted);font-size:11px;'
          + 'padding:50px 0;text-align:center;">'
          + '(no return data yet)</div>';
        return;
      }}
      const minTs = Math.min(...allTs), maxTs = Math.max(...allTs);
      const minP  = Math.min(...allPct, 0), maxP = Math.max(...allPct, 0);
      const tsRange = (maxTs - minTs) || 1;
      const pRange  = (maxP - minP)  || 1;
      const x = ts  => pad + ((ts - minTs) / tsRange) * (W - 2*pad);
      const y = pct => H - pad - ((pct - minP) / pRange) * (H - 2*pad);

      // Zero line
      const zeroY = y(0);
      let svg = '<svg width="100%" viewBox="0 0 ' + W + ' ' + H
        + '" preserveAspectRatio="xMidYMid meet" '
        + 'style="background:rgba(255,255,255,0.02);border-radius:6px;">';
      svg += '<line x1="' + pad + '" y1="' + zeroY + '" x2="' + (W-pad)
        + '" y2="' + zeroY + '" stroke="rgba(255,255,255,0.10)" '
        + 'stroke-dasharray="2 3"/>';
      // One polyline per AI
      let legend = '';
      names.forEach((n, i) => {{
        const pts = (series[n] || []).map(p => x(p.ts) + ',' + y(p.pct))
          .join(' ');
        const color = palette[i % palette.length];
        if (pts) {{
          svg += '<polyline points="' + pts + '" fill="none" stroke="'
            + color + '" stroke-width="1.6"/>';
        }}
        // Latest point dot
        const last = (series[n] || []).slice(-1)[0];
        if (last) {{
          svg += '<circle cx="' + x(last.ts) + '" cy="' + y(last.pct)
            + '" r="3" fill="' + color + '"/>';
          legend += '<span style="display:inline-flex;align-items:center;'
            + 'margin:0 8px 4px 0;font-size:10px;color:var(--text);">'
            + '<span style="width:8px;height:8px;background:' + color
            + ';border-radius:1px;margin-right:5px;"></span>' + n
            + ' <span style="color:var(--muted);margin-left:4px;">'
            + (last.pct >= 0 ? '+' : '') + last.pct.toFixed(1) + '%</span>'
            + '</span>';
        }}
      }});
      svg += '</svg>';
      el.innerHTML = svg + '<div style="margin-top:4px;">' + legend + '</div>';
    }}

    loadAiReturns();
    setInterval(loadAiReturns, 300000);  // refresh every 5 min
  </script>
{_FOOTER}
</body></html>"""
    return HTMLResponse(body)


def login_page(error: str | None = None) -> HTMLResponse:
    err_html = f'<div class="err">{error}</div>' if error else ""
    body = f"""<!doctype html>
<html><head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="theme-color" content="#0c0f16">
  <title>BAPTOU — Sign in</title>
  <style>{_BASE_CSS}</style>
</head><body>
  <div class="wrap">
    <a class="brand" href="/" style="text-decoration:none;color:inherit;">◈ BAPTOU</a>
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
{_FOOTER}
</body></html>"""
    return HTMLResponse(body, status_code=401 if error else 200)


def signup_page(error: str | None = None,
                prefill: dict | None = None) -> HTMLResponse:
    err_html = f'<div class="err">{error}</div>' if error else ""
    pf = prefill or {}
    body = f"""<!doctype html>
<html><head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="theme-color" content="#0c0f16">
  <title>BAPTOU — Create account</title>
  <style>{_BASE_CSS}</style>
</head><body>
  <div class="wrap">
    <a class="brand" href="/" style="text-decoration:none;color:inherit;">◈ BAPTOU</a>
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
{_FOOTER}
</body></html>"""
    return HTMLResponse(body, status_code=400 if error else 200)


_SAFE_AREA_CSS = """
  html { background: var(--bg); }
  body {
    padding-top: env(safe-area-inset-top);
    padding-bottom: env(safe-area-inset-bottom);
    background: var(--bg);
    min-height: calc(100vh + env(safe-area-inset-top));
  }
"""


def dashboard_page(user: dict, tab: str = "overview") -> HTMLResponse:
    name = user.get("display_name") or user.get("username") or "user"
    # V7.1.14: clickable Overview / Bots / Universe / Tools tabs so the
    # phone view matches the conceptual map of the desktop app.
    # /web/dashboard?tab=bots, ?tab=universe, ?tab=tools route here.
    tab = (tab or "overview").lower()
    if tab not in ("overview", "bots", "universe", "tools"):
        tab = "overview"

    # Tab bar HTML
    tabs_html = ""
    for key, label in (("overview", "OVERVIEW"), ("bots", "BOTS"),
                       ("universe", "UNIVERSE"), ("tools", "TOOLS")):
        active_cls = " active" if tab == key else ""
        tabs_html += (f'<a class="tab{active_cls}" '
                      f'href="/web/dashboard?tab={key}">{label}</a>')

    # Per-tab HTML — built as plain strings outside the big f-string
    # because Python 3.11 rejects backslash escapes inside f-string
    # {...} expressions.
    overview_html = (
        '<div class="card"><h2>PORTFOLIO VALUE</h2>'
        '<div id="pvchart"><span style="color:var(--muted);font-size:11px;">'
        'Loading…</span></div></div>'
        '<div class="card"><h2>BOTS SNAPSHOT</h2>'
        '<div id="botsmini"></div></div>'
        '<div class="card"><h2>QUICK ACTIONS</h2>'
        '<p style="color:var(--muted);font-size:11px;line-height:1.7;">'
        'Use the <b>BOTS</b> tab to start, stop or liquidate per bot. '
        'Use the desktop app to configure auto-schedule, '
        'cloud-execution and broker keys.</p></div>'
    )
    bots_html = '<div id="bots"></div>'
    universe_html = (
        '<div class="card"><h2>UNIVERSE</h2>'
        '<p class="hint">The candidate tickers each bot considers. '
        "Manage the lists from the desktop app's UNIVERSE tab — they "
        'live in <code>longbot_universe.txt</code> / '
        '<code>shortbot_universe.txt</code> / '
        '<code>daybot_universe.txt</code> on the machine running the '
        'bot. Mobile-side editing coming soon.</p></div>'
    )
    uname  = user.get("username", "")
    uemail = user.get("email", "")
    udisp  = user.get("display_name", "")
    tools_html = (
        '<div class="card"><h2>ACCOUNT</h2>'
        '<div class="stat"><span class="k">USERNAME</span>'
        f'<span class="v">{uname}</span></div>'
        '<div class="stat"><span class="k">EMAIL</span>'
        f'<span class="v">{uemail}</span></div>'
        '<div class="stat"><span class="k">DISPLAY NAME</span>'
        f'<span class="v">{udisp}</span></div>'
        '<p style="margin-top:18px;">'
        '<a href="/web/logout">Sign out</a></p></div>'
        '<div class="card"><h2>API KEYS</h2>'
        '<p class="hint">Broker / Anthropic keys are stored encrypted '
        "on the BAPTOU server. Manage them from the desktop app's "
        '<b>Tools → ACCOUNT LINKING</b> section (Sync keys to BAPTOU '
        'server). Web-side editing coming soon.</p></div>'
        '<div class="card"><h2>DOWNLOAD APP</h2>'
        '<p class="hint">For the full desktop experience: '
        '<a href="/">Download BAPTOU for Windows →</a></p></div>'
    )
    tab_html = {
        "overview": overview_html,
        "bots":     bots_html,
        "universe": universe_html,
        "tools":    tools_html,
    }.get(tab, "")

    body = f"""<!doctype html>
<html><head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="theme-color" content="#0c0f16">
  <title>BAPTOU — Dashboard</title>
  <style>{_BASE_CSS}{_SAFE_AREA_CSS}
    .tabbar  {{ display:flex; gap:0; overflow-x:auto;
                 border-bottom: 1px solid var(--border);
                 margin: 4px 0 18px;
                 -webkit-overflow-scrolling: touch; }}
    .tab     {{ padding: 12px 18px; color: var(--muted);
                 font-size: 10px; letter-spacing: 2px; font-weight: 600;
                 text-decoration: none;
                 border-bottom: 2px solid transparent;
                 white-space: nowrap; flex-shrink: 0; }}
    .tab:hover  {{ color: var(--text); }}
    .tab.active {{ color: var(--text);
                    border-bottom-color: var(--green); }}
    .botcard {{ margin-bottom: 14px; cursor: pointer; }}
    /* V4.6.105 — silenced bots are shown muted + dashed with a SILENCED badge,
       so they're visually distinct from active (unsilenced) bots. */
    .botcard.silenced {{ opacity: .55; border-style: dashed; }}
    .stat.silenced {{ opacity: .55; }}
    .badge-sil {{ font-size: 9px; font-weight: 700; letter-spacing: 1px;
                  color: var(--muted); border: 1px solid var(--muted);
                  border-radius: 5px; padding: 1px 6px; margin-left: 6px;
                  vertical-align: middle; }}
    .botcard .head {{ display:flex; align-items:center; gap:8px;
                       margin-bottom: 10px; }}
    .botcard .dot {{ width:12px; height:12px; border-radius:6px;
                      background: var(--muted); display:inline-block;
                      flex-shrink:0; }}
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
    .detail     {{ display:none; margin-top:14px;
                    border-top: 1px solid var(--border); padding-top:12px; }}
    .detail.open {{ display:block; }}
    .pos-row    {{ display:flex; justify-content:space-between;
                    padding: 8px 0; border-bottom:1px solid var(--border);
                    font-size:11px; }}
    .pos-row:last-child {{ border-bottom:none; }}
    .expand-hint {{ color:var(--muted); font-size:9px;
                     letter-spacing:1px; margin-top:6px; }}
    select.period {{ background:var(--panel2); color:var(--text);
                      border:1px solid var(--border); border-radius:5px;
                      padding:4px 8px; font-family:inherit; font-size:10px;
                      letter-spacing:1px; cursor:pointer; }}
    @keyframes pulse {{
      0%   {{ opacity: 1; }}
      50%  {{ opacity: 0.25; }}
      100% {{ opacity: 1; }}
    }}
  </style>
</head><body>
  <div class="wrap">
    <a class="brand" href="/" style="text-decoration:none;color:inherit;">◈ BAPTOU</a>
    <div class="sub">TRADING PLATFORM</div>

    <div class="tabbar">{tabs_html}</div>

    <!-- Summary header -->
    <div class="card">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:4px;">
        <h2 style="margin:0;">{name.upper()}</h2>
        <select class="period" id="periodSel" onchange="onPeriodChange()">
          <option value="1D">Today</option>
          <option value="1W">1 Week</option>
          <option value="1M">1 Month</option>
        </select>
      </div>
      <div class="stat"><span class="k">TOTAL EQUITY</span>
        <span class="v" id="total">—</span></div>
      <div class="stat"><span class="k">P/L  <span id="periodLabel">TODAY</span></span>
        <span class="v" id="today">—</span></div>
      <div class="stat"><span class="k">SERVER</span>
        <span class="v" id="srv">checking…</span></div>
      <div class="stat"><span class="k">BOTS RUNNING</span>
        <span class="v" id="runcount">—</span></div>
      <div class="stat"><span class="k">BROKER</span>
        <span class="v" style="display:flex;gap:6px;">
          <button class="brk-btn on" data-brk="alpaca"
                  onclick="setBroker('alpaca')">Alpaca</button>
          <button class="brk-btn" data-brk="ibkr"
                  onclick="setBroker('ibkr')">IBKR</button>
        </span></div>
    </div>
    <style>
      .brk-btn{{background:rgba(138,147,201,0.15);color:var(--muted);
        border:1px solid transparent;border-radius:6px;padding:3px 12px;
        font-size:12px;font-weight:700;cursor:pointer;transition:all .15s;}}
      .brk-btn:hover{{background:rgba(138,147,201,0.30);}}
      .brk-btn.on{{background:var(--accent,#5b6cf0);color:#fff;
        border-color:var(--accent,#5b6cf0);}}
    </style>

    {tab_html}

    <!-- V4.6.16 — AI usage across the whole APEX platform (v4.0.0 spec) -->
    <div class="card" id="aiUsageCard" style="margin-top:18px;">
      <h2 style="margin:0 0 10px 0;letter-spacing:1.5px;">AI USAGE  ·  PLATFORM-WIDE</h2>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;">
        <div>
          <div style="color:var(--muted);font-size:11px;margin-bottom:6px;
               letter-spacing:1px;">CREATOR — most-used AIs to BUILD bots</div>
          <div id="aiCreatorChart" style="min-height:180px;"></div>
        </div>
        <div>
          <div style="color:var(--muted);font-size:11px;margin-bottom:6px;
               letter-spacing:1px;">RUNNER — most-used AIs to RUN bots</div>
          <div id="aiRunnerChart" style="min-height:180px;"></div>
        </div>
      </div>
      <div style="color:var(--muted);font-size:10px;margin-top:10px;">
        Last 100 generations:
        <span id="aiGenRoll" style="color:var(--text);"></span>
      </div>
    </div>
  </div>

  <script>
    // V4.6.16 — render a tiny SVG donut for AI usage (no external chart lib).
    function renderDonut(targetId, dataMap) {{
      const el = document.getElementById(targetId);
      if (!el) return;
      const entries = Object.entries(dataMap || {{}})
        .sort((a, b) => b[1] - a[1]).slice(0, 6);
      const total = entries.reduce((s, [k, v]) => s + v, 0);
      if (total === 0) {{
        el.innerHTML = '<div style="color:var(--muted);font-size:11px;'
          + 'padding:30px 0;text-align:center;">(no data yet — '
          + 'publish or generate a bot)</div>';
        return;
      }}
      // Color palette pulled from the app's BAPTOU teal theme
      const palette = ['#3eb8a4', '#8a93c9', '#c8a070', '#c28e97',
                       '#cdc578', '#9aa6c4'];
      const r = 64, cx = 90, cy = 90;
      let cumAngle = -Math.PI / 2;
      let arcs = '', legend = '';
      entries.forEach(([k, v], i) => {{
        const angle = (v / total) * Math.PI * 2;
        const x1 = cx + r * Math.cos(cumAngle), y1 = cy + r * Math.sin(cumAngle);
        cumAngle += angle;
        const x2 = cx + r * Math.cos(cumAngle), y2 = cy + r * Math.sin(cumAngle);
        const large = angle > Math.PI ? 1 : 0;
        const color = palette[i % palette.length];
        arcs += '<path d="M ' + cx + ' ' + cy + ' L ' + x1 + ' ' + y1
              + ' A ' + r + ' ' + r + ' 0 ' + large + ' 1 '
              + x2 + ' ' + y2 + ' Z" fill="' + color + '"/>';
        const pct = ((v / total) * 100).toFixed(0);
        legend += '<div style="display:flex;align-items:center;'
                + 'font-size:11px;margin:3px 0;color:var(--text);">'
                + '<span style="width:10px;height:10px;border-radius:2px;'
                + 'background:' + color + ';margin-right:8px;"></span>'
                + k + ' <span style="color:var(--muted);margin-left:auto;">'
                + v + ' (' + pct + '%)</span></div>';
      }});
      el.innerHTML =
        '<div style="display:flex;align-items:center;gap:18px;">'
        + '<svg width="180" height="180" viewBox="0 0 180 180">'
        + arcs
        + '<circle cx="90" cy="90" r="34" fill="var(--panel)"/>'
        + '<text x="90" y="86" text-anchor="middle" fill="var(--muted)" '
        + 'font-size="9" letter-spacing="1">TOTAL</text>'
        + '<text x="90" y="102" text-anchor="middle" fill="var(--text)" '
        + 'font-size="15" font-weight="700">' + total + '</text>'
        + '</svg>'
        + '<div style="flex:1;">' + legend + '</div></div>';
    }}

    async function refreshAiStats() {{
      try {{
        const r = await fetch('/web/api/ai-stats');
        const j = await r.json();
        renderDonut('aiCreatorChart', j.providers);
        renderDonut('aiRunnerChart',  j.runners);
        const recent = (j.gen_log || []).slice(0, 8)
          .map(g => g.model + ' (' + g.credits + '◊)').join('  ·  ');
        const rEl = document.getElementById('aiGenRoll');
        if (rEl) rEl.textContent = recent || '(no recent generations)';
      }} catch (e) {{
        console.error('ai-stats fetch failed', e);
      }}
    }}

    // V4.6.14 — dashboard sides come from the server (which discovers
    // custom bots in the user's private_bots/ folder). Default to the
    // built-ins until the first /web/api/status response arrives.
    let SIDES = ["LONG", "SHORT", "DAY"];
    // V4.6.65 — which broker the dashboard is viewing (Alpaca / IBKR).
    let BROKER = (localStorage.getItem("apex_broker") || "alpaca");
    function setBroker(b) {{
      BROKER = b;
      try {{ localStorage.setItem("apex_broker", b); }} catch(e) {{}}
      document.querySelectorAll(".brk-btn").forEach(el => {{
        el.classList.toggle("on", el.dataset.brk === b);
      }});
      refresh();
    }}
    const fmt$ = n =>
      n == null ? "—"
        : (n >= 0 ? "+$" : "-$") +
          Math.abs(n).toLocaleString(undefined,
            {{minimumFractionDigits: 2, maximumFractionDigits: 2}});
    const fmtPct = n => n == null ? "—" :
      (n >= 0 ? "+" : "") + n.toFixed(2) + "%";

    let _lastState = null;
    let _periodCache = {{}};  // period → {{total, today_pl, today_pct}}

    function renderBotsMini(state) {{
      const c = document.getElementById("botsmini");
      if (!c) return;
      c.innerHTML = "";
      for (const side of SIDES) {{
        const s = state.bots[side] || {{}};
        const running = s.running === true;
        const silenced = s.silenced === true;
        const dotCls = running ? "dot run" : "dot stop";
        const plCol = (s.today_pl == null) ? "var(--muted)"
                      : (s.today_pl >= 0 ? "var(--green)" : "var(--red)");
        const pos = (s.positions == null) ? "—" : s.positions;
        c.insertAdjacentHTML("beforeend", `
          <div class="stat${{silenced ? ' silenced' : ''}}">
            <span class="k">
              <span class="${{dotCls}}" style="vertical-align:middle;
                margin-right:8px;"></span>${{side}}${{silenced ? ' <span class="badge-sil">SILENCED</span>' : ''}}
            </span>
            <span class="v">${{fmt$(s.equity)}}
              <span style="font-size:10px;color:${{plCol}};">
                ${{fmt$(s.today_pl)}} (${{fmtPct(s.today_pct)}})</span>
              <span style="font-size:10px;color:var(--muted);"> · ${{pos}} pos</span>
            </span>
          </div>`);
      }}
    }}

    // V4.6.129 — combined portfolio-value curve on the overview (sober inline
    // SVG, no chart lib). Sums each bot's equity history aligned from the end.
    async function renderPortfolioChart() {{
      const el = document.getElementById("pvchart");
      if (!el) return;
      let per = "1M";
      const sel = document.getElementById("periodSel");
      if (sel && sel.value) per = (sel.value === "1D") ? "1W" : sel.value;
      let lists = [];
      try {{
        for (const side of SIDES) {{
          const r = await fetch(
            `/web/api/portfolio/history?side=${{side}}&period=${{per}}`);
          if (!r.ok) continue;
          const j = await r.json();
          if (j.history && j.history.length > 1)
            lists.push(j.history.map(h => h.equity));
        }}
      }} catch (e) {{
        el.innerHTML = '<span style="color:var(--red);font-size:11px;">'
                     + 'Could not load history.</span>';
        return;
      }}
      if (!lists.length) {{
        el.innerHTML = '<span style="color:var(--muted);font-size:11px;">'
          + 'No equity history yet — once your bots run, the curve fills in.'
          + '</span>';
        return;
      }}
      const n = Math.min(...lists.map(a => a.length));
      let series = [];
      for (let i = 0; i < n; i++) {{
        let sum = 0;
        for (const a of lists) sum += (a[a.length - n + i] || 0);
        series.push(sum);
      }}
      if (series.length < 2) {{
        el.innerHTML = '<span style="color:var(--muted);font-size:11px;">'
                     + 'Not enough history yet.</span>';
        return;
      }}
      const W = 600, H = 170, P = 10;
      const mn = Math.min(...series), mx = Math.max(...series);
      const rng = (mx - mn) || 1;
      const pts = series.map((v, i) => {{
        const x = P + (i / (series.length - 1)) * (W - 2 * P);
        const y = H - P - ((v - mn) / rng) * (H - 2 * P);
        return x.toFixed(1) + "," + y.toFixed(1);
      }}).join(" ");
      const first = series[0], last = series[series.length - 1];
      const up = last >= first;
      const col = up ? "var(--green)" : "var(--red)";
      const pl = last - first, pct = first ? (pl / first * 100) : 0;
      el.innerHTML =
        `<div style="font-size:13px;margin-bottom:6px;">${{fmt$(last)}}`
        + ` <span style="color:${{col}};font-size:11px;">${{fmt$(pl)}}`
        + ` (${{pct >= 0 ? '+' : ''}}${{pct.toFixed(2)}}%) · ${{per}}</span></div>`
        + `<svg viewBox="0 0 ${{W}} ${{H}}" preserveAspectRatio="none" `
        + `style="width:100%;height:170px;">`
        + `<polyline fill="none" stroke="${{col}}" stroke-width="2" `
        + `points="${{pts}}"/></svg>`;
    }}

    function computeTotals(state) {{
      let total = 0, today = 0, haveAny = false;
      for (const side of SIDES) {{
        const s = state.bots[side] || {{}};
        if (s.equity != null) {{ total += s.equity; haveAny = true; }}
        if (s.today_pl != null) {{ today += s.today_pl; }}
      }}
      return {{ total: haveAny ? total : null, today_pl: today, haveAny }};
    }}

    function renderTotals(total, today_pl, haveAny) {{
      const tot = document.getElementById("total");
      if (tot) tot.textContent = haveAny ? fmt$(total) : "—";
      const t = document.getElementById("today");
      if (t) {{
        t.textContent = haveAny ? fmt$(today_pl) : "—";
        t.className = "v " + ((today_pl||0) >= 0 ? "pos" : "neg");
      }}
    }}

    function onPeriodChange() {{
      const sel = document.getElementById("periodSel");
      const lbl = document.getElementById("periodLabel");
      const map = {{"1D":"TODAY","1W":"THIS WEEK","1M":"THIS MONTH"}};
      lbl.textContent = map[sel.value] || "TODAY";
      renderPortfolioChart();
      if (sel.value === "1D") {{
        // Use cached today data
        if (_lastState) {{
          const t = computeTotals(_lastState);
          renderTotals(t.total, t.today_pl, t.haveAny);
        }}
      }} else {{
        // Try to fetch portfolio history for the period
        fetchPeriodPl(sel.value);
      }}
    }}

    async function fetchPeriodPl(period) {{
      try {{
        // Sum up each bot's history for the period
        let totalPl = 0, haveAny = false;
        for (const side of SIDES) {{
          const r = await fetch(
            `/web/api/portfolio/history?side=${{side}}&period=${{period}}`);
          if (!r.ok) continue;
          const j = await r.json();
          if (j.history && j.history.length > 0) {{
            const first = j.history[0].equity;
            const last  = j.history[j.history.length-1].equity;
            if (first && last) {{ totalPl += (last - first); haveAny = true; }}
          }}
        }}
        if (haveAny) {{
          const t = computeTotals(_lastState || {{bots:{{}}}});
          renderTotals(t.total, totalPl, true);
        }}
      }} catch(e) {{
        // Silently ignore — period history is best-effort
      }}
    }}

    function renderBots(state) {{
      const c = document.getElementById("bots");
      if (!c) return;
      c.innerHTML = "";
      for (const side of SIDES) {{
        const s = state.bots[side] || {{}};
        const running = s.running === true;
        const silenced = s.silenced === true;
        const dotCls = running ? "dot run" : "dot stop";
        const dis = (state.linked === false) ? "disabled" : "";
        const nPos = s.positions == null ? 0 : s.positions;
        c.insertAdjacentHTML("beforeend", `
          <div class="card botcard${{silenced ? ' silenced' : ''}}" id="card-${{side}}" onclick="toggleDetail('${{side}}')">
            <div class="head">
              <span class="${{dotCls}}"></span>
              <h2 style="margin:0;flex:1;">${{side}}</h2>
              ${{silenced ? '<span class="badge-sil">SILENCED</span>' : ''}}
              <span class="v" style="font-size:12px;">${{fmt$(s.equity)}}</span>
            </div>
            <div class="stat"><span class="k">TODAY P/L</span>
              <span class="v ${{(s.today_pl||0) >= 0 ? 'pos' : 'neg'}}">
                ${{fmt$(s.today_pl)}} (${{fmtPct(s.today_pct)}})</span></div>
            <div class="stat"><span class="k">POSITIONS</span>
              <span class="v">${{nPos}}</span></div>
            <div class="stat"><span class="k">STATUS</span>
              <span class="v">${{running ? 'RUNNING' : 'STOPPED'}}</span></div>
            <p class="expand-hint">▼ TAP FOR DETAILS</p>
            <!-- Detail panel (hidden by default) -->
            <div class="detail" id="detail-${{side}}">
              <div id="positions-${{side}}" style="font-size:11px;color:var(--muted);">
                Loading positions…
              </div>
              <div class="actions" onclick="event.stopPropagation()">
                <button class="btn go" ${{running || dis ? 'disabled' : ''}}
                        onclick="act('${{side}}','start')">▶  START</button>
                <button class="btn stop" ${{!running || dis ? 'disabled' : ''}}
                        onclick="act('${{side}}','stop')">■  STOP</button>
                <button class="btn liq" ${{dis ? 'disabled' : ''}}
                        onclick="liq('${{side}}',${{nPos}})">⚠  LIQUIDATE</button>
              </div>
            </div>
          </div>
        `);
      }}
      if (state.linked === false) {{
        c.insertAdjacentHTML("afterbegin", `
          <div class="card">
            <h2>NO BROKER LINKED</h2>
            <p class="hint">Open the desktop app → Tools → API Keys, fill
            in your Alpaca keys, then click <b>Sync keys to BAPTOU server</b>
            under ACCOUNT LINKING. Refresh this page once that's done.</p>
          </div>`);
      }}
    }}

    const _detailOpen = {{}};
    function toggleDetail(side) {{
      const el = document.getElementById(`detail-${{side}}`);
      if (!el) return;
      const open = el.classList.toggle("open");
      _detailOpen[side] = open;
      if (open) fetchPositions(side);
    }}

    async function fetchPositions(side) {{
      const el = document.getElementById(`positions-${{side}}`);
      if (!el) return;
      try {{
        const r = await fetch(`/web/api/bots/${{side}}/positions`);
        const j = await r.json();
        if (!j.linked) {{
          el.innerHTML = '<span style="color:var(--muted);">No broker linked.</span>';
          return;
        }}
        if (!j.positions || j.positions.length === 0) {{
          el.innerHTML = '<span style="color:var(--muted);">No open positions.</span>';
          return;
        }}
        let html = '';
        for (const p of j.positions) {{
          const plColor = p.pl >= 0 ? 'var(--green)' : 'var(--red)';
          const sign = p.pl >= 0 ? '+' : '';
          html += `<div class="pos-row">
            <span><b>${{p.symbol}}</b> ×${{p.qty.toFixed(0)}}</span>
            <span>${{sign}}$${{Math.abs(p.pl).toFixed(2)}}
              <span style="color:var(--muted);">(${{p.pl_pct >= 0 ? '+' : ''}}${{p.pl_pct.toFixed(1)}}%)</span>
            </span>
          </div>`;
        }}
        el.innerHTML = html;
      }} catch(e) {{
        el.innerHTML = '<span style="color:var(--red);">Could not load positions.</span>';
      }}
    }}

    async function refresh() {{
      try {{
        const r = await fetch("/web/api/status?broker=" + BROKER);
        const j = await r.json();
        _lastState = j;
        // V4.6.14 — adopt the server's sides list so custom bots
        // (CRYPTO etc.) render alongside the built-ins.
        if (Array.isArray(j.sides) && j.sides.length > 0) {{
          SIDES = j.sides;
        }}
        document.getElementById("srv").textContent = "OK";
        document.getElementById("srv").className = "v pos";
        const t = computeTotals(j);
        const sel = document.getElementById("periodSel");
        if (!sel || sel.value === "1D") {{
          renderTotals(t.total, t.today_pl, t.haveAny);
        }}
        renderBots(j);
        renderBotsMini(j);
        renderPortfolioChart();
        const rc = document.getElementById("runcount");
        if (rc) {{
          const n = j.running_count || 0;
          rc.textContent = n + " / " + (j.sides ? j.sides.length : 0);
          rc.className = "v " + (n > 0 ? "pos" : "");
        }}
        // Re-fetch open detail panels
        for (const side of SIDES) {{
          if (_detailOpen[side]) fetchPositions(side);
        }}
      }} catch (e) {{
        document.getElementById("srv").textContent = "OFFLINE";
        document.getElementById("srv").className = "v neg";
      }}
    }}

    async function act(side, cmd) {{
      if (!confirm(`${{cmd.toUpperCase()}} ${{side}} bot?`)) return;
      try {{
        const r = await fetch(`/web/api/bots/${{side}}/${{cmd}}?broker=${{BROKER}}`,
                              {{ method: "POST" }});
        const j = await r.json();
        if (!r.ok) alert(j.detail || "Action failed.");
      }} catch (e) {{ alert(e.message); }}
      refresh();
    }}

    async function liq(side, nPos) {{
      const posText = nPos > 0
        ? `${{nPos}} open position${{nPos > 1 ? 's' : ''}} will be sold at market price.`
        : 'All open positions will be sold at market price.';
      if (!confirm(
        `LIQUIDATE ${{side}} bot?\\n\\n` + posText +
        `\\n\\nThis cannot be undone. Proceed?`)) return;
      try {{
        const r = await fetch(`/web/api/bots/${{side}}/liquidate`,
                              {{ method: "POST" }});
        const j = await r.json();
        alert(j.detail || (r.ok ? "Liquidation order sent."
                                 : "Liquidate failed."));
      }} catch (e) {{ alert(e.message); }}
      refresh();
    }}

    setBroker(BROKER);
    setInterval(refresh, 30000);
    // V4.6.16 — AI usage donut refreshes every 2 min (platform-wide
    // data changes slowly; no need to poll as often as user state).
    refreshAiStats();
    setInterval(refreshAiStats, 120000);
  </script>
{_FOOTER}
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
