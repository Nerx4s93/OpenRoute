"""Server-rendered HTML admin UI (no template engine, no JS frameworks).

Module of the ``web-admin`` microservice. Multi-page: «Прокси» (``/admin/ui``)
and «Ключи» (``/admin/ui/keys``); on each page the add-form sits ABOVE the table
so nothing has to be scrolled back to. Per-key statistics are computed from the
request log, so they survive key deletion (the ``key_id`` lives on in the log
independently of the key configuration). The panel does NOT read the gateway's
DB directly — it fetches data from the gateway's internal API
(``X-Internal-Token``); see ``main.py`` of this service.

Login is a password-only form on ``/login`` (``ADMIN_PASSWORD``); on success the
server creates a session: an HttpOnly cookie holding a random ``secrets`` token
(validity comes solely from the server-side store — no signatures/claims). CSRF:
each session has its own random token, carried as a hidden field in POST forms
and compared constant-time on POST. The «Выйти» button (``POST /admin/ui/logout``)
deletes the session server-side and clears the cookie.

Auto-refresh: ``render_*_fragments()`` is the single source of markup for the
dynamic sections of each page. The full page and the data endpoints
(``/admin/ui/data``, ``/admin/ui/keys/data``) render the same fragments; inline
JS polls the data endpoint every 5 seconds and swaps them in by id — the page
stays fresh without clobbering form input.

Data shapes (frozen):

- Proxies: ``status`` = ``ProxyManager.status()``; ``usage_by_proxy`` keys are
  masked proxies (``host:port``, the ``account`` column of the request log).
  For the enable/disable/delete forms the gateway augments each row with a
  ``real_proxy`` (the full, unmasked proxy URL) — mirroring NVRoute's
  ``real_token`` — so actions can address the row; the table only ever shows
  the masked ``host:port``. A defensive resolver below accepts several spellings
  so the UI degrades gracefully if the gateway names it differently.
- Keys: ``keys`` = ``KeyDB.all()`` rows (plaintext and only sha256 hashes ever
  reach the page for hidden fields).
"""

from __future__ import annotations

import html
from datetime import datetime

_PANEL = "/admin/ui"


def _esc(x: object) -> str:
    return html.escape("" if x is None else str(x), quote=True)


def _num(x: int | float) -> str:
    return f"{x:,}".replace(",", " ")


