# Slack Mimic

Mirror messages from a Slack workspace where you **cannot install apps**
(HeartStamp, "HS") into your **own** free Slack workspace, in near real-time.

Because HS has app installation disabled, the only way to read its messages is to
act as *you* — reusing your own logged-in browser session (an `xoxc` token plus
the `d` cookie). Posting into your own workspace uses a normal bot token.

> ⚠️ **Terms of Service.** Automating your personal Slack credentials violates
> Slack's Terms of Service and could get your HS account flagged or suspended.
> This tool only reads messages you can already see with your own account, but
> the risk is real and yours to accept. There is no supported/compliant way to
> do full-fidelity channel mirroring out of a workspace that blocks apps.

## How it works

```
[HS Source Reader] --events--> [Transformer] --actions--> [Target Poster]
        |                           |                           |
        +----------- [State Store (SQLite) + User Cache] -------+
```

- **Source Reader** authenticates with your `xoxc` token + `d` cookie and watches
  the allowlisted HS channels. Real-time via websocket where available, with a
  fast-polling fallback.
- **Transformer** resolves user ids to names/avatars, rewrites Slack markup, and
  maps threads.
- **Target Poster** re-posts into your workspace with a bot token, mimicking the
  original author's name and avatar, preserving threads, edits, and deletes.
- **State Store** (SQLite) tracks the last-seen message per channel, the
  source→target message map, and a user cache — so restarts resume cleanly with
  no duplicates.

## Setup

### 1. Install

```bash
uv sync --extra dev
```

### 2. Create the target bot (in YOUR workspace)

1. Create a Slack app at <https://api.slack.com/apps> for your own workspace.
2. Add these **Bot Token Scopes**: `chat:write`, `chat:write.customize`,
   `files:write`, `reactions:write`.
3. Install the app to your workspace and copy the **Bot User OAuth Token**
   (`xoxb-…`). This is `TARGET_BOT_TOKEN`.
4. Invite the bot to each target channel (`/invite @yourbot`).

### 3. Extract your HS session credentials

You need two values from a logged-in HeartStamp web session.

**`HS_XOXC_TOKEN`** — open HS in your browser (`https://app.slack.com`), open
DevTools → Console, and run:

```js
JSON.parse(localStorage.localConfig_v2).teams
```

Find the entry for the HeartStamp team and copy its `token` value — it starts
with `xoxc-`.

**`HS_D_COOKIE`** — DevTools → Application → Cookies → `https://app.slack.com` →
copy the value of the cookie named `d` (it starts with `xoxd-`). Use the raw
value; do not URL-decode it.

Put both in a `.env` file (copy `.env.example`):

```
HS_XOXC_TOKEN=xoxc-...
HS_D_COOKIE=xoxd-...
TARGET_BOT_TOKEN=xoxb-...
```

> These credentials rotate. If the daemon starts reporting auth failures,
> re-extract them.

### 4. Configure channel mappings

Copy `config.example.yaml` to `config.yaml` and list each source→target channel.
Channel ids (not names) are required. To find an HS channel id, open the channel
in the browser — the id is the `C…` segment in the URL.

```yaml
channels:
  - source: C0123ABCD   # HS #general
    target: C0456WXYZ   # your #hs-general
    label: general
```

### 5. Run

```bash
# Verify credentials first
uv run python scripts/check_auth.py

# Start the mirror
uv run slack-mimic
```

## Development

```bash
uv run pytest
```

## Reverse relay (reply back to HeartStamp, with approval)

Optionally, you can reply *into* HeartStamp from your own workspace, gated by a
per-message approval. When you (the owner) write a message in a mapped channel/DM,
the bot DMs you a card with a **Review & send** button; clicking it opens a modal
with your message in an **editable box** — **Send** posts it to the matching
HeartStamp channel/DM *as you* (via your `xoxc` token), **Cancel/Discard** drops it.

Anti-echo is built in: messages you approve into HS are not mirrored back into
your workspace, and the bot's own mirrored posts are never treated as your input.

### Enable it
1. On your existing target Slack app: turn on **Socket Mode**, create an
   **app-level token** (`xapp-…`, scope `connections:write`), enable **Event
   Subscriptions** (`message.channels`, `message.groups`, `message.im`,
   `message.mpim`) and **Interactivity**.
2. Add bot scopes (then **Reinstall**): `channels:history`, `groups:history`,
   `im:history`, `mpim:history`, `im:write`.
3. Set in `.env`: `TARGET_APP_TOKEN=xapp-…`
4. Set in `config.yaml`:
   ```yaml
   reverse_enabled: true
   owner_member_id: U0XXXXXXX   # your member id in the TARGET workspace
   ```
5. Restart `slack-mimic`. Only *your* messages in mapped channels are eligible.

## Limitations

- **Deletes** are reliably detected only over the websocket; under pure polling
  they may be missed.
- **Free target workspaces** retain ~90 days of history; posting is unaffected.
- Credentials can rotate and require re-extraction.
