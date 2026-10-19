"""Server-rendered HTML admin UI (no template engine, no JS frameworks).

Module of the ``admin-service`` microservice. Multi-page: «Прокси» (``/admin/ui``)
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

Data shapes (FROZEN by DESIGN.md §2):

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
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin: 0; font: 14px/1.45 -apple-system, "Segoe UI", Roboto, sans-serif;
       background: #0d1117; color: #c9d1d9; }
.wrap { max-width: 1180px; margin: 0 auto; padding: 24px 16px 64px; }
header.top { display: flex; align-items: baseline; gap: 16px; flex-wrap: wrap;
             border-bottom: 1px solid #21262d; padding-bottom: 12px; }
header.top h1 { font-size: 20px; margin: 0; color: #58a6ff; }
header.top nav { display: flex; gap: 16px; }
header.top nav a { padding-bottom: 2px; border-bottom: 2px solid transparent; }
header.top nav a.active { border-bottom-color: #58a6ff; font-weight: 600; }
header.top .muted { margin-left: auto; }
a { color: #58a6ff; text-decoration: none; }
a:hover { text-decoration: underline; }
.muted { color: #8b949e; }
.msg { margin: 14px 0 0; padding: 10px 14px; border: 1px solid #1f6feb55;
       background: #1f6feb22; border-radius: 8px; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
         gap: 12px; margin: 18px 0; }
.card { background: #161b22; border: 1px solid #21262d; border-radius: 10px;
        padding: 14px 16px; }
.card .num { font-size: 24px; font-weight: 700; }
.card .lbl { color: #8b949e; margin-top: 2px; }
h2 { font-size: 15px; text-transform: uppercase; letter-spacing: .06em;
     color: #8b949e; margin: 28px 0 10px; }
table { width: 100%; border-collapse: collapse; background: #161b22;
        border: 1px solid #21262d; border-radius: 10px; overflow: hidden; }
th, td { padding: 8px 10px; text-align: left; border-bottom: 1px solid #21262d;
         font-size: 13px; vertical-align: middle; }
th { color: #8b949e; font-weight: 600; }
tr:last-child td { border-bottom: none; }
tr.dead td { opacity: .55; }
td.mono, code { font-family: ui-monospace, Consolas, monospace; }
.badge { display: inline-block; padding: 1px 8px; border-radius: 999px;
         font-size: 12px; border: 1px solid transparent; }
.b-ok { color: #3fb950; border-color: #23863666; background: #23863622; }
.b-warn { color: #d29922; border-color: #9e6a0366; background: #9e6a0322; }
.b-err { color: #f85149; border-color: #da363366; background: #da363322; }
.b-off { color: #8b949e; border-color: #30363d; background: #21262d; }
button { font: inherit; font-size: 12px; padding: 3px 10px; border-radius: 6px;
         border: 1px solid #30363d; background: #21262d; color: #c9d1d9;
         cursor: pointer; }
button:hover { background: #30363d; }
button.danger { color: #f85149; }
button.acc { color: #3fb950; }
td.actions { white-space: nowrap; }
td.actions form { display: inline-block; margin: 0 2px 0 0; }
form.add { display: flex; gap: 8px; align-items: stretch; flex-wrap: wrap; }
form.add input[type=text] { flex: 1; min-width: 110px; font: inherit; color: #c9d1d9;
    background: #0d1117; border: 1px solid #30363d; border-radius: 8px;
    padding: 8px 12px; }
form.add input.wide { flex: 2; min-width: 230px; }
form.add input[type=text]:focus { outline: none; border-color: #1f6feb; }
form.add label.chk { display: flex; align-items: center; gap: 6px; padding: 0 10px;
    border: 1px solid #30363d; border-radius: 8px; color: #c9d1d9; white-space: nowrap; }
form.add button { font-size: 13px; padding: 8px 16px; }
.login-wrap { max-width: 340px; margin: 12vh auto 0; }
.login-box { background: #161b22; border: 1px solid #21262d; border-radius: 12px;
             padding: 28px 26px 24px; }
.login-box h1 { font-size: 18px; margin: 0 0 4px; color: #58a6ff; }
.login-box p.sub { margin: 0 0 18px; color: #8b949e; }
.login-box input[type=password] { width: 100%; font: inherit; color: #c9d1d9;
     background: #0d1117; border: 1px solid #30363d; border-radius: 8px;
     padding: 10px 12px; margin-bottom: 12px; }
.login-box input[type=password]:focus { outline: none; border-color: #1f6feb; }
.login-box button[type=submit] { width: 100%; font-size: 14px; padding: 10px 0; }
.err { margin: 14px 0 0; padding: 10px 14px; border: 1px solid #da363366;
       background: #da363322; border-radius: 8px; color: #f85149; }
"""