_CSS = """
:root {
  color-scheme: dark;
  --bg: #090e15; --surface: #101822; --surface-2: #141e29;
  --line: #263341; --line-soft: #1d2935; --text: #eaf2f4;
  --muted: #8d9ca9; --mint: #91e8ce; --blue: #91baff;
  --amber: #f5c77a; --red: #ff9b9b;
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body { margin: 0; min-width: 320px; background: var(--bg); color: var(--text);
  font: 14px/1.5 Inter, "Segoe UI", system-ui, sans-serif; }
body::before { content: ""; position: fixed; inset: 0; pointer-events: none;
  background: radial-gradient(ellipse 60% 40% at 95% -10%, #194c4b24, transparent 75%);
  z-index: -1; }
a { color: inherit; text-decoration: none; }
button, input { font: inherit; }
button, a, input { -webkit-tap-highlight-color: transparent; }
:focus-visible { outline: 2px solid var(--mint); outline-offset: 3px; }
.muted { color: var(--muted); }
.mono, code { font-family: "Cascadia Code", Consolas, ui-monospace, monospace; }
.wrap { min-height: 100vh; display: grid; grid-template-columns: 240px minmax(0, 1fr); }
.sidebar { position: sticky; top: 0; height: 100vh; display: flex; flex-direction: column;
  padding: 30px 16px 22px; border-right: 1px solid var(--line-soft);
  background: #0d141d; }
.brand { display: flex; align-items: center; gap: 12px; padding: 3px 12px 38px; }
.brand-mark { display: grid; place-items: center; width: 36px; height: 36px; border-radius: 11px;
  background: linear-gradient(145deg, #a5f4d9, #4da9ae); color: #092328;
  font-size: 17px; font-weight: 900; letter-spacing: -.08em; box-shadow: 0 8px 28px #4da9ae2d; }
.brand-name { font-size: 17px; font-weight: 750; letter-spacing: -.04em; line-height: 1.1; }
.brand-sub { display: block; margin-top: 3px; color: #8293a0; font-size: 10px;
  font-weight: 650; letter-spacing: .14em; text-transform: uppercase; }
.nav-label { padding: 0 13px 10px; color: #657784; font-size: 10px;
  font-weight: 800; text-transform: uppercase; letter-spacing: .17em; }
header.top nav { display: grid; gap: 5px; }
header.top nav a { display: flex; align-items: center; gap: 11px; min-height: 42px;
  padding: 0 13px; border: 1px solid transparent; border-radius: 10px;
  color: #a4b2bd; font-weight: 600; transition: background .2s, color .2s, transform .2s; }
header.top nav a::before { content: ""; display: block; width: 16px; height: 16px;
  border: 1.7px solid currentColor; border-radius: 5px; opacity: .8; }
header.top nav a[href$="keys"]::before { border-radius: 50%; width: 14px; height: 14px;
  box-shadow: 5px 4px 0 -3px currentColor; }
header.top nav a:hover { background: #1a2934; color: var(--text); transform: translateX(2px); }
header.top nav a.active { background: #83decf16; border-color: #88e5cf30; color: var(--mint); }
.sidebar-bottom { margin-top: auto; padding: 18px 10px 0; border-top: 1px solid var(--line-soft); }
.side-status { display: flex; align-items: center; gap: 9px; color: #a9b8bd; font-size: 12px; }
.live-dot { width: 7px; height: 7px; flex: none; border-radius: 50%;
  background: var(--mint); box-shadow: 0 0 0 4px #91e8ce19, 0 0 12px #91e8ce80;
  animation: breathe 2.5s ease-in-out infinite; }
.sidebar-bottom form { margin: 20px 0 0; }
.sidebar-bottom form button { width: 100%; text-align: left; padding: 9px 11px; }
.main { min-width: 0; padding: 0 clamp(22px, 4vw, 64px) 72px; }
.topline { display: flex; align-items: center; justify-content: space-between; gap: 20px;
  min-height: 74px; border-bottom: 1px solid var(--line-soft); }
.breadcrumb { color: #7d8e9c; font-size: 12px; font-weight: 650; letter-spacing: .025em; }
.breadcrumb strong { color: #d6e5e5; font-weight: 650; }
.top-meta { display: flex; align-items: center; gap: 9px; color: #99a9b5; font-size: 12px; }
.top-meta .live-dot { width: 6px; height: 6px; }
#srv-now { font-variant-numeric: tabular-nums; white-space: nowrap; }
.hero { position: relative; overflow: hidden; margin: 30px 0 24px; min-height: 180px;
  display: flex; align-items: center; justify-content: space-between; gap: 20px;
  padding: 35px clamp(24px, 3vw, 42px); border: 1px solid #36505a;
  border-radius: 19px; background: linear-gradient(110deg, #193938, #172b35 53%, #17212d); }
.hero::before { content: ""; position: absolute; inset: 0; opacity: .65; pointer-events: none;
  background: radial-gradient(circle at 82% 18%, #55d1b84a, transparent 27%),
              radial-gradient(circle at 100% 100%, #6a89d930, transparent 35%); }
.hero::after { content: ""; position: absolute; width: 290px; height: 290px; right: -75px;
  top: -85px; border: 1px solid #b4ffee25; border-radius: 50%; box-shadow:
  0 0 0 40px #b4ffee09, 0 0 0 85px #b4ffee07; pointer-events: none; }
.hero-content, .hero-aside { position: relative; z-index: 1; }
.eyebrow { color: #a5edda; font-size: 11px; font-weight: 800; letter-spacing: .19em;
  text-transform: uppercase; }
h1 { margin: 7px 0 5px; font-size: clamp(27px, 3vw, 38px); line-height: 1.16;
  font-weight: 750; letter-spacing: -.045em; }
.hero p { margin: 0; max-width: 490px; color: #b7cdcf; font-size: 13px; }
.hero-aside { display: flex; align-items: center; gap: 9px; flex: none; padding: 10px 14px;
  background: #091b2280; border: 1px solid #aeefdc39; border-radius: 999px;
  color: #d6f6eb; font-size: 11px; font-weight: 650; white-space: nowrap; }
.msg { margin: 0 0 22px; padding: 12px 16px; border: 1px solid #8de9d244;
  background: #8de9d214; color: #b5f4e3; border-radius: 10px; animation: rise .35s ease both; }
.period-bar { display: flex; align-items: center; justify-content: space-between; gap: 16px;
  flex-wrap: wrap; margin: 0 0 15px; }
.period-title { color: #8294a0; font-size: 10px; font-weight: 800;
  letter-spacing: .14em; text-transform: uppercase; }
.period-tabs { display: inline-flex; align-items: center; gap: 2px; padding: 3px;
  border: 1px solid var(--line); border-radius: 10px; background: var(--surface); }
.period-tabs a { padding: 6px 12px; border-radius: 7px; color: #8ea0ab; font-size: 11px;
  font-weight: 650; white-space: nowrap; transition: background .2s, color .2s; }
.period-tabs a:hover { color: var(--text); background: #1b2a35; }
.period-tabs a.active { color: var(--mint); background: #83decf16;
  box-shadow: inset 0 0 0 1px #88e5cf30; }
.cards { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 13px;
  margin: 0 0 28px; }
.card { position: relative; overflow: hidden; min-height: 136px; padding: 20px 19px 16px;
  background: var(--surface); border: 1px solid var(--line); border-radius: 15px;
  transition: transform .25s, border-color .25s, box-shadow .25s; }
.card:hover { transform: translateY(-3px); border-color: #4c6c70; box-shadow: 0 15px 30px #00000020; }
.card::after { content: ""; position: absolute; width: 90px; height: 90px; right: -27px;
  top: -32px; border-radius: 50%; background: var(--accent); opacity: .1; }
.card .glyph { display: grid; place-items: center; width: 30px; height: 30px;
  border-radius: 9px; background: color-mix(in srgb, var(--accent) 12%, transparent);
  color: var(--accent); font-size: 15px; font-weight: 700; }
.card .num { margin-top: 11px; font-size: clamp(23px, 2.1vw, 30px); line-height: 1.15;
  font-weight: 750; letter-spacing: -.05em; font-variant-numeric: tabular-nums; }
.card .lbl { margin-top: 4px; color: var(--muted); font-size: 11px; font-weight: 600; }
.card:nth-child(1) { --accent: #91e8ce; } .card:nth-child(2) { --accent: #92b9ff; }
.card:nth-child(3) { --accent: #ff9b9b; } .card:nth-child(4) { --accent: #c9b1ff; }
.card:nth-child(5) { --accent: #f5c77a; } .card:nth-child(6) { --accent: #ffb38a; }
.section { margin: 0 0 22px; overflow: hidden; background: var(--surface);
  border: 1px solid var(--line); border-radius: 15px; }
.section-head { display: flex; align-items: center; justify-content: space-between; gap: 15px;
  min-height: 70px; padding: 16px 22px; border-bottom: 1px solid var(--line-soft); }
.section-title { display: flex; align-items: center; gap: 11px; }
.section-icon { display: grid; place-items: center; width: 30px; height: 30px;
  border-radius: 9px; color: var(--mint); background: #91e8ce17; font-size: 16px; }
h2 { margin: 0; font-size: 14px; font-weight: 700; letter-spacing: -.01em; }
.section-note { color: #8294a0; font-size: 11px; white-space: nowrap; }
.table-scroll { width: 100%; overflow-x: auto; }
.table-scroll { scrollbar-color: #496777 #111c26; scrollbar-width: thin; }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 13px 14px; text-align: left; border-bottom: 1px solid var(--line-soft);
  font-size: 12px; vertical-align: middle; }
th { color: #8394a1; font-size: 10px; font-weight: 750; text-transform: uppercase;
  letter-spacing: .095em; background: #111c26; white-space: nowrap; }
th:first-child, td:first-child { padding-left: 22px; }
th:last-child, td:last-child { padding-right: 22px; }
tbody tr { transition: background .18s; }
tbody tr:hover { background: #1b2a35; }
tbody tr:last-child td { border-bottom: 0; }
tr.dead td { color: #7f909d; }
td.mono { font-size: 11px; white-space: nowrap; }
td.actions { white-space: nowrap; }
td.actions form { display: inline-block; margin: 2px 5px 2px 0; }
.badge { display: inline-flex; align-items: center; gap: 6px; padding: 4px 9px;
  border: 1px solid transparent; border-radius: 999px; font-size: 10px;
  font-weight: 700; white-space: nowrap; }
.badge::before { content: ""; width: 5px; height: 5px; border-radius: 50%; background: currentColor; }
.b-ok { color: #96e9c8; border-color: #67d7af34; background: #67d7af16; }
.b-warn { color: #f5c77a; border-color: #efbd6a38; background: #efbd6a16; }
.b-err { color: #ff9b9b; border-color: #ff858538; background: #ff858516; }
.b-off { color: #a1acb7; border-color: #9baab22b; background: #9baab211; }
button { padding: 7px 11px; border: 1px solid #3a4a58; border-radius: 8px;
  background: #1b2935; color: #dbe9eb; font-size: 11px; font-weight: 650;
  cursor: pointer; transition: background .2s, border-color .2s, transform .2s; }
button:hover { background: #273b48; border-color: #77958e; transform: translateY(-1px); }
button:active { transform: translateY(0); }
button.danger { color: var(--red); border-color: #ba77773d; background: #ba777710; }
button.danger:hover { background: #ba777728; }
button.acc { color: var(--mint); border-color: #71cdaa48; background: #71cdaa13; }
button.acc:hover { background: #71cdaa28; }
.form-content { padding: 22px; }
form.add { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 15px; }
.field { grid-column: span 2; display: grid; gap: 7px; min-width: 0; }
.field.wide { grid-column: span 3; }
.field label, .check-field > span { color: #a9bac3; font-size: 11px; font-weight: 700; }
form.add input[type=text], .login-box input[type=password] { width: 100%; min-width: 0;
  padding: 10px 12px; border: 1px solid #344756; border-radius: 9px;
  background: #0d1720; color: var(--text); outline: none;
  transition: border-color .2s, box-shadow .2s, background .2s; }
form.add input[type=text]::placeholder, .login-box input::placeholder { color: #647684; }
form.add input[type=text]:focus, .login-box input[type=password]:focus {
  border-color: #85dfcb; box-shadow: 0 0 0 3px #85dfcb1c; background: #0f1d27; }
.form-footer { grid-column: 1 / -1; display: flex; align-items: center;
  justify-content: space-between; gap: 16px; padding-top: 5px; }
.check-field { display: flex; align-items: center; gap: 9px; color: #a9bac3;
  font-size: 11px; font-weight: 700; }
.check-field input { accent-color: #91e8ce; width: 15px; height: 15px; }
form.add button, .login-box button[type=submit] { padding: 10px 18px;
  background: var(--mint); border-color: var(--mint); color: #092322; font-size: 12px; font-weight: 800; }
form.add button:hover, .login-box button[type=submit]:hover {
  background: #b1f5df; border-color: #b1f5df; }
.form-hint { color: #6e818e; font-size: 11px; }
.page-enter { animation: rise .55s cubic-bezier(.2,.7,.2,1) both; }
.page-enter:nth-of-type(2) { animation-delay: .06s; }
.page-enter:nth-of-type(3) { animation-delay: .12s; }
.login-page { min-height: 100vh; display: grid; grid-template-columns: minmax(0, 1.05fr) minmax(420px, .95fr); }
.login-visual { position: relative; overflow: hidden; display: flex; flex-direction: column;
  justify-content: space-between; min-height: 100vh; padding: clamp(34px, 5vw, 72px);
  border-right: 1px solid #38574e; background: linear-gradient(145deg, #102c32, #0e1926 75%); }
.login-visual::before { content: ""; position: absolute; width: 600px; height: 600px;
  left: -210px; bottom: -230px; border: 1px solid #91e8ce2c; border-radius: 50%;
  box-shadow: 0 0 0 65px #91e8ce09, 0 0 0 145px #91e8ce08, 0 0 0 240px #91e8ce06; }
.login-visual::after { content: ""; position: absolute; width: 360px; height: 360px;
  right: -130px; top: 18%; border: 1px solid #91e8ce2c; border-radius: 50%;
  box-shadow: 0 0 0 65px #91e8ce08, 0 0 0 140px #91e8ce05; }
.login-visual > * { position: relative; z-index: 1; }
.login-brand { display: flex; align-items: center; gap: 12px; font-size: 20px;
  font-weight: 750; letter-spacing: -.04em; }
.login-brand .brand-mark { width: 42px; height: 42px; }
.login-statement { max-width: 570px; padding-bottom: 12vh; }
.login-statement .eyebrow { margin-bottom: 19px; }
.login-statement h2 { font-size: clamp(40px, 5vw, 68px); line-height: 1.07;
  letter-spacing: -.06em; font-weight: 750; }
.login-statement p { margin: 19px 0 0; max-width: 420px; color: #a9c3c9; font-size: 15px; }
.login-foot { color: #78959d; font-size: 11px; letter-spacing: .09em; }
.login-main { display: grid; place-items: center; padding: 32px; }
.login-wrap { width: min(100%, 380px); animation: rise .6s ease both; }
.login-kicker { color: var(--mint); font-size: 11px; font-weight: 800;
  letter-spacing: .16em; text-transform: uppercase; }
.login-box h1 { margin: 11px 0 8px; font-size: 35px; }
.login-box p.sub { margin: 0 0 29px; color: var(--muted); }
.login-box label { display: block; margin-bottom: 8px; color: #b5c5ce;
  font-size: 12px; font-weight: 700; }
.login-box input[type=password] { margin-bottom: 18px; padding: 13px 14px; }
.login-box button[type=submit] { width: 100%; padding: 13px; font-size: 13px; }
.login-help { margin-top: 25px; color: #667b89; font-size: 11px; text-align: center; }
.err { margin: 17px 0 0; padding: 11px 13px; border: 1px solid #ff858548;
  background: #ff858517; border-radius: 9px; color: var(--red); }
@keyframes breathe { 50% { box-shadow: 0 0 0 6px #91e8ce0b, 0 0 18px #91e8ce9c; } }
@keyframes rise { from { opacity: 0; transform: translateY(12px); }
  to { opacity: 1; transform: translateY(0); } }
@media (max-width: 1220px) { .cards { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
@media (max-width: 900px) {
  .wrap { display: block; }
  .sidebar { position: relative; height: auto; padding: 12px 20px; flex-direction: row;
    align-items: center; gap: 16px; border-right: 0; border-bottom: 1px solid var(--line-soft); }
  .brand { padding: 0; }
  .brand-sub, .nav-label, .sidebar-bottom { display: none; }
  header.top nav { display: flex; margin-left: auto; }
  header.top nav a { min-height: 38px; }
  .main { padding: 0 22px 50px; }
  .login-page { grid-template-columns: 1fr; }
  .login-visual { min-height: 250px; padding: 30px; }
  .login-statement { padding: 40px 0 0; }
  .login-statement h2 { font-size: 36px; }
  .login-statement p, .login-foot { display: none; }
  .login-main { min-height: 440px; }
}
@media (max-width: 650px) {
  .sidebar { padding: 11px 14px; }
  .brand-name { font-size: 15px; }
  .brand-mark { width: 31px; height: 31px; }
  header.top nav a { min-height: 34px; padding: 0 9px; font-size: 12px; }
  header.top nav a::before { display: none; }
  .main { padding: 0 14px 40px; }
  .topline { min-height: 52px; }
  .top-meta { font-size: 10px; }
  .crumb-root, .meta-label { display: none; }
  .hero { min-height: 160px; margin: 18px 0; padding: 25px 21px; }
  .hero-aside { display: none; }
  .period-bar { align-items: flex-start; flex-direction: column; gap: 8px; }
  .period-tabs { width: 100%; overflow-x: auto; }
  .period-tabs a { flex: 1; padding: 6px 7px; text-align: center; }
  .cards { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px; }
  .card { min-height: 120px; padding: 15px; }
  .section-head { padding: 14px 16px; }
  .section-note { display: none; }
  .table-scroll::before { content: "Прокрутите таблицу →"; display: block; position: sticky;
    left: 0; width: max-content; padding: 10px 16px; color: #7895a3; font-size: 10px; }
  .table-scroll table { min-width: 760px; }
  .field, .field.wide { grid-column: span 6; }
  .form-content { padding: 16px; }
  .form-footer { align-items: stretch; flex-direction: column; }
  form.add button { width: 100%; }
  .login-visual { min-height: 195px; }
  .login-statement { padding-top: 24px; }
  .login-statement h2 { font-size: 30px; }
  .login-main { min-height: 390px; padding: 24px; }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation-duration: .01ms !important;
    transition-duration: .01ms !important; scroll-behavior: auto !important; }
}
"""

