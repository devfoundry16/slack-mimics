# Slack Mimic

Mirror messages from a Slack workspace where you **can't install apps** (call it
**HeartStamp**) into your **own** Slack workspace — in near real-time, with the
original author's name and avatar, threads, files, edits and reactions. Optionally
reply *back* into HeartStamp from your own workspace, gated by your approval.

This README is a **complete, from-scratch setup guide** — no prior experience
needed. Follow it top to bottom and copy-paste each command.

> ⚠️ **Please read: Terms of Service.** The only way to read a workspace that
> blocks apps is to reuse *your own* logged-in session (a token + cookie from your
> browser). Automating that violates Slack's Terms and could get your HeartStamp
> account flagged or suspended. It only reads messages you can already see, but the
> risk is real and it's your decision. Don't use this on an account you can't
> afford to lose.

---

## Table of contents
1. [How it works (in plain terms)](#1-how-it-works)
2. [What you'll need](#2-what-youll-need)
3. [Install](#3-install)
4. [Create the bot in your own workspace](#4-create-the-bot)
5. [Get your HeartStamp session credentials](#5-get-your-heartstamp-credentials)
6. [Put your secrets in `.env`](#6-put-your-secrets-in-env)
7. [Check that everything connects](#7-check-connections)
8. [See which channels you can copy](#8-discover-channels)
9. [Copy the channels into your workspace](#9-copy-channels)
10. [Copy your DMs (optional)](#10-copy-dms)
11. [Tidy up group-DM names (optional)](#11-tidy-group-dm-names)
12. [Add yourself to the new channels](#12-invite-yourself)
13. [Choose how much history to copy](#13-backfill)
14. [Start mirroring](#14-run)
15. [Reply back to HeartStamp with approval (optional)](#15-reverse-relay)
16. [Keep it running 24/7](#16-always-on)
17. [Troubleshooting](#17-troubleshooting)
18. [Command reference](#18-command-reference)

---

## 1. How it works

```
HeartStamp (you can't add apps)                Your own workspace
        │                                             ▲
        │  read as YOU (session token)                │ post as a bot
        └──────────────►  Slack Mimic  ───────────────┘
                          (this tool)
```

- **Reading from HeartStamp** uses *your* browser session (a token + cookie) —
  the only way in when apps are blocked.
- **Posting into your workspace** uses a normal **bot** you create (you're the
  admin there, so this is allowed). The bot can *mimic* each original author's
  name and picture.
- A small program runs continuously, copying new messages across within seconds.

You set it up once with a few commands. After that it just runs.

---

## 2. What you'll need

- A Mac or Linux computer (these instructions use the Terminal).
- Membership in the **HeartStamp** workspace (you can already read the channels
  you want to mirror), logged in via a browser.
- **Admin** of your **own** free Slack workspace (where messages get copied to).
- About 20 minutes.

---

## 3. Install

**Install `uv`** (a Python tool manager) if you don't have it — paste into Terminal:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Get this project and install it.** In Terminal, go to the project folder and run:

```bash
uv sync --extra dev
```

That downloads everything the tool needs. You only do this once.

> Every command below starts with `uv run` — that just means "run inside this
> project." Run them from the project folder.

---

## 4. Create the bot

This bot lives in **your own** workspace and posts the mirrored messages.

1. Go to **https://api.slack.com/apps** → **Create New App** → **From scratch**.
2. Name it (e.g. `Mirror`) and pick **your own** workspace.
3. Left menu → **OAuth & Permissions** → **Bot Token Scopes** → add all of these:
   - `chat:write`
   - `chat:write.customize`  ← lets it show each original author's name/photo
   - `files:write`
   - `reactions:write`
   - `channels:manage`, `groups:write`  ← create channels
   - `channels:read`, `groups:read`  ← see existing channels
   - `channels:join`  ← join channels it creates
4. Scroll up → **Install to Workspace** → **Allow**.
5. Copy the **Bot User OAuth Token** — it starts with `xoxb-`. You'll need it soon.

---

## 5. Get your HeartStamp credentials

You need two values from your logged-in HeartStamp browser tab. Open HeartStamp in
Chrome (`https://app.slack.com`), then open DevTools (**View → Developer →
Developer Tools**, or right-click → Inspect).

**Value 1 — the token (`xoxc-…`):** click the **Console** tab, paste this, press Enter:

```js
JSON.parse(localStorage.localConfig_v2).teams
```

Find the HeartStamp team in the result and copy its `token` value (starts with `xoxc-`).

**Value 2 — the cookie (`xoxd-…`):** click the **Application** tab → left side
**Cookies** → `https://app.slack.com` → click the row named **`d`** → copy its
**Value** (starts with `xoxd-`). Copy it exactly, don't decode it.

> These are the keys to your HeartStamp account — keep them private. They change
> occasionally; if the tool later says "authentication failed," just repeat this
> step to get fresh values.

---

## 6. Put your secrets in `.env`

Make your secrets file from the template:

```bash
cp .env.example .env
```

Open `.env` in any text editor and fill in the three values:

```
HS_XOXC_TOKEN=xoxc-...        # from step 5, value 1
HS_D_COOKIE=xoxd-...          # from step 5, value 2
TARGET_BOT_TOKEN=xoxb-...     # from step 4
```

(Leave `TARGET_APP_TOKEN` blank for now — it's only for the optional reply-back
feature in step 15.) This file stays on your computer and is never shared.

---

## 7. Check connections

Make sure both sides work before doing anything else:

```bash
uv run python scripts/check_auth.py --env .env
```

You should see two green ✓ lines — one for HeartStamp (your name) and one for the
bot. If either shows ✗, re-check that value in `.env`.

---

## 8. Discover channels

See the list of HeartStamp channels you're a member of (with their IDs):

```bash
uv run python scripts/probe_hs.py --env .env
```

To preview recent messages from one channel (to confirm reading works), add its ID:

```bash
uv run python scripts/probe_hs.py --env .env --channel C0123ABCD
```

---

## 9. Copy channels

This creates matching channels in **your** workspace and writes the mapping file
(`config.yaml`) automatically. **Preview first** (creates nothing):

```bash
uv run python scripts/provision.py --env .env --dry-run
```

If it looks right, do it for real:

```bash
uv run python scripts/provision.py --env .env
```

- It creates one channel per HeartStamp channel (same names), or **reuses** one
  that already exists, and the bot joins each so it can post.
- Private HeartStamp channels are recreated as private.
- Options: `--public-only` (skip private channels), `--prefix hs-` (name them
  `#hs-general` etc. to avoid clashes).

When it finishes you'll have a `config.yaml` listing every source→target channel.

---

## 10. Copy DMs (optional)

To also mirror your direct messages and group DMs into private channels:

```bash
# Preview (nothing created). Excludes your 1:1 DM with "keith" by default.
uv run python scripts/provision_dms.py --env .env --dry-run

# Do it, excluding specific people's 1:1 DMs by name:
uv run python scripts/provision_dms.py --env .env --exclude keith --exclude bob
```

- 1:1 DMs become private `#dm-name` channels; group DMs become private channels.
- `--exclude <name>` skips a **1:1** DM with someone (group DMs are always kept).
- Your existing channel list in `config.yaml` is preserved — DMs are added to it.

> DMs are your most private conversations. Mirroring them copies their contents
> into channels in your workspace — only do this if you're comfortable with that.

---

## 11. Tidy group-DM names (optional)

Group DMs get long auto-generated names. Shorten them:

```bash
uv run python scripts/rename_dms.py --env .env          # preview with --dry-run first
```

Turns `#mpdm-alice--bob--carol-1` into something like `#gdm-alice-bob-carol-xxxx`.

**Changed your mind about group DMs?** Remove them entirely (archives the channels
and drops them from `config.yaml`, keeping regular channels + 1:1 DMs):

```bash
uv run python scripts/remove_group_dms.py --env .env    # preview with --dry-run first
```

---

## 12. Invite yourself

The bot created the channels, so at first only the bot is in them. Add yourself.

First get **your own member ID** in your workspace: click your profile picture →
**Profile** → the **⋯ More** button → **Copy member ID** (a `U…` value). Then:

```bash
uv run python scripts/invite_me.py --user U0XXXXXXX --env .env
```

It adds you to every channel in `config.yaml` (skips ones you're already in, so
it's safe to re-run).

---

## 13. Backfill

By default the tool starts mirroring from **now** (no old messages). To also copy
recent history on the first run, set how many days in `config.yaml`:

```yaml
backfill_days: 7      # copy the last 7 days on first start; 0 = only new messages
```

**Skip history for specific noisy channels** (e.g. app DMs like Linear or a bot),
so they only mirror new messages. Get their source IDs from `config.yaml`, then:

```bash
uv run python scripts/set_cursor_now.py D0B1NT2FE1F D0AU28D5FAQ
```

(Run this while the tool is stopped. It marks those channels as "start from now.")

---

## 14. Run

Start mirroring:

```bash
uv run slack-mimic
```

Now post a test message in a HeartStamp channel you copied — it should appear in
the matching channel in your workspace within a few seconds, showing the original
author's name and photo.

- To stop: press **Ctrl-C**.
- To restart: run it again — it resumes where it left off (no duplicates).
- Add `-v` for detailed logs if you're troubleshooting (otherwise leave it off —
  the detailed logs are large).

**Test the bot alone** (before a full run), post one message to a channel:

```bash
uv run python scripts/send_test.py --channel C0XXXXXXX --text "hello" --username "Test"
```

---

## 15. Reverse relay (optional)

Reply *into* HeartStamp from your own workspace, with an approval step. When you
write in a mirrored channel, the bot DMs you a card with a **Review & send**
button; clicking it opens a box where you can **edit** the message, then **Send**
posts it to HeartStamp **as you** (or **Cancel** to discard).

**Turn it on:**
1. On your Slack app (from step 4): **Settings → Socket Mode → Enable**, create an
   **app-level token** (scope `connections:write`) and copy it (`xapp-…`).
2. **Event Subscriptions → Enable**, add bot events `message.channels`,
   `message.groups`, `message.im`, `message.mpim`.
3. **Interactivity & Shortcuts → Enable**.
4. **OAuth & Permissions → Bot Token Scopes**, add `channels:history`,
   `groups:history`, `im:history`, `mpim:history`, `im:write`.
5. **Install App → Reinstall to Workspace**.
6. In `.env`, set `TARGET_APP_TOKEN=xapp-…`.
7. In `config.yaml`, set:
   ```yaml
   reverse_enabled: true
   owner_member_id: U0XXXXXXX   # your member ID (from step 12)
   ```
8. Restart with `uv run slack-mimic`. Only *your* messages are eligible, and
   nothing reaches HeartStamp without your click.

---

## 16. Always-on

`uv run slack-mimic` runs only while that Terminal window is open. To keep it
running after you close the window, install it as a service.

### Linux (Ubuntu, systemd)

From the project folder, after steps 3–14 work:

```bash
chmod +x scripts/install_service.sh
scripts/install_service.sh
```

This writes `~/.config/systemd/user/slack-mimic.service` pointing at this folder,
starts it, and enables *linger* (asks for your password once) so it keeps running
when you log out and starts on boot. It restarts automatically if it crashes, but
**not** on an authentication failure — fix `.env`, then restart it.

- Status: `systemctl --user status slack-mimic`
- Watch its log: `journalctl --user -u slack-mimic -f`
- Restart (e.g. after editing `.env` or `config.yaml`): `systemctl --user restart slack-mimic`
- Stop: `systemctl --user stop slack-mimic` (add `disable` to stop starting on boot)
- Remove: `scripts/install_service.sh --uninstall`

If you move the project folder, run the install script again.

### macOS (launchd)

Install the included service (starts on login):

```bash
cp com.slackmimic.mirror.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.slackmimic.mirror.plist
```

- Check it's running: `launchctl list | grep slackmimic`
- Watch its log: `tail -f mirror.log`
- Stop it: `launchctl unload ~/Library/LaunchAgents/com.slackmimic.mirror.plist`

(The plist assumes the project lives at its current path; edit the paths inside if
you move it.)

---

## 17. Troubleshooting

- **"authentication failed" / stopped mirroring** → your HeartStamp token or cookie
  expired. Redo [step 5](#5-get-your-heartstamp-credentials) and update `.env`.
- **A message didn't appear** → make sure the bot is a member of that target
  channel (it is for channels it created; for others, `/invite @yourbot`).
- **`not_in_channel` when posting** → invite the bot to that channel.
- **Rate-limited / slow during setup** → normal; the tool waits and retries
  automatically. Provisioning many channels can take a few minutes.
- **Duplicates after enabling reverse relay** → shouldn't happen (built-in
  anti-echo); if it does, stop and re-run so cursors resync.
- **Gets killed / high memory** → run without `-v`, and prefer the systemd/launchd service
  or a machine with more free memory.

---

## 18. Command reference

| Command | What it does |
|---|---|
| `uv sync --extra dev` | Install the tool (once). |
| `scripts/check_auth.py` | Verify HeartStamp + bot credentials. |
| `scripts/check_hs_auth.py` | Verify only HeartStamp credentials. |
| `scripts/probe_hs.py [--channel C…]` | List your channels; sample messages. |
| `scripts/provision.py [--dry-run] [--public-only] [--prefix hs-]` | Create channels + write `config.yaml`. |
| `scripts/provision_dms.py [--dry-run] [--exclude name]` | Add DMs / group DMs to the mapping. |
| `scripts/rename_dms.py [--dry-run]` | Shorten group-DM channel names. |
| `scripts/remove_group_dms.py [--dry-run]` | Archive group DMs + drop from config. |
| `scripts/invite_me.py --user U… \| --email you@…` | Add yourself to all mapped channels. |
| `scripts/set_cursor_now.py D… [D… …]` | Skip backfill for specific channels. |
| `scripts/send_test.py --channel C… --text "…"` | Post one test message as the bot. |
| `slack-mimic [--backfill DAYS] [-v]` | Run the mirror (and reverse relay if enabled). |
| `scripts/install_service.sh [--uninstall]` | Install/remove the systemd user service (Linux). |

All commands are prefixed with `uv run` and most take `--env .env`. Add `--help`
to any script to see its options.

### `config.yaml` fields
```yaml
db_path: slackmimic.sqlite3      # where state (cursors, history map) is kept
poll_interval_seconds: 5         # how often to check when websocket is unavailable
use_websocket: true              # prefer real-time; falls back to polling
backfill_days: 7                 # history to copy on first run (0 = none)
reverse_enabled: false           # reply-back with approval (step 15)
owner_member_id: ""              # your member ID (for reverse relay)
channels:                        # written by the provision scripts
  - source: C0123ABCD            # HeartStamp channel/DM id
    target: C0XXXXXXX            # your workspace channel id
    label: general
```

---

## Development

```bash
uv run --extra dev pytest        # run the test suite
```

## Limitations

- **Deletes** are reliably mirrored only over the websocket connection; with
  polling they may be missed.
- **Free workspaces** keep ~90 days of history; posting is unaffected.
- Session credentials rotate and occasionally need re-extracting (step 5).