# Proxy states. Unlike NVRoute's accounts there is no permanently "dead" state:
# proxies are never disabled permanently — sustained failure is expressed as a
# cooldown only (DESIGN.md §5.1). "disabled"/"dead" are still read defensively
# so the renderer keeps working if the gateway adds such a flag later.
_STATE_LABELS = {
    "ok": ("ok", "b-ok"),
    "cooldown": ("cooldown", "b-warn"),
    "rpm": ("RPM-кап", "b-warn"),
    "noproxy": ("нет в пуле", "b-warn"),
    "off": ("выключен", "b-off"),
    "dead": ("мёртв", "b-err"),
}


def _proxy_state(s: dict) -> str:
    """Classify one status row into a state key (see ``_STATE_LABELS``)."""
    if s.get("disabled"):
        return "dead"
    if s.get("manual"):
        return "off"
    if not s.get("enabled", True):
        return "noproxy"
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


def _proxy_rows(proxies: list[dict], usage: dict, csrf: str) -> str:
    rows = []
    for s in proxies:
        state = _proxy_state(s)
        label, cls = _STATE_LABELS[state]
        if state == "cooldown":
            label = f"cooldown {float(s.get('cooldown_seconds_left') or 0):.0f}s"
        u = usage.get(_proxy_display(s), {}) or {}
        dead_cls = ' class="dead"' if state in ("dead", "off") else ""
        real = _esc(_proxy_identity(s))
        toggle = (
            f'<form method="post" action="{_PANEL}/proxies/set">'
            f'<input type="hidden" name="_csrf" value="{_esc(csrf)}">'
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
            f'<input type="hidden" name="proxy" value="{real}">'
            '<button class="danger" type="submit">Удалить</button></form>'
        )
        rows.append(
            f"<tr{dead_cls}>"
            f'<td class="mono">{_esc(_proxy_display(s))}</td>'
            f'<td><span class="badge {cls}">{label}</span></td>'
            f"<td>{_num(s.get('requests', 0))}</td>"
            f"<td>{_num(s.get('errors', 0))}</td>"
            f"<td>{_num(u.get('tokens', 0))}</td>"
            f'<td class="muted">{_esc(s.get("last_error") or "")}</td>'
            f'<td class="actions">{toggle}{delete}</td>'
            "</tr>"
        )
    return "\n".join(rows) or '<tr><td colspan="7" class="muted">Прокси нет</td></tr>'