# Proxy states. Unlike NVRoute's accounts there is no permanently "dead" state:
# proxies are never disabled permanently — sustained failure is expressed as a
# cooldown only. "disabled"/"dead" are still read defensively
# so the renderer keeps working if the gateway adds such a flag later.
_STATE_LABELS = {
    "ok": ("ok", "b-ok"),
    "quota": ("лимит", "b-warn"),
    "ban": ("бан", "b-err"),
    "cooldown": ("cooldown", "b-warn"),
    "rpm": ("RPM-кап", "b-warn"),
    "noproxy": ("нет в пуле", "b-warn"),
    "off": ("выключен", "b-off"),
    "dead": ("мёртв", "b-err"),
}

# Spending windows for the dashboard. Backend windows are rolling:
# day = 24h, week = 7d, month = 30d, all = no cutoff.
_PERIOD_OPTIONS = (
    ("day", "День", "Последние 24 часа"),
    ("week", "Неделя", "Последние 7 дней"),
    ("month", "Месяц", "Последние 30 дней"),
    ("all", "Всё время", "За всё время"),
)
_PERIOD_CAPTIONS = {
    "day": "за день",
    "week": "за неделю",
    "month": "за месяц",
    "all": None,
}
_PERIOD_SECTIONS = {
    "day": "За день",
    "week": "За неделю",
    "month": "За месяц",
    "all": "За всё время",
}


