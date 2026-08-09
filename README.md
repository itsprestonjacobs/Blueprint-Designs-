# Sail's Customs

Discord bot for **Sail's Customs** — security, tickets, applications and staff
management, built on discord.py 2.7 with Components V2 throughout.

Entry point is `main.py`.

---

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env                # paste your bot token
cp config.example.json config.json  # then fill in IDs, or use the panel below
python main.py
```

Python 3.10+ (built and tested on 3.12).

**Privileged intents** — in the [Developer Portal](https://discord.com/developers/applications)
→ your app → Bot, enable **Server Members** and **Message Content**. The bot
won't start without them.

**Permissions:** Ban Members, Kick Members, Moderate Members, Manage Roles,
Manage Nicknames, Manage Channels, Manage Server, View Audit Log, Send Messages,
Attach Files, Read Message History.

The bot's role must sit **above** anyone it may have to act on. Discord never
lets a bot act on the server owner, whatever permissions it holds.

### Hosting

Pterodactyl-style panels: set the startup command to

```
python -u main.py
```

`-u` disables output buffering, otherwise the console stays empty. Install
dependencies from the panel console with `pip install -r requirements.txt`.

Keep `data/` between restarts — it holds tickets, bans, applications and cases.

---

## Config panel

```bash
pip install -r panel/requirements.txt
python panel/app.py          # http://127.0.0.1:5000
```

Edits `config.json` through a web UI with channel and role dropdowns pulled
live from Discord, so you never type an ID by hand. Binds to `127.0.0.1` only —
it reads your token and rewrites your config, so never expose it.

Changes apply on restart, or `/reload <cog>`.

---

## Security

### Global bans

One ban applies to every server the bot is in. Adding fans out to all of them,
joining a new server replays the list into it, and anyone listed is banned the
moment they try to join anywhere.

| Command | Does |
|---|---|
| `/gban add` `/gban remove` | Ban or lift everywhere |
| `/gban check` `/gban list` | Look someone up, or the whole list |
| `/gban sync` | Push only the bans a server is missing |
| `/gban audit` | Per-server drift report |
| `/gban import` | Pull a server's existing bans into the list |
| `/gban servers` | Which servers are protected, and where the bot can't ban |

**Authority is one role in the home server** (`security.gban_role`), checked
against that server wherever the command runs. A cross-server power can't be
gated per-server, or anyone holding a similarly-named role elsewhere could use
it.

Refuses to ban itself, the owner, gban-role holders, and anyone whitelisted.

### Anti-nuke

Reacts to audit-log entries as they arrive, counting destructive actions per
person. A compromised admin can delete forty channels in ten seconds, so
per-event beats any periodic scan.

Watches bans, kicks, channel and role create/delete, webhooks, privileged role
grants, prunes and server-setting changes — each with its own `count in window`
threshold. Response is `quarantine` (default), `ban`, `kick` or `alert`.

`/antinuke status` · `toggle` · `incidents`

### Anti-raid

Join-rate lockdown (raises verification, quarantines arrivals, auto-lifts) and
an account-age gate. Held rather than banned by default — raids sweep up
bystanders and a role is far easier to undo.

`/antiraid status` · `lock` · `release` · `toggle`

### Moderation

`/warn` `/timeout` `/untimeout` `/kick` `/ban` `/unban` `/purge` `/cases`

Numbered case log, DMs on action, and one shared guard: no acting on yourself,
the bot, the owner, or anyone at or above your role height.

`/ban` is this server only — use `/gban add` for everywhere.

### Lookup

`/lookup` pulls everything the bot knows about someone into one reply: account
age, join date, roles, global ban, infractions, cases, leave and tickets.
Flags young accounts and colours the result by risk.

---

## Tickets

Two panels, each a dropdown of its own categories:

```
/ticket panel                 orders   — 12 services
/ticket panel which:support   support  — help and prize claims
```

Picking one opens a channel in the matching category, pings the configured
roles, and posts a panel with Claim and Close. Closing saves a transcript to
the log channel first.

`/ticket add` `/ticket remove` `/ticket close` `/ticket unblock`

**Close guard** — closing several tickets inside a short window blocks the
closer and alerts the raid channel with Restore / Continue Blocking. Reversible
on purpose: a legitimate sweep of spam tickets looks identical to abuse from a
counter's point of view. `/ticket testguard` fires it against yourself to test.

---

## Applications

`/apply panel` posts an Apply button. The rest happens in DMs: pick categories,
write a response, attach past work, then it posts to the review channel with
Accept and Deny. Deciding asks for a reason, recorded on the post and DM'd.

Portfolio images are re-uploaded to the review channel rather than linked —
Discord signs CDN URLs and they expire within a day.

`/apply toggle` `/apply pending`

---

## Staff

**Leave** — `/loa request` posts to the LOA channel with Approve and Deny.
Approved leave prefixes the member's nickname with `LOA | `, keeping the rest,
and restores it exactly when the leave ends or is ended early. An hourly sweep
expires leave automatically. `/loa end` `/loa list`

**Infractions** — `/infraction issue` with a severity ladder from Notice to
Termination. Numbered, logged, DM'd, and kept as history so escalation is based
on the record. `/infraction history` `view` `void`

Voiding marks an entry void and keeps the trail. A disciplinary history that
can be quietly erased isn't a record.

---

## Other

`/help` `/ping` `/config` `/reload <cog>` ·
`/presence set|pause|resume|next|list`

The presence rotates through Playing / Watching / Listening / Competing with
`{members}`, `{guild}` and `{channels}` as live placeholders. A **Streaming**
status makes Discord add a "Watch" button to the bot's profile.

---

## Layout

```
main.py             entry point, cog loading, command sync
core/
  config.py         config loading, unset-ID report
  security.py       global-ban authority, whitelists, rate tracking
  store.py          atomic lock-guarded JSON persistence
  perms.py          role-tier checks
  ui.py             Components V2 builders + limit enforcement
  logs.py           posting to log channels
cogs/               one module per feature, all hot-reloadable
panel/app.py        localhost config UI
scripts/preview.py  offline view validation
data/               runtime state — keep this between deploys
```

Every write goes through `core/store.py`: one `asyncio.Lock` per file, atomic
`os.replace` commits, and a `.bak` kept before each overwrite that it recovers
from if the live file is ever unreadable.

Every message goes through `core/ui.py`, which enforces Discord's limits in one
place: 40 components, 4000 characters, 25 select options.

### Components V2 notes

- A V2 message can't carry `content` or `embeds` — all text lives in
  `TextDisplay`, so role pings go in a separate plain message.
- Editing a message into a V2 view needs `content=None, embeds=[], attachments=[]`.
- `attachment://` only resolves for files uploaded in the *same* request. Edits
  must reference the attachments' real CDN URLs instead.
- Modals are still classic and cap at 5 inputs.

**Validate before deploying:** `python scripts/preview.py` — builds every view
offline and checks it against Discord's limits. No token needed.

> The earlier order-management version — orders, pricing, panels, giveaways,
> reviews, payouts — is preserved at the **`v1-full`** tag.