def _key_rows(keys: list[dict], csrf: str) -> str:
    rows = []
    for k in keys:
        on = k["enabled"]
        state_label, state_cls = ("активен", "b-ok") if on else ("отозван", "b-off")
        dead_cls = "" if on else ' class="dead"'
        real = _esc(k["key_hash"])  # sha256 — не секрет, годится для hidden-полей
        toggle = (
            f'<form method="post" action="{_PANEL}/keys/set">'
            f'<input type="hidden" name="_csrf" value="{_esc(csrf)}">'
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


def _cards_html(status: dict, total: dict) -> str:
    return (
        f'<div class="card"><div class="num">{_num(total.get("requests", 0))}</div>'
        f'<div class="lbl">Запросов всего</div></div>'
        f'<div class="card"><div class="num">{_num(total.get("tokens", 0))}</div>'
        f'<div class="lbl">Токенов потрачено</div></div>'
        f'<div class="card"><div class="num">{_num(total.get("errors", 0))}</div>'
        f'<div class="lbl">Ошибок</div></div>'
        f'<div class="card"><div class="num">{status.get("proxies_enabled", 0)}/{status.get("proxies_total", 0)}</div>'
        f'<div class="lbl">Прокси в ротации</div></div>'
        f'<div class="card"><div class="num">{status.get("proxies_cooling", 0)} / {status.get("proxies_manually_disabled", 0)}</div>'
        f'<div class="lbl">В cooldown / выключено</div></div>'
    )


# Auto-refresh without JS frameworks: every N seconds fetch the page's data
# endpoint and swap the ready-made HTML fragments into containers by id.
# A hidden tab does not poll; network errors are swallowed — the next tick
# retries. Add-forms are NOT reloaded, so input is never lost.
_SCRIPT = """<script>
(function () {
  var URL = "__DATA_URL__";
  function tick() {
    fetch(URL).then(function (r) { return r.ok ? r.json() : null; }).then(function (d) {
      if (!d) return;
      for (var id in d) {
        var el = document.getElementById(id);
        if (el) el.innerHTML = d[id];
      }
    }).catch(function () {});
  }
  setInterval(function () { if (!document.hidden) tick(); }, 5000);
})();
</script>"""


def _now() -> str:
    return datetime.now().strftime("%d.%m.%Y %H:%M:%S")


def _header(active: str, srv_now: str, csrf: str) -> str:
    """Shared header with navigation; the active page is highlighted.
    On the right — server time and the «Выйти» button (POST with session CSRF)."""
    def nav(href: str, label: str, name: str) -> str:
        cls = ' class="active"' if name == active else ""
        return f'<a href="{href}"{cls}>{label}</a>'

    logout = (
        f'<form method="post" action="{_PANEL}/logout">'
        f'<input type="hidden" name="_csrf" value="{_esc(csrf)}">'
        '<button type="submit">Выйти</button></form>'
    )
    return (
        '<header class="top">'
        "<h1>OpenRoute Admin</h1>"
        "<nav>"
        + nav(_PANEL, "Прокси", "proxies")
        + nav(f"{_PANEL}/keys", "Ключи", "keys")
        + "</nav>"
        + f'<span class="muted" id="srv-now">{srv_now}</span>'
        + logout
        + "</header>"
    )


def _shell(*, active: str, msg: str, data_url: str, fragments: dict, body: str,
           csrf: str) -> str:
    """Page shell: header with navigation, msg banner, body, refresh script."""
    banner = f'<div class="msg">{_esc(msg)}</div>' if msg else ""
    script = _SCRIPT.replace("__DATA_URL__", data_url)
    return f"""<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>OpenRoute Admin</title><style>{_CSS}</style>
</head><body><div class="wrap">
{_header(active, fragments["srv-now"], csrf)}
{banner}
{body}
{script}
</div></body></html>"""


# --- Proxies page (/admin/ui) ----------------------------------------------


def render_proxies_fragments(*, status: dict, usage: dict, usage_by_proxy: dict,
                             recent: list[dict], csrf: str) -> dict:
    """HTML fragments for the «Прокси» page: ``{element_id: innerHTML}``.

    Ids: ``srv-now``, ``cards``, ``proxies-rows``, ``logs-rows``.
    ``status`` is ``ProxyManager.status()``; ``usage_by_proxy`` keys are masked
    proxies (the ``account`` column in the request log).
    """
    total = usage.get("total", {"requests": 0, "errors": 0, "tokens": 0, "avg_latency_ms": 0})
    return {
        "srv-now": _now(),
        "cards": _cards_html(status, total),
        "proxies-rows": _proxy_rows(status.get("proxies", []), usage_by_proxy, csrf),
        "logs-rows": _log_rows(recent),
    }


def render_proxies_page(*, status: dict, usage: dict, usage_by_proxy: dict,
                        recent: list[dict], csrf: str, msg: str = "") -> str:
    """Full HTML page «Прокси» (``/admin/ui``)."""
    fr = render_proxies_fragments(status=status, usage=usage,
                                  usage_by_proxy=usage_by_proxy,
                                  recent=recent, csrf=csrf)
    body = f"""<div class="cards" id="cards">{fr["cards"]}</div>

<h2>Пул прокси</h2>
<table>
<thead>
<tr><th>Прокси</th><th>Статус</th><th>Запросы</th><th>Ошибки</th>
<th>Токены</th><th>Последняя ошибка</th><th>Действия</th></tr>
</thead>
<tbody id="proxies-rows">
{fr["proxies-rows"]}
</tbody>
</table>

<h2>Последние запросы</h2>
<table>
<thead>
<tr><th>Время</th><th>Key</th><th>Модель</th><th>Статус</th><th>Задержка</th>
<th>Токены</th><th>Прокси</th><th>Ошибка</th></tr>
</thead>
<tbody id="logs-rows">
{fr["logs-rows"]}
</tbody>
</table>"""
    return _shell(active="proxies", msg=msg, data_url=f"{_PANEL}/data",
                  fragments=fr, body=body, csrf=csrf)


# --- Keys page (/admin/ui/keys) --------------------------------------------


def render_keys_fragments(*, keys: list[dict], usage: dict, csrf: str) -> dict:
    """HTML fragments for the «Ключи» page: ``{element_id: innerHTML}``.

    Ids: ``srv-now``, ``keys-rows``, ``key-stats-rows``.
    """
    return {
        "srv-now": _now(),
        "keys-rows": _key_rows(keys, csrf),
        "key-stats-rows": _key_stats_rows(usage, keys),
    }


def render_keys_page(*, keys: list[dict], usage: dict, csrf: str, msg: str = "") -> str:
    """Full HTML page «Ключи» (``/admin/ui/keys``)."""
    fr = render_keys_fragments(keys=keys, usage=usage, csrf=csrf)
    body = f"""<h2>Добавить клиентский ключ</h2>
<form class="add" method="post" action="{_PANEL}/keys/add">
  <input type="hidden" name="_csrf" value="{_esc(csrf)}">
  <input type="text" class="wide" name="key" placeholder="sk-... (plaintext; сохраняется только sha256)" autocomplete="off">
  <input type="text" name="key_id" placeholder="key_id (необязательно)" autocomplete="off">
  <input type="text" name="name" placeholder="Имя (необязательно)" autocomplete="off">
  <input type="text" name="rpm" placeholder="RPM (пусто = без лимита)" autocomplete="off">
  <input type="text" name="tpm" placeholder="TPM (пусто = без лимита)" autocomplete="off">
  <input type="text" name="models" placeholder="Модели через запятую (пусто = все)" autocomplete="off">
  <label class="chk"><input type="checkbox" name="admin" value="1"> admin</label>
  <button type="submit">Добавить</button>
</form>

<h2>Клиентские ключи</h2>
<table>
<thead>
<tr><th>Key ID</th><th>Имя</th><th>Admin</th><th>RPM</th><th>TPM</th>
<th>Модели</th><th>Статус</th><th>Действия</th></tr>
</thead>
<tbody id="keys-rows">
{fr["keys-rows"]}
</tbody>
</table>

<h2>Статистика по ключам</h2>
<table>
<thead>
<tr><th>Key ID</th><th>Статус</th><th>Запросы</th><th>Ошибки</th>
<th>Токены</th></tr>
</thead>
<tbody id="key-stats-rows">
{fr["key-stats-rows"]}
</tbody>
</table>"""
    return _shell(active="keys", msg=msg, data_url=f"{_PANEL}/keys/data",
                  fragments=fr, body=body, csrf=csrf)


# --- Login page (/login): PASSWORD ONLY, no username -----------------------


def render_login_page(err: str = "") -> str:
    """Admin login form: a single field — password (``ADMIN_PASSWORD``).
    The error is inline and never leaks detail (plain «wrong password»); the
    field is cleared by the browser via autocomplete."""
    err_html = f'<div class="err">{_esc(err)}</div>' if err else ""
    return f"""<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>OpenRoute Admin — вход</title><style>{_CSS}</style>
</head><body><div class="wrap"><div class="login-wrap"><div class="login-box">
<h1>OpenRoute Admin</h1>
<p class="sub">Введите пароль администратора</p>
<form method="post" action="/login">
  <input type="password" name="password" placeholder="Пароль"
         autocomplete="current-password" autofocus required>
  <button type="submit">Войти</button>
</form>
{err_html}
</div></div></div></body></html>"""