def _proxy_state(s: dict) -> str:
    """Classify one status row into a state key (see ``_STATE_LABELS``)."""
    if s.get("disabled"):
        return "dead"
    if s.get("manual"):
        return "off"
    if not s.get("enabled", True):
        return "noproxy"
    if (s.get("banned_seconds_left") or 0) > 0:
        return "ban"
    if (s.get("quota_seconds_left") or 0) > 0:
        return "quota"
    if (s.get("cooldown_seconds_left") or 0) > 0:
        return "cooldown"
    if s.get("rpm_capped"):
        return "rpm"
    return "ok"


def _proxy_display(s: dict) -> str:
    """Masked ``host:port`` shown in the table (never the credentials)."""
    return str(s.get("proxy") or s.get("url") or "")


def _proxy_identity(s: dict) -> str:
    """Value for the hidden form fields that address the row.

    Mirrors NVRoute's ``real_token``: the internal API snapshot augments each
    status row with the full, unmasked proxy URL (``real_proxy``) for the
    enable/disable/delete forms. Restrictive fallbacks keep the UI working if
    the gateway exposes it under another spelling: any candidate that looks like
    a full URL (``scheme://...``) is preferred, because ``ProxyDB`` rows are
    keyed by the full ``"http://user:pass@host:port"`` string.
    """
    candidates = [s.get(k) for k in ("real_proxy", "real_url", "url", "proxy")]
    candidates = [str(c) for c in candidates if c]
    for c in candidates:
        if "://" in c:
            return c
    return candidates[0] if candidates else ""


