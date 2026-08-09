"""Localhost configuration panel.

A small web UI for editing config.json without hand-editing JSON. It reads the
live config, offers real channel/role dropdowns pulled from Discord's REST API,
and writes changes back atomically.

    pip install -r panel/requirements.txt
    python panel/app.py
    # open http://127.0.0.1:5000

Binds to 127.0.0.1 only. It edits your bot's config and can read your token from
.env, so it must never be exposed to a network.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template_string, request, url_for

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CONFIG_PATH = ROOT / "config.json"
DATA_DIR = ROOT / "data"

app = Flask(__name__)

# Discord REST is cached for the process lifetime; the panel is short-lived and
# a server's roles don't change while you're filling in a form.
_cache: dict[str, object] = {}


# -- config io ------------------------------------------------------------


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def save_config(cfg: dict) -> None:
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(CONFIG_PATH)


def dig(cfg: dict, dotted: str, default=None):
    node = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return default if node is None else node


def put(cfg: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    node = cfg
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


# -- discord rest ---------------------------------------------------------


def token() -> str | None:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("DISCORD_TOKEN="):
                return line.split("=", 1)[1].strip()
    return os.getenv("DISCORD_TOKEN")


def api(path: str):
    tok = token()
    if not tok:
        return None
    req = urllib.request.Request(
        f"https://discord.com/api/v10{path}",
        headers={"Authorization": f"Bot {tok}", "User-Agent": "BlueprintPanel/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.load(resp)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return None


def guild_roles(guild_id) -> list[dict]:
    if not guild_id:
        return []
    key = f"roles:{guild_id}"
    if key not in _cache:
        rows = api(f"/guilds/{guild_id}/roles") or []
        rows = [r for r in rows if r["name"] != "@everyone"]
        rows.sort(key=lambda r: r.get("position", 0), reverse=True)
        _cache[key] = rows
    return _cache[key]  # type: ignore[return-value]


def guild_channels(guild_id) -> list[dict]:
    if not guild_id:
        return []
    key = f"channels:{guild_id}"
    if key not in _cache:
        rows = api(f"/guilds/{guild_id}/channels") or []
        # 0 = text, 5 = announcement; only those can take a log message.
        rows = [c for c in rows if c.get("type") in (0, 5)]
        rows.sort(key=lambda c: c.get("position", 0))
        _cache[key] = rows
    return _cache[key]  # type: ignore[return-value]


def global_bans() -> list[dict]:
    path = DATA_DIR / "globalbans.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    rows = list((data.get("bans") or {}).values())
    rows.sort(key=lambda r: r.get("at", 0), reverse=True)
    return rows


# -- form handling --------------------------------------------------------

CHANNEL_KEYS = ["security_log", "mod_log"]

ANTINUKE_ACTIONS = [
    "ban", "kick", "channel_delete", "channel_create", "role_delete",
    "role_create", "webhook_create", "role_grant", "prune", "guild_update",
]


def as_int(value, default=None):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def id_list(raw: str) -> list[int]:
    out = []
    for chunk in (raw or "").replace(",", " ").split():
        n = as_int(chunk)
        if n:
            out.append(n)
    return out


@app.route("/", methods=["GET"])
def index():
    cfg = load_config()
    gid = dig(cfg, "guild_id")
    return render_template_string(
        TEMPLATE,
        cfg=cfg,
        dig=lambda k, d=None: dig(cfg, k, d),
        roles=guild_roles(gid),
        channels=guild_channels(gid),
        channel_keys=CHANNEL_KEYS,
        antinuke_actions=ANTINUKE_ACTIONS,
        bans=global_bans(),
        connected=bool(token()) and bool(guild_roles(gid)),
        saved=request.args.get("saved"),
    )


@app.route("/save", methods=["POST"])
def save():
    cfg = load_config()
    f = request.form

    put(cfg, "guild_id", as_int(f.get("guild_id")))
    put(cfg, "accent_color", f.get("accent_color") or "0x3B82F6")
    put(cfg, "branding.name", f.get("branding_name") or "Blueprint")
    put(cfg, "branding.footer", f.get("branding_footer") or "")

    for tier in ("admin", "hr", "support", "designer"):
        put(cfg, f"roles.{tier}", id_list(f.get(f"role_{tier}", "")))

    for key in CHANNEL_KEYS:
        put(cfg, f"channels.{key}", as_int(f.get(f"channel_{key}")))

    put(cfg, "security.gban_role", as_int(f.get("gban_role")))
    put(cfg, "security.quarantine_role", as_int(f.get("quarantine_role")))
    put(cfg, "security.whitelist_users", id_list(f.get("whitelist_users", "")))
    put(cfg, "security.whitelist_roles", id_list(f.get("whitelist_roles", "")))

    put(cfg, "security.antinuke.enabled", f.get("antinuke_enabled") == "on")
    put(cfg, "security.antinuke.punishment", f.get("antinuke_punishment", "quarantine"))
    for action in ANTINUKE_ACTIONS:
        count = as_int(f.get(f"an_{action}_count"))
        window = as_int(f.get(f"an_{action}_window"))
        if count and window:
            put(cfg, f"security.antinuke.thresholds.{action}", {"count": count, "window": window})

    put(cfg, "security.antiraid.enabled", f.get("antiraid_enabled") == "on")
    put(cfg, "security.antiraid.join_limit", as_int(f.get("join_limit"), 6))
    put(cfg, "security.antiraid.join_window", as_int(f.get("join_window"), 10))
    put(cfg, "security.antiraid.min_account_age_hours", as_int(f.get("min_account_age_hours"), 0))
    put(cfg, "security.antiraid.young_account_action", f.get("young_account_action", "hold"))
    put(cfg, "security.antiraid.lockdown_minutes", as_int(f.get("lockdown_minutes"), 10))

    put(cfg, "presence.enabled", f.get("presence_enabled") == "on")
    put(cfg, "presence.interval", max(as_int(f.get("presence_interval"), 45) or 45, 15))

    statuses = []
    for kind, text in zip(f.getlist("presence_type"), f.getlist("presence_text")):
        if text.strip():
            statuses.append({"type": kind, "text": text.strip()})
    if statuses:
        put(cfg, "presence.statuses", statuses)

    save_config(cfg)
    return redirect(url_for("index", saved=1))


@app.route("/api/config")
def api_config():
    return jsonify(load_config())


TEMPLATE = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Blueprint — Config</title>
<style>
  :root{
    --bg:#0d1117; --panel:#161b22; --panel2:#1c2230; --line:#26303d;
    --text:#e6edf3; --muted:#8b949e; --accent:#3b82f6; --ok:#43b581;
    --err:#f04747; --warn:#faa61a; --radius:10px;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
       font:14px/1.55 ui-sans-serif,system-ui,"Segoe UI",Roboto,sans-serif}
  header{background:linear-gradient(100deg,#132038,#0d1117);border-bottom:1px solid var(--line);
         padding:22px 28px;display:flex;align-items:center;gap:16px;position:sticky;top:0;z-index:10}
  header .mark{width:6px;height:34px;background:var(--accent);border-radius:3px}
  header h1{margin:0;font-size:19px;letter-spacing:.3px}
  header .sub{color:var(--muted);font-size:12.5px;margin-top:2px}
  .pill{margin-left:auto;font-size:12px;padding:5px 11px;border-radius:999px;
        border:1px solid var(--line);color:var(--muted)}
  .pill.on{color:var(--ok);border-color:#1d3b2f;background:#12241d}
  .pill.off{color:var(--warn);border-color:#3d3222;background:#241f12}
  main{max-width:1080px;margin:0 auto;padding:26px 20px 90px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
        padding:20px 22px;margin-bottom:18px}
  .card h2{margin:0 0 4px;font-size:15px;letter-spacing:.2px}
  .card p.hint{margin:0 0 16px;color:var(--muted);font-size:12.5px}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:14px}
  label{display:block;font-size:12px;color:var(--muted);margin-bottom:5px}
  input,select{width:100%;background:var(--panel2);border:1px solid var(--line);color:var(--text);
        padding:9px 11px;border-radius:7px;font:inherit;font-size:13px}
  input:focus,select:focus{outline:none;border-color:var(--accent)}
  .row{display:flex;gap:10px;align-items:center}
  .row input[type=number]{width:90px}
  .thresh{display:grid;grid-template-columns:1.4fr .8fr .8fr;gap:10px;align-items:center;
          padding:7px 0;border-bottom:1px solid #1e2530}
  .thresh:last-child{border-bottom:none}
  .thresh code{color:var(--text);font-size:12.5px}
  .thresh small{color:var(--muted);display:block;font-size:11px}
  .switch{display:flex;align-items:center;gap:9px;margin-bottom:14px}
  .switch input{width:auto}
  table{width:100%;border-collapse:collapse;font-size:12.5px}
  th{text-align:left;color:var(--muted);font-weight:500;padding:7px 8px;border-bottom:1px solid var(--line)}
  td{padding:8px;border-bottom:1px solid #1b222c}
  .empty{color:var(--muted);padding:16px 0;text-align:center;font-size:13px}
  .bar{position:fixed;left:0;right:0;bottom:0;background:#0f1620ee;backdrop-filter:blur(8px);
       border-top:1px solid var(--line);padding:13px 28px;display:flex;align-items:center;gap:14px}
  button{background:var(--accent);color:#fff;border:0;padding:10px 22px;border-radius:7px;
         font:inherit;font-weight:600;cursor:pointer}
  button:hover{background:#2f6fd8}
  .saved{color:var(--ok);font-size:13px}
  .warn{color:var(--warn);font-size:12.5px}
  .stat{display:flex;gap:22px;flex-wrap:wrap}
  .stat div span{display:block;font-size:20px;font-weight:600}
  .stat div small{color:var(--muted);font-size:11.5px}
</style>
</head>
<body>
<header>
  <div class="mark"></div>
  <div>
    <h1>Blueprint Configuration</h1>
    <div class="sub">config.json &middot; localhost only</div>
  </div>
  {% if connected %}
    <span class="pill on">Connected to Discord</span>
  {% else %}
    <span class="pill off">Offline &mdash; enter IDs by hand</span>
  {% endif %}
</header>

<main>
<form method="post" action="/save">

  <div class="card">
    <h2>Server</h2>
    <p class="hint">The home server. Global-ban permission is always checked here, whichever server a command is run in.</p>
    <div class="grid">
      <div>
        <label>Guild ID</label>
        <input name="guild_id" value="{{ dig('guild_id','') }}">
      </div>
      <div>
        <label>Bot name</label>
        <input name="branding_name" value="{{ dig('branding.name','Blueprint') }}">
      </div>
      <div>
        <label>Footer text</label>
        <input name="branding_footer" value="{{ dig('branding.footer','') }}">
      </div>
      <div>
        <label>Accent colour</label>
        <input name="accent_color" value="{{ dig('accent_color','0x3B82F6') }}">
      </div>
    </div>
  </div>

  <div class="card">
    <h2>Security roles</h2>
    <p class="hint">Who can issue global bans, and who anti-nuke must never touch.</p>
    <div class="grid">
      <div>
        <label>Global ban role &mdash; only this role can /gban</label>
        {% if roles %}
        <select name="gban_role">
          <option value="">none</option>
          {% for r in roles %}
            <option value="{{ r.id }}" {% if dig('security.gban_role')|string == r.id %}selected{% endif %}>{{ r.name }}</option>
          {% endfor %}
        </select>
        {% else %}
        <input name="gban_role" value="{{ dig('security.gban_role','') }}">
        {% endif %}
      </div>
      <div>
        <label>Quarantine role &mdash; used to hold raiders</label>
        {% if roles %}
        <select name="quarantine_role">
          <option value="">none</option>
          {% for r in roles %}
            <option value="{{ r.id }}" {% if dig('security.quarantine_role')|string == r.id %}selected{% endif %}>{{ r.name }}</option>
          {% endfor %}
        </select>
        {% else %}
        <input name="quarantine_role" value="{{ dig('security.quarantine_role','') }}">
        {% endif %}
      </div>
      <div>
        <label>Whitelisted role IDs &mdash; exempt from anti-nuke</label>
        <input name="whitelist_roles" value="{{ dig('security.whitelist_roles',[])|join(' ') }}">
      </div>
      <div>
        <label>Whitelisted user IDs</label>
        <input name="whitelist_users" value="{{ dig('security.whitelist_users',[])|join(' ') }}">
      </div>
    </div>
  </div>

  <div class="card">
    <h2>Staff tiers</h2>
    <p class="hint">Space-separated role IDs. Admin satisfies every other tier.</p>
    <div class="grid">
      {% for tier in ['admin','hr','support','designer'] %}
      <div>
        <label>{{ tier }}</label>
        <input name="role_{{ tier }}" value="{{ dig('roles.' ~ tier, [])|join(' ') }}">
      </div>
      {% endfor %}
    </div>
  </div>

  <div class="card">
    <h2>Log channels</h2>
    <p class="hint">Security events post to the security log. Unset means that logging is simply off.</p>
    <div class="grid">
      {% for key in channel_keys %}
      <div>
        <label>{{ key }}</label>
        {% if channels %}
        <select name="channel_{{ key }}">
          <option value="">none</option>
          {% for c in channels %}
            <option value="{{ c.id }}" {% if dig('channels.' ~ key)|string == c.id %}selected{% endif %}>#{{ c.name }}</option>
          {% endfor %}
        </select>
        {% else %}
        <input name="channel_{{ key }}" value="{{ dig('channels.' ~ key,'') }}">
        {% endif %}
      </div>
      {% endfor %}
    </div>
  </div>

  <div class="card">
    <h2>Anti-nuke</h2>
    <p class="hint">Counts destructive audit-log actions per person. Cross a threshold and the response fires.</p>
    <div class="switch">
      <input type="checkbox" id="an" name="antinuke_enabled" {% if dig('security.antinuke.enabled',True) %}checked{% endif %}>
      <label for="an" style="margin:0">Enabled</label>
    </div>
    <div class="grid" style="margin-bottom:14px">
      <div>
        <label>Response</label>
        <select name="antinuke_punishment">
          {% for opt in ['quarantine','ban','kick','alert'] %}
            <option value="{{ opt }}" {% if dig('security.antinuke.punishment','quarantine')==opt %}selected{% endif %}>{{ opt }}</option>
          {% endfor %}
        </select>
      </div>
    </div>
    {% for action in antinuke_actions %}
      {% set t = dig('security.antinuke.thresholds.' ~ action, {}) %}
      <div class="thresh">
        <div><code>{{ action }}</code><small>triggers after this many, inside the window</small></div>
        <div><label>count</label><input type="number" min="1" name="an_{{ action }}_count" value="{{ t.get('count',3) }}"></div>
        <div><label>window (s)</label><input type="number" min="1" name="an_{{ action }}_window" value="{{ t.get('window',20) }}"></div>
      </div>
    {% endfor %}
  </div>

  <div class="card">
    <h2>Anti-raid</h2>
    <p class="hint">Join-rate lockdown and an account-age gate. Held members get the quarantine role rather than a ban, since raids sweep up bystanders.</p>
    <div class="switch">
      <input type="checkbox" id="ar" name="antiraid_enabled" {% if dig('security.antiraid.enabled',True) %}checked{% endif %}>
      <label for="ar" style="margin:0">Enabled</label>
    </div>
    <div class="grid">
      <div><label>Join limit</label><input type="number" min="2" name="join_limit" value="{{ dig('security.antiraid.join_limit',6) }}"></div>
      <div><label>Join window (s)</label><input type="number" min="1" name="join_window" value="{{ dig('security.antiraid.join_window',10) }}"></div>
      <div><label>Min account age (h) &mdash; 0 disables</label><input type="number" min="0" name="min_account_age_hours" value="{{ dig('security.antiraid.min_account_age_hours',0) }}"></div>
      <div>
        <label>New account action</label>
        <select name="young_account_action">
          {% for opt in ['hold','kick','ban'] %}
            <option value="{{ opt }}" {% if dig('security.antiraid.young_account_action','hold')==opt %}selected{% endif %}>{{ opt }}</option>
          {% endfor %}
        </select>
      </div>
      <div><label>Lockdown length (min)</label><input type="number" min="1" name="lockdown_minutes" value="{{ dig('security.antiraid.lockdown_minutes',10) }}"></div>
    </div>
  </div>

  <div class="card">
    <h2>Status rotation</h2>
    <p class="hint">Placeholders: {members} {guild} {channels}. Streaming adds a Watch button to the bot profile.</p>
    <div class="switch">
      <input type="checkbox" id="pr" name="presence_enabled" {% if dig('presence.enabled',True) %}checked{% endif %}>
      <label for="pr" style="margin:0">Enabled</label>
    </div>
    <div class="grid" style="margin-bottom:12px">
      <div><label>Interval (s, min 15)</label><input type="number" min="15" name="presence_interval" value="{{ dig('presence.interval',45) }}"></div>
    </div>
    {% for s in dig('presence.statuses',[]) %}
      <div class="row" style="margin-bottom:8px">
        <select name="presence_type" style="max-width:170px">
          {% for opt in ['playing','watching','listening','competing','custom','streaming'] %}
            <option value="{{ opt }}" {% if s.type==opt %}selected{% endif %}>{{ opt }}</option>
          {% endfor %}
        </select>
        <input name="presence_text" value="{{ s.text }}">
      </div>
    {% endfor %}
    {% for i in range(2) %}
      <div class="row" style="margin-bottom:8px">
        <select name="presence_type" style="max-width:170px">
          {% for opt in ['playing','watching','listening','competing','custom','streaming'] %}
            <option value="{{ opt }}">{{ opt }}</option>
          {% endfor %}
        </select>
        <input name="presence_text" placeholder="add another…">
      </div>
    {% endfor %}
  </div>

  <div class="card">
    <h2>Global ban list</h2>
    <p class="hint">Read-only here. Add and remove with /gban in Discord so every server stays in sync.</p>
    <div class="stat" style="margin-bottom:14px">
      <div><span>{{ bans|length }}</span><small>banned</small></div>
    </div>
    {% if bans %}
    <table>
      <tr><th>User</th><th>ID</th><th>Reason</th><th>By</th></tr>
      {% for b in bans[:25] %}
        <tr>
          <td>{{ b.get('username','unknown') }}</td>
          <td><code>{{ b.get('user') }}</code></td>
          <td>{{ b.get('reason','')[:70] }}</td>
          <td>{{ b.get('by_name','') }}</td>
        </tr>
      {% endfor %}
    </table>
    {% else %}
      <div class="empty">Nobody is global banned.</div>
    {% endif %}
  </div>

  <div class="bar">
    <button type="submit">Save to config.json</button>
    {% if saved %}<span class="saved">&#10003; Saved. Restart the bot, or run /reload &lt;cog&gt;.</span>{% endif %}
    {% if not connected %}<span class="warn">Not connected to Discord &mdash; dropdowns unavailable, type IDs manually.</span>{% endif %}
  </div>
</form>
</main>
</body>
</html>
"""


if __name__ == "__main__":
    print("Blueprint config panel  ->  http://127.0.0.1:5000")
    print(f"editing {CONFIG_PATH}")
    app.run(host="127.0.0.1", port=5000, debug=False)
