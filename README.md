# Blueprint Utilities

A minimal Discord bot base for **Blueprint Designs**, built on discord.py 2.7
with Components V2 (`LayoutView` / `Container` / `TextDisplay` / `MediaGallery`).

This is a stripped-back starting point. The full-featured version — tickets,
orders, panels, applications, staff tools, giveaways, moderation, reviews and
payouts — is preserved at the **`v1-full`** tag:

```bash
git checkout v1-full
```

---

## Setup

**1. Install**

```bash
pip install -r requirements.txt
```

**2. Token and config**

```bash
cp .env.example .env               # paste your bot token
cp config.example.json config.json # add your guild and role IDs
```

Both are gitignored — they hold your token and server IDs.

**3. Privileged intents**

In the [Developer Portal](https://discord.com/developers/applications) → your app
→ Bot, enable **Server Members Intent** and **Message Content Intent**.

**4. Run**

```bash
python bot.py
```

Python 3.10+ (built on 3.12).

---

## What's here

| Command | Does |
|---|---|
| `/ping` | Gateway latency |
| `/config` | Which config values are still unset |
| `/reload <cog>` | Hot-reload a cog without restarting |
| `/presence set` | Pin the bot to one status |
| `/presence pause` / `resume` | Stop or restart the rotation |
| `/presence next` | Jump to the next status |
| `/presence list` | Show the rotation |

### Rotating status

Driven by `presence` in config.json. Supports Playing, Watching, Listening,
Competing, Custom and Streaming, with `{members}`, `{guild}` and `{channels}`
as live placeholders.

Note: a **Streaming** status makes Discord put a "Watch" button on the bot's
profile, and only renders purple with a real Twitch/YouTube URL.

---

## Layout

```
bot.py              entry point: intents, cog loading, command sync
core/
  config.py         loads config.json, reports unset IDs at boot
  perms.py          role-tier permission checks
  ui.py             Components V2 builders + limit enforcement
  logs.py           posting to configured log channels
cogs/
  general.py        /ping, /config, /reload
  presence.py       rotating status
scripts/
  preview.py        validate every view offline, no token needed
assets/             images uploaded with messages
```

### Building on it

Every message goes through `core/ui.py`, which enforces Discord's limits in one
place: **40 components**, **4000 characters**, **25 select options**.

```python
from core import ui

await ui.respond(interaction, ui.panel("Title", "Body"), ephemeral=True)
await ui.send_panel(channel, ui.panel("Title", "Body", banner="assets/x.png"))
await interaction.response.send_message(view=ui.ok("Done."))
```

Things Components V2 will not let you do:

- No `content` or `embeds` on a V2 message — all text lives in `TextDisplay`.
  Role pings must be sent as a separate plain message.
- Editing a message into a V2 view needs `content=None, embeds=[], attachments=[]`.
- `Section` requires an `accessory` (Button or Thumbnail).
- Modals are still classic, and cap at 5 inputs.

Local images are uploaded per message as `attachment://name`. Discord signs CDN
URLs and they expire within 24 hours, so linking one is not durable.

**Validate before you deploy:**

```bash
python scripts/preview.py
```

A cog opts into the harness with `PREVIEW_VIEWS = [("name", lambda: SomeView())]`.