def _duration_label(seconds: object) -> str:
    """Compact remaining-time label for quota/ban badges."""
    try:
        total = max(0, int(float(seconds or 0)))
    except (TypeError, ValueError):
        total = 0
    if total < 60:
        return "<1м"
    hours, rest = divmod(total, 3600)
    minutes = rest // 60
    if hours:
        return f"{hours}ч {minutes:02d}м"
    return f"{minutes}м"


def _proxy_rows(proxies: list[dict], usage: dict, csrf: str,
                period: str = "all") -> str:
    rows = []
    for s in proxies:
        state = _proxy_state(s)
        label, cls = _STATE_LABELS[state]
        if state == "quota":
            label = f"лимит {_duration_label(s.get('quota_seconds_left'))}"
        elif state == "ban":
            label = f"бан {_duration_label(s.get('banned_seconds_left'))}"
        elif state == "cooldown":
            label = f"cooldown {float(s.get('cooldown_seconds_left') or 0):.0f}s"
        u = usage.get(_proxy_display(s), {}) or {}
        # Requests/errors come from the persistent request log (all-time), not
        # from the gateway's in-memory counters, which reset on restart.
        requests = u.get("requests", s.get("requests", 0))
        errors = u.get("errors", s.get("errors", 0))
        dead_cls = ' class="dead"' if state in ("dead", "off") else ""
        real = _esc(_proxy_identity(s))
        toggle = (
            f'<form method="post" action="{_PANEL}/proxies/set">'
            f'<input type="hidden" name="_csrf" value="{_esc(csrf)}">'
            f'<input type="hidden" name="period" value="{_esc(period)}">'
            f'<input type="hidden" name="proxy" value="{real}">'
            f'<input type="hidden" name="enabled" value="{0 if s.get("manual") is False else 1}">'
        )
        if s.get("manual"):
            toggle += '<button class="acc" type="submit">Включить</button></form>'
        else:
            toggle += '<button type="submit">Отключить</button></form>'
        delete = (
            f'<form method="post" action="{_PANEL}/proxies/delete" '
            f'onsubmit="return confirm(\'Удалить прокси {_esc(_proxy_display(s))}?\')">'
            f'<input type="hidden" name="_csrf" value="{_esc(csrf)}">'
            f'<input type="hidden" name="period" value="{_esc(period)}">'
            f'<input type="hidden" name="proxy" value="{real}">'
            '<button class="danger" type="submit">Удалить</button></form>'
        )
        rows.append(
            f"<tr{dead_cls}>"
            f'<td class="mono">{_esc(_proxy_display(s))}</td>'
            f'<td><span class="badge {cls}">{label}</span></td>'
            f"<td>{_num(requests)}</td>"
            f"<td>{_num(errors)}</td>"
            f"<td>{_num(u.get('tokens', 0))}</td>"
            f'<td class="muted">{_esc(s.get("last_error") or "")}</td>'
            f'<td class="actions">{toggle}{delete}</td>'
            "</tr>"
        )
    return "\n".join(rows) or '<tr><td colspan="7" class="muted">Прокси нет</td></tr>'


def _key_rows(keys: list[dict], csrf: str, period: str = "all") -> str:
    rows = []
    for k in keys:
        on = k["enabled"]
        state_label, state_cls = ("активен", "b-ok") if on else ("отозван", "b-off")
        dead_cls = "" if on else ' class="dead"'
        real = _esc(k["key_hash"])  # sha256 — не секрет, годится для hidden-полей
        toggle = (
            f'<form method="post" action="{_PANEL}/keys/set">'
            f'<input type="hidden" name="_csrf" value="{_esc(csrf)}">'
            f'<input type="hidden" name="period" value="{_esc(period)}">'
            f'<input type="hidden" name="key_hash" value="{real}">'
            f'<input type="hidden" name="enabled" value="{0 if on else 1}">'
        )
        if on:
            toggle += '<button type="submit">Отозвать</button></form>'
        else:
            toggle += '<button class="acc" type="submit">Включить</button></form>'
        delete = (
            f'<form method="post" action="{_PANEL}/keys/delete" '
            f'onsubmit="return confirm(\'Удалить ключ {_esc(k["key_id"])}?\')">'
            f'<input type="hidden" name="_csrf" value="{_esc(csrf)}">'
            f'<input type="hidden" name="period" value="{_esc(period)}">'
            f'<input type="hidden" name="key_hash" value="{real}">'
            '<button class="danger" type="submit">Удалить</button></form>'
        )
        admin_badge = (
            f'<span class="badge b-ok">admin</span>' if k["admin"] else '<span class="muted">—</span>'
        )
        models = ", ".join(k["models"]) if k["models"] else None
        rows.append(
            f"<tr{dead_cls}>"
            f'<td class="mono">{_esc(k["key_id"])}</td>'
            f"<td>{_esc(k['name'])}</td>"
            f"<td>{admin_badge}</td>"
            f"<td>{_num(k['rpm']) if k['rpm'] is not None else '—'}</td>"
            f"<td>{_num(k['tpm']) if k['tpm'] is not None else '—'}</td>"
            f'<td class="mono">{_esc(models) if models else "—"}</td>'
            f'<td><span class="badge {state_cls}">{state_label}</span></td>'
            f'<td class="actions">{toggle}{delete}</td>'
            "</tr>"
        )
    return "\n".join(rows) or '<tr><td colspan="8" class="muted">Ключей нет — добавьте выше</td></tr>'


