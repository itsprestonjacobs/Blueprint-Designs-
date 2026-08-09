# Blueprint Utilities

Security bot for **Blueprint Designs**, built on discord.py 2.7.

Global bans that apply across every Blueprint server, anti-nuke, anti-raid, and
a localhost web panel for configuration.

> The earlier full-featured version — tickets, orders, panels, applications,
> staff tools, giveaways, moderation, reviews, payouts — is preserved at the
> **`v1-full`** tag: `git checkout v1-full`

---

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env                # paste your bot token
cp config.example.json config.json  # or configure via the panel below
python bot.py
```

Python 3.10+ (built on 3.12).

**Privileged intents** — in the [Developer Portal](https://discord.com/developers/applications)
→ Bot, enable **Server Members** and **Message Content**.

**Permissions the bot needs:** Ban Members, Kick Members, Manage Roles,
Manage Server, View Audit Log. Its role must sit **above** anyone it may have to
act against.

---

## Config panel

```bash
pip install -r panel/requirements.txt
python panel/app.py          # http://127.0.0.1:5000
```

Edits `config.json` through a web UI, with channel and role dropdowns pulled
live from your server so you never type an ID by hand. Binds to `127.0.0.1`
only — it can read your token and rewrite your config, so never expose it.

Changes take effect on restart, or `/reload <cog>`.

---

## Global bans

One ban applies everywhere the bot is. Adding a ban fans it out to every current
server, joining a new server replays the whole list into it, and anyone on the
list is banned the moment they try to join anywhere.

| Command | Does |
|---|---|
| `/gban add <user> <reason>` | Ban across every server |
| `/gban remove <user_id>` | Lift everywhere |
| `/gban check <user_id>` | Look someone up |
| `/gban list` | Everyone on the list |
| `/gban sync` | Re-apply the list to all servers |
| `/gban servers` | Which servers are protected, and whether the bot can ban there |

**Authority comes from one role in the home server** (`security.gban_role`),
checked against that server no matter where the command is run. A cross-server
power can't be gated per-server, or anyone holding a similarly-named role in a
satellite server could use it.

The bot refuses to global ban itself, the home server's owner, anyone holding
the global-ban role, and anyone whitelisted.

---

## Anti-nuke

Reacts to audit-log entries as they arrive and counts destructive actions per
person. A compromised admin can delete forty channels in ten seconds, so
reacting per event matters more than any periodic scan.

Watched: bans, kicks, channel create/delete, role create/delete, webhook
creation, privileged role grants, prunes, server-setting changes.

Each has its own `count in window` threshold. Cross one and the response fires:

- `quarantine` (default) — strip every removable role
- `ban` / `kick`
- `alert` — log only

Whitelisted users and roles are skipped. The server owner can't be removed, so
they trigger a loud alert instead.

`/antinuke status` · `/antinuke toggle` · `/antinuke incidents`

---

## Anti-raid

**Join rate** — too many joins inside a window puts the server into lockdown:
verification level goes to High and new arrivals get the quarantine role.
Auto-lifts after a set time, or `/antiraid release`.

**Account age** — accounts younger than a threshold can be held, kicked or
banned on sight.

Held members are quarantined rather than banned by default. Raids sweep up
bystanders, and a role is much easier to undo than a ban.

`/antiraid status` · `/antiraid lock` · `/antiraid release` · `/antiraid toggle`

---

## Other commands

`/ping` · `/config` · `/reload <cog>` · `/presence set|pause|resume|next|list`

The presence rotates through Playing / Watching / Listening / Competing, with
`{members}`, `{guild}` and `{channels}` as live placeholders. A **Streaming**
status makes Discord add a "Watch" button to the bot's profile.

---

## Layout

```
bot.py              entry point, cog loading, command sync
core/
  config.py         config loading, unset-ID report
  security.py       global-ban authority, whitelists, rate tracking
  store.py          atomic lock-guarded JSON persistence
  perms.py          role-tier checks
  ui.py             Components V2 builders + limit enforcement
  logs.py           posting to log channels
cogs/
  globalban.py      cross-server bans
  antinuke.py       audit-log watch and response
  antiraid.py       join-rate lockdown, account-age gate
  general.py        /ping /config /reload
  presence.py       rotating status
panel/app.py        localhost config UI
scripts/preview.py  offline view validation
```

Every write goes through `core/store.py`: one `asyncio.Lock` per file and
atomic `os.replace` commits, so concurrent bans can't lose each other.

Every message goes through `core/ui.py`, which enforces Discord's limits in one
place: 40 components, 4000 characters, 25 select options.

**Validate before deploying:** `python scripts/preview.py`
