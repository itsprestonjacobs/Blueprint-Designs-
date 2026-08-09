# Blueprint Utilities

A Discord server-operations bot built on **discord.py 2.7** using **Components V2**
(`LayoutView` / `Container` / `Section` / `TextDisplay` / `MediaGallery`) for every
panel, log and confirmation the bot sends.

Covers tickets and orders, staff management, giveaways, applications, moderation,
reviews, a live queue, payouts and suggestions — 51 commands across 12 modules.

---

## Setup

**1. Install dependencies**

```bash
pip install -r requirements.txt
```

**2. Add your token and config**

```bash
cp .env.example .env               # then paste your bot token in
cp config.example.json config.json # then fill in with /setup auto
```

Both `.env` and `config.json` are gitignored — they hold your token and your
server's channel/role IDs, so they never get committed.

**3. Enable privileged intents**

In the [Discord Developer Portal](https://discord.com/developers/applications) →
your app → **Bot**, turn on both:

- **Server Members Intent** — needed for welcome messages, autoroles and activity checks
- **Message Content Intent** — needed for ticket transcripts

The bot will not start without these.

**4. Invite the bot**

It needs `bot` + `applications.commands` scopes, and these permissions:
Manage Channels, Manage Roles, Ban Members, Kick Members, Moderate Members,
Manage Messages, Read Message History, Send Messages, Attach Files.

Make sure the bot's own role sits **above** any role it needs to grant or anyone
it needs to moderate.

**5. Run**

```bash
python bot.py
```

**6. Configure it from Discord — `/setup auto`**

You don't need to edit `config.json` by hand. Run:

```
/setup auto
```

The bot scans your server and matches its categories, log channels and staff roles
to the config by name — ignoring emoji and decoration, so `🌐 Website Development
Orders` and `│ Support Tickets │` both match fine. It shows you everything it
found and **writes nothing until you press Apply**.

Anything it misses or gets wrong, fix individually:

| Command | Sets |
|---|---|
| `/setup auto` | Everything it can detect, with a confirm step |
| `/setup guild` | Binds the bot to this server (restart after) |
| `/setup category <type> <category>` | Where a ticket type opens |
| `/setup ping <type> <role>` | Who gets pinged for that ticket type |
| `/setup channel <key> <channel>` | A log or board channel |
| `/setup role <tier> <role>` | A staff permission tier |
| `/setup link <key> <url>` | A URL used by panel buttons |
| `/setup show <section>` | What's configured now, with ✅/🔴 per key |

`/setup auto` never overwrites a value you've already set, so it's safe to re-run
after adding new categories.

Everything is optional: any unset value simply disables the feature that needs it.
On boot the bot prints what's still missing, and `/config` shows the same in Discord.

---

## Configuration

Everything lives in `config.json`.

| Section | What it does |
|---|---|
| `guild_id` | Your server. Set this for instant command sync. |
| `accent_color` | Accent stripe on every container. Default Blueprint blue `0x3B82F6`. |
| `branding` | Bot name, footer text, default banner image URL. |
| `links` | Every URL used by panel buttons, in one place. Panels reference these as `"config:links.support"`. |
| `roles` | Role IDs per tier: `admin`, `hr`, `support`, `designer`, plus `verified` and `autoroles`. |
| `channels` | Where each kind of log or board is posted. |
| `tickets` | Per-user open limit, channel naming, and the 14 ticket categories. |
| `order_status` | Services shown on the status board and their OPENED/CLOSED state. |
| `applications` | Open positions, their questions and the role granted on accept. |
| `payouts.commission_rate` | Fraction of an order's price the designer keeps (`1.0` = all of it). |

### Permission tiers

Commands are gated by role ID, not Discord permissions. `admin` satisfies every
other tier, and anyone with the Administrator permission always passes — so you
can't lock yourself out before roles are configured.

### Ticket categories

Each entry under `tickets.categories` needs a `category_id` (the Discord category
the channel is created in), optional `ping_roles`, and up to **5** `questions`
asked in the intake modal before the ticket opens.

The 14 categories ship pre-named: Support, Applications, Discord, Livery, Uniform,
Graphics, Bot, ELS, Photography, Google Services, Hosting, Map Templates,
Videography and Website Development.

---

## Panels

Panels are **data files**, not code — see `panels/*.json`. Each defines a heading,
body, link buttons, and an optional dropdown whose options carry their own content.

Shipped: `dashboard`, `assistance`, `store`, `courses`, `designer_info`,
`support_info`, `hr_dashboard`.

Post one with `/panel send <name>`. After editing a file, `/panel reload` re-reads
it without restarting the bot.

Button URLs use `"config:links.<key>"` so every link lives in `config.json`. A
button whose URL isn't set yet is skipped rather than breaking the panel.

---

## Commands

**Tickets** — `/ticket panel` `/ticket add` `/ticket remove` `/ticket rename` `/ticket close`

**Orders** — `/log` `/pass` `/updatestatus` `/status post` `/queue post` `/queue refresh`

**Staff** — `/infraction` `/infractions` `/promotion` `/loa request` `/loa end` `/loa list` `/activitycheck` `/activityresults` `/qc`

**Moderation** — `/warn` `/timeout` `/untimeout` `/kick` `/ban` `/unban` `/purge` `/cases`

**Giveaways** — `/giveaway create` `/giveaway end` `/giveaway reroll` `/giveaway list`

**Applications** — `/applications panel` `/applications toggle` `/applications pending`

**Community** — `/review` `/reviews` `/suggest` `/suggestion` `/partnership` `/verifypanel`

**Stats** — `/stats` `/leaderboard` `/payout record` `/payout owed`

**Setup** — `/setup auto` `/setup guild` `/setup category` `/setup ping` `/setup channel` `/setup role` `/setup link` `/setup show`

**Admin** — `/ping` `/config` `/reload` `/panel send` `/panel list` `/panel reload`

---

## Development

**Validate every panel offline — no token needed:**

```bash
python scripts/preview.py          # check all views
python scripts/preview.py --dump   # also print the JSON payloads
```

This builds every Components V2 view the bot can send and asserts each stays
within Discord's limits (40 components, 4000 characters, 25 select options). Run
it after any panel change — it catches most layout errors before Discord does.

A cog opts into the harness by defining:

```python
PREVIEW_VIEWS = [("name", lambda: SomeView())]
```

**Hot-reload a cog without restarting:** `/reload <cog>`

---

## How it's put together

```
bot.py              entry point: intents, cog loading, command sync
core/
  config.py         loads config.json, reports unset IDs at boot
  store.py          atomic lock-guarded JSON persistence
  perms.py          role-tier checks
  ui.py             Components V2 builders + limit enforcement
  logs.py           posting to configured log channels
cogs/               one module per feature, all hot-reloadable
panels/             panel content as JSON
data/               runtime state (gitignored)
```

### Notes on the implementation

**Every panel goes through `core/ui.py`.** Branding stays consistent and the
component/character limits are enforced in one place instead of per-cog.

**All writes go through `core/store.py`.** Plain JSON loses data when two
coroutines write at once, so each file has its own `asyncio.Lock`, writes commit
atomically via `os.replace`, and read-modify-write happens under a single lock.
Verified against 200 concurrent writers with no lost updates and no duplicate
ticket numbers. If the load ever outgrows JSON, only this one file changes.

**Buttons survive restarts.** Per-ticket, per-giveaway and per-suggestion buttons
are `DynamicItem`s whose `custom_id` carries the record's ID, so the bot doesn't
need to register a persistent view per open ticket — it parses the ID back out of
the button that was clicked.

**Components V2 constraints worth knowing if you extend this:**

- A V2 message can't carry `content` or `embeds` — all text goes in `TextDisplay`.
  That's why role pings are sent as a separate plain message just before the panel.
- Editing a message into a V2 view requires passing `content=None, embeds=[], attachments=[]`.
- `Section` requires an `accessory` (a Button or Thumbnail).
- Modals are still classic — V2 has no modal layout, and modals cap at 5 inputs.