def _key_stats_rows(usage: dict, keys: list[dict]) -> str:
    """Per-``key_id`` stats from the request log — including deleted keys
    (usage comes from reqlog grouped by key_id; key config is not needed)."""
    by_id = {k["key_id"]: k for k in keys}
    entries = [(kid, st) for kid, st in usage.items() if kid != "total"]
    # most active first; deleted keys with no requests are meaningless to show
    entries.sort(key=lambda kv: (-kv[1]["requests"], kv[0]))
    rows = []
    for kid, st in entries:
        live = by_id.get(kid)
        if live is None:
            state, cls = ("удалён", "b-err")
        elif live["enabled"]:
            state, cls = ("активен", "b-ok")
        else:
            state, cls = ("отозван", "b-off")
        rows.append(
            "<tr>"
            f'<td class="mono">{_esc(kid)}</td>'
            f'<td><span class="badge {cls}">{state}</span></td>'
            f"<td>{_num(st['requests'])}</td>"
            f"<td>{_num(st['errors'])}</td>"
            f"<td>{_num(st['tokens'])}</td>"
            "</tr>"
        )
    return "\n".join(rows) or '<tr><td colspan="5" class="muted">Запросов ещё не было</td></tr>'


def _log_rows(recent: list[dict]) -> str:
    rows = []
    for r in recent:
        ts = datetime.fromtimestamp(r["ts"]).strftime("%d.%m %H:%M:%S")
        toks = (r.get("prompt_tokens") or 0) + (r.get("completion_tokens") or 0)
        err = _esc(r.get("error") or "")
        tr_open = '<tr class="dead">' if (r["status"] >= 400 or r.get("error")) else "<tr>"
        rows.append(
            tr_open
            + f'<td class="mono muted">{ts}</td>'
            f'<td class="mono">{_esc(r["key_id"])}</td>'
            f'<td class="mono">{_esc(r["model"] or r["path"])}</td>'
            f"<td>{r['status']}</td>"
            f"<td>{r['latency_ms']} мс</td>"
            f"<td>{_num(toks) if toks else '—'}</td>"
            f'<td class="mono">{_esc(r.get("account") or "—")}</td>'
            f'<td class="muted">{err}</td>'
            "</tr>"
        )
    return "\n".join(rows) or '<tr><td colspan="8" class="muted">Пусто</td></tr>'


def _cards_html(status: dict, total: dict, period: str = "all") -> str:
    ready = status.get("proxies_ready", status.get("proxies_enabled", 0))
    quota_parked = status.get("egress_groups_quota_parked", 0)
    groups = status.get("egress_groups_total", 0)
    caption = _PERIOD_CAPTIONS.get(period)
    requests_label = "Запросов всего" if not caption else f"Запросов {caption}"
    tokens_label = "Токенов потрачено" if not caption else f"Токенов {caption}"
    errors_label = "Ошибок" if not caption else f"Ошибок {caption}"
    return (
        f'<div class="card"><div class="glyph">↗</div><div class="num">{_num(total.get("requests", 0))}</div>'
        f'<div class="lbl">{requests_label}</div></div>'
        f'<div class="card"><div class="glyph">◈</div><div class="num">{_num(total.get("tokens", 0))}</div>'
        f'<div class="lbl">{tokens_label}</div></div>'
        f'<div class="card"><div class="glyph">!</div><div class="num">{_num(total.get("errors", 0))}</div>'
        f'<div class="lbl">{errors_label}</div></div>'
        f'<div class="card"><div class="glyph">◎</div><div class="num">{ready}/{status.get("proxies_total", 0)}</div>'
        f'<div class="lbl">Прокси в ротации</div></div>'
        f'<div class="card"><div class="glyph">◷</div><div class="num">{status.get("proxies_cooling", 0)} / {status.get("proxies_manually_disabled", 0)}</div>'
        f'<div class="lbl">В cooldown / выключено</div></div>'
        f'<div class="card"><div class="glyph">⊘</div><div class="num">{quota_parked} / {groups}</div>'
        f'<div class="lbl">Прокси в лимите</div></div>'
    )


# Live updates without JS frameworks: keep an SSE stream open and swap the
# ready-made HTML fragments into containers by id. If EventSource is missing,
# fall back to the old JSON endpoint at a 1s interval. A hidden tab closes the
# stream; returning to it opens a fresh one and receives a full snapshot.
# Add-forms are NOT part of the fragments, so typed input is never lost.
_SCRIPT = """<script>
(function () {
  var STREAM_URL = "__STREAM_URL__";
  var DATA_URL = "__DATA_URL__";
  var source = null;
  var pollTimer = null;
  var pollBusy = false;

  function apply(d) {
    if (!d) return;
    for (var id in d) {
      var el = document.getElementById(id);
      if (el && el.innerHTML !== d[id]) el.innerHTML = d[id];
    }
  }

  function tick() {
    if (pollBusy) return;
    pollBusy = true;
    fetch(DATA_URL, { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(apply)
      .catch(function () {})
      .finally(function () { pollBusy = false; });
  }

  function startPolling() {
    if (pollTimer) return;
    tick();
    pollTimer = setInterval(function () { if (!document.hidden) tick(); }, 1000);
  }

  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  function connect() {
    if (!("EventSource" in window)) { startPolling(); return; }
    stopPolling();
    if (source) source.close();
    source = new EventSource(STREAM_URL);
    source.onmessage = function (event) {
      try { apply(JSON.parse(event.data)); } catch (e) {}
    };
    // EventSource reconnects automatically after network/5xx errors.
  }

  function disconnect() {
    if (source) { source.close(); source = null; }
    stopPolling();
  }

  connect();
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) disconnect();
    else connect();
  });
})();
</script>"""


def _now() -> str:
    return datetime.now().strftime("%d.%m.%Y %H:%M:%S")


