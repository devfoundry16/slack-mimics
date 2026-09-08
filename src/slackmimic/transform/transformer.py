"""Turn a :class:`SourceEvent` into a :class:`TargetAction`.

Responsibilities:

* Map the source channel to its target channel (drops events for channels not
  in the allowlist by returning ``None``).
* Resolve the author to a display name + avatar for author mimicking.
* Rewrite Slack markup that references the *source* workspace (user and channel
  mentions) into plain, readable text, since those ids mean nothing in the
  target workspace. Ordinary mrkdwn (bold, links, code) is left intact — the
  target also renders mrkdwn.
"""

from __future__ import annotations

import re
from typing import Optional

from ..config import Config
from ..models import EventKind, SourceEvent, TargetAction
from ..users import UserResolver

# <@U123> or <@U123|name>
_USER_MENTION = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]+)?>")
# <#C123|name> or <#C123>
_CHANNEL_MENTION = re.compile(r"<#[CG][A-Z0-9]+(?:\|([^>]*))?>")
# <!here>, <!channel>, <!everyone>, <!subteam^...|@name>
_SPECIAL_MENTION = re.compile(r"<!(here|channel|everyone)(?:\|[^>]+)?>")
_SUBTEAM_MENTION = re.compile(r"<!subteam\^[A-Z0-9]+(?:\|(@[^>]+))?>")


class Transformer:
    def __init__(self, config: Config, users: UserResolver) -> None:
        self._config = config
        self._users = users

    async def transform(self, event: SourceEvent) -> Optional[TargetAction]:
        target_channel = self._config.target_for(event.channel)
        if target_channel is None:
            return None

        author = await self._users.resolve(event.user) if event.user else None
        text = await self._rewrite(event.text)

        return TargetAction(
            kind=event.kind,
            channel=target_channel,
            source_channel=event.channel,
            source_ts=event.ts,
            text=text,
            username=author.name if author else None,
            icon_url=author.icon_url if author else None,
            source_thread_ts=event.thread_ts,
            files=event.files,
            reaction=event.reaction,
        )

    async def _rewrite(self, text: str) -> str:
        if not text:
            return text

        # Resolve user mentions to @DisplayName.
        async def replace_users(s: str) -> str:
            out: list[str] = []
            last = 0
            for m in _USER_MENTION.finditer(s):
                out.append(s[last : m.start()])
                user = await self._users.resolve(m.group(1))
                out.append(f"@{user.name}")
                last = m.end()
            out.append(s[last:])
            return "".join(out)

        text = await replace_users(text)
        text = _CHANNEL_MENTION.sub(lambda m: f"#{m.group(1)}" if m.group(1) else "#channel", text)
        text = _SPECIAL_MENTION.sub(lambda m: f"@{m.group(1)}", text)
        text = _SUBTEAM_MENTION.sub(lambda m: m.group(1) or "@group", text)
        return text


def is_actionable(event: SourceEvent) -> bool:
    """Filter out events we never mirror (e.g. our own noise, empty non-edits)."""
    if event.kind in (EventKind.DELETE, EventKind.REACTION_ADD, EventKind.REACTION_REMOVE):
        return True
    # For creates/edits, require some content (text or files).
    return bool(event.text) or bool(event.files)