def _period_control(active: str, base: str) -> str:
    """Segmented control for the spending window (full page reload)."""
    tabs = []
    for key, label, title in _PERIOD_OPTIONS:
        cls = ' class="active"' if key == active else ""
        tabs.append(f'<a href="{base}?period={key}"{cls} title="{title}">{label}</a>')
    return (
        '<div class="period-bar page-enter">'
        '<div class="period-title">Период расходов</div>'
        f'<nav class="period-tabs" aria-label="Период расходов">{"".join(tabs)}</nav>'
        "</div>"
    )


def _header(active: str, csrf: str, period: str = "all") -> str:
    """Shared sidebar, page breadcrumb, and CSRF-protected logout."""
    def nav(href: str, label: str, name: str) -> str:
        target = f"{href}?period={period}" if period != "all" else href
        cls = ' class="active"' if name == active else ""
        return f'<a href="{target}"{cls}>{label}</a>'

    logout = (
        f'<form method="post" action="{_PANEL}/logout">'
        f'<input type="hidden" name="_csrf" value="{_esc(csrf)}">'
        '<button type="submit">↗ &nbsp; Выйти из панели</button></form>'
    )
    return (
        '<header class="top sidebar">'
        '<div class="brand"><span class="brand-mark" aria-hidden="true">OR</span>'
        '<span class="brand-name">OpenRoute<span class="brand-sub">Admin console</span></span></div>'
        '<div class="nav-label">Управление</div>'
        "<nav>"
        + nav(_PANEL, "Прокси", "proxies")
        + nav(f"{_PANEL}/keys", "Ключи", "keys")
        + "</nav>"
        + '<div class="sidebar-bottom">'
        + logout
        + '</div>'
        + "</header>"
    )


def _shell(*, active: str, msg: str, data_url: str, stream_url: str,
           fragments: dict, body: str, csrf: str, period: str = "all") -> str:
    """Page shell: header with navigation, msg banner, body, live-update script."""
    banner = f'<div class="msg">{_esc(msg)}</div>' if msg else ""
    script = (_SCRIPT.replace("__DATA_URL__", data_url)
                     .replace("__STREAM_URL__", stream_url))
    return f"""<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#090e15">
<title>OpenRoute Admin</title><style>{_CSS}</style>
</head><body><div class="wrap">
{_header(active, csrf, period)}
<main class="main">
<div class="topline"><div class="breadcrumb"><span class="crumb-root">Панель управления &nbsp;/&nbsp;</span>
<strong>{"Прокси" if active == "proxies" else "Ключи"}</strong></div>
<div class="top-meta"><span class="live-dot"></span> <span class="meta-label">Данные на</span>
<span id="srv-now">{fragments["srv-now"]}</span></div></div>
{banner}
{body}
{script}
</main></div></body></html>"""


# --- Proxies page (/admin/ui) ----------------------------------------------


def render_proxies_fragments(*, status: dict, usage: dict, usage_by_proxy: dict,
                             recent: list[dict], csrf: str, period: str = "all") -> dict:
    """HTML fragments for the «Прокси» page: ``{element_id: innerHTML}``.

    Ids: ``srv-now``, ``cards``, ``proxies-rows``, ``logs-rows``.
    ``status`` is ``ProxyManager.status()``; ``usage_by_proxy`` keys are masked
    proxies (the ``account`` column in the request log). ``period`` is a
    rolling spending window (``day``/``week``/``month``/``all``).
    """
    total = usage.get("total", {"requests": 0, "errors": 0, "tokens": 0, "avg_latency_ms": 0})
    return {
        "srv-now": _now(),
        "cards": _cards_html(status, total, period),
        "proxies-rows": _proxy_rows(status.get("proxies", []), usage_by_proxy, csrf, period),
        "logs-rows": _log_rows(recent),
    }


def render_proxies_page(*, status: dict, usage: dict, usage_by_proxy: dict,
                        recent: list[dict], csrf: str, msg: str = "",
                        period: str = "all") -> str:
    """Full HTML page «Прокси» (``/admin/ui``)."""
    fr = render_proxies_fragments(status=status, usage=usage,
                                  usage_by_proxy=usage_by_proxy,
                                  recent=recent, csrf=csrf, period=period)
    body = f"""<div class="hero page-enter">
  <div class="hero-content"><div class="eyebrow">Система / обзор</div>
    <h1>Пул прокси</h1>
  </div>
</div>
{_period_control(period, _PANEL)}
<div class="cards page-enter" id="cards">{fr["cards"]}</div>

<section class="section page-enter" aria-labelledby="pool-title">
<div class="section-head"><div class="section-title"><span class="section-icon" aria-hidden="true">◎</span>
<h2 id="pool-title">Пул прокси</h2></div><span class="section-note">Расходы: {_esc(_PERIOD_SECTIONS.get(period, "За всё время").lower())}</span></div>
<div class="table-scroll"><table>
<thead>
<tr><th>Прокси</th><th>Статус</th><th>Запросы</th><th>Ошибки</th>
<th>Токены</th><th>Последняя ошибка</th><th>Действия</th></tr>
</thead>
<tbody id="proxies-rows">
{fr["proxies-rows"]}
</tbody>
</table></div></section>

<section class="section page-enter" aria-labelledby="requests-title">
<div class="section-head"><div class="section-title"><span class="section-icon" aria-hidden="true">↗</span>
<h2 id="requests-title">Последние запросы</h2></div><span class="section-note">Журнал активности</span></div>
<div class="table-scroll"><table>
<thead>
<tr><th>Время</th><th>Key</th><th>Модель</th><th>Статус</th><th>Задержка</th>
<th>Токены</th><th>Прокси</th><th>Ошибка</th></tr>
</thead>
<tbody id="logs-rows">
{fr["logs-rows"]}
</tbody>
</table></div></section>"""
    return _shell(active="proxies", msg=msg,
                  data_url=f"{_PANEL}/data?period={period}",
                  stream_url=f"{_PANEL}/events?period={period}",
                  fragments=fr, body=body, csrf=csrf, period=period)


# --- Keys page (/admin/ui/keys) --------------------------------------------


def render_keys_fragments(*, keys: list[dict], usage: dict, csrf: str,
                          period: str = "all") -> dict:
    """HTML fragments for the «Ключи» page: ``{element_id: innerHTML}``.

    Ids: ``srv-now``, ``keys-rows``, ``key-stats-rows``.
    """
    return {
        "srv-now": _now(),
        "keys-rows": _key_rows(keys, csrf, period),
        "key-stats-rows": _key_stats_rows(usage, keys),
    }


def render_keys_page(*, keys: list[dict], usage: dict, csrf: str, msg: str = "",
                     period: str = "all") -> str:
    """Full HTML page «Ключи» (``/admin/ui/keys``)."""
    fr = render_keys_fragments(keys=keys, usage=usage, csrf=csrf, period=period)
    body = f"""<div class="hero page-enter">
  <div class="hero-content"><div class="eyebrow">Доступ / управление</div>
    <h1>Клиентские ключи</h1>
  </div>
</div>
<section class="section page-enter" aria-labelledby="add-title">
<div class="section-head"><div class="section-title"><span class="section-icon" aria-hidden="true">＋</span>
<h2 id="add-title">Добавить клиентский ключ</h2></div><span class="section-note">Новый доступ</span></div>
<div class="form-content"><form class="add" method="post" action="{_PANEL}/keys/add">
  <input type="hidden" name="_csrf" value="{_esc(csrf)}">
  <input type="hidden" name="period" value="{_esc(period)}">
  <div class="field wide"><label for="key">Ключ доступа</label>
    <input id="key" type="text" name="key" placeholder="sk-..." autocomplete="off" required></div>
  <div class="field"><label for="key_id">Key ID</label>
    <input id="key_id" type="text" name="key_id" placeholder="Генерируется автоматически" autocomplete="off"></div>
  <div class="field"><label for="key_name">Имя</label>
    <input id="key_name" type="text" name="name" placeholder="Например, Frontend" autocomplete="off"></div>
  <div class="field"><label for="rpm">RPM</label>
    <input id="rpm" type="text" inputmode="numeric" name="rpm" placeholder="Без лимита" autocomplete="off"></div>
  <div class="field"><label for="tpm">TPM</label>
    <input id="tpm" type="text" inputmode="numeric" name="tpm" placeholder="Без лимита" autocomplete="off"></div>
  <div class="field wide"><label for="models">Доступные модели</label>
    <input id="models" type="text" name="models" placeholder="Через запятую; пусто — все модели" autocomplete="off"></div>
  <div class="form-footer"><label class="check-field"><input type="checkbox" name="admin" value="1"> Администратор</label>
    <button type="submit">＋ &nbsp; Добавить ключ</button></div>
</form></div></section>

<section class="section page-enter" aria-labelledby="keys-title">
<div class="section-head"><div class="section-title"><span class="section-icon" aria-hidden="true">◇</span>
<h2 id="keys-title">Клиентские ключи</h2></div><span class="section-note">Управление доступом</span></div>
<div class="table-scroll"><table>
<thead>
<tr><th>Key ID</th><th>Имя</th><th>Admin</th><th>RPM</th><th>TPM</th>
<th>Модели</th><th>Статус</th><th>Действия</th></tr>
</thead>
<tbody id="keys-rows">
{fr["keys-rows"]}
</tbody>
</table></div></section>

{_period_control(period, f"{_PANEL}/keys")}
<section class="section page-enter" aria-labelledby="stats-title">
<div class="section-head"><div class="section-title"><span class="section-icon" aria-hidden="true">↗</span>
<h2 id="stats-title">Статистика по ключам</h2></div><span class="section-note">{_esc(_PERIOD_SECTIONS.get(period, "За всё время"))}</span></div>
<div class="table-scroll"><table>
<thead>
<tr><th>Key ID</th><th>Статус</th><th>Запросы</th><th>Ошибки</th>
<th>Токены</th></tr>
</thead>
<tbody id="key-stats-rows">
{fr["key-stats-rows"]}
</tbody>
</table></div></section>"""
    return _shell(active="keys", msg=msg,
                  data_url=f"{_PANEL}/keys/data?period={period}",
                  stream_url=f"{_PANEL}/keys/events?period={period}",
                  fragments=fr, body=body, csrf=csrf, period=period)


# --- Login page (/login): PASSWORD ONLY, no username -----------------------


def render_login_page(err: str = "") -> str:
    """Admin login form: a single field — password (``ADMIN_PASSWORD``).
    The error is inline and never leaks detail (plain «wrong password»); the
    field is cleared by the browser via autocomplete."""
    err_html = f'<div class="err">{_esc(err)}</div>' if err else ""
    return f"""<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#090e15">
<title>OpenRoute Admin — вход</title><style>{_CSS}</style>
</head><body><div class="login-page">
<aside class="login-visual">
  <div class="login-brand"><span class="brand-mark" aria-hidden="true">OR</span> OpenRoute</div>
  <div class="login-statement"><div class="eyebrow">Административная панель</div>
    <h2>Каждый маршрут<br>под контролем.</h2>
    <p>Единое пространство для управления прокси, ключами и активностью сервиса.</p>
  </div><div class="login-foot">OPENROUTE &nbsp; / &nbsp; CONTROL PLANE</div>
</aside>
<main class="login-main"><div class="login-wrap"><div class="login-box">
<div class="login-kicker">Защищённый доступ</div><h1>OpenRoute Admin</h1>
<p class="sub">Введите пароль администратора, чтобы продолжить.</p>
<form method="post" action="/login">
  <label for="password">Пароль</label>
  <input id="password" type="password" name="password" placeholder="Введите пароль"
         autocomplete="current-password" autofocus required>
  <button type="submit">Войти в панель &nbsp; ↗</button>
</form>
{err_html}
<div class="login-help">Доступ к панели защищён серверной сессией</div>
</div></div></main></div></body></html>"""
