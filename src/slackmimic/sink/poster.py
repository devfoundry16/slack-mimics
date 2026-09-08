"""Apply a :class:`TargetAction` to the target workspace.

The poster is the only unit that writes to the target. It resolves threading and
edit/delete targets through the :class:`StateStore` message map, so the whole
pipeline stays idempotent and resumable.
"""

from __future__ import annotations

import logging
from typing import Optional

from ..client_hs import HeartStampClient
from ..models import EventKind, TargetAction
from ..state.store import StateStore
from .client_target import TargetClient

log = logging.getLogger(__name__)


class Poster:
    def __init__(
        self,
        target: TargetClient,
        store: StateStore,
        hs_client: HeartStampClient,
    ) -> None:
        self._target = target
        self._store = store
        self._hs = hs_client

    async def apply(self, action: TargetAction) -> None:
        handler = {
            EventKind.CREATE: self._create,
            EventKind.EDIT: self._edit,
            EventKind.DELETE: self._delete,
            EventKind.REACTION_ADD: self._reaction_add,
            EventKind.REACTION_REMOVE: self._reaction_remove,
        }[action.kind]
        await handler(action)

    async def _resolve_thread_ts(self, action: TargetAction) -> Optional[str]:
        """Map the source thread root to the target thread root, if this is a reply."""
        if not action.source_thread_ts or action.source_thread_ts == action.source_ts:
            return None
        mapping = await self._store.get_target_ts(
            action.source_channel, action.source_thread_ts
        )
        return mapping[1] if mapping else None

    async def _create(self, action: TargetAction) -> None:
        # Skip if we've already mirrored this source message (idempotency).
        if await self._store.get_target_ts(action.source_channel, action.source_ts):
            return

        thread_ts = await self._resolve_thread_ts(action)
        target_ts = await self._target.post_message(
            action.channel,
            action.text or " ",
            username=action.username,
            icon_url=action.icon_url,
            thread_ts=thread_ts,
        )
        await self._store.record_mapping(
            action.source_channel, action.source_ts, action.channel, target_ts
        )
        await self._mirror_files(action, thread_ts=thread_ts or target_ts)

    async def _edit(self, action: TargetAction) -> None:
        mapping = await self._store.get_target_ts(action.source_channel, action.source_ts)
        if not mapping:
            # Never saw the original — treat the edit as a new message.
            await self._create(action)
            return
        target_channel, target_ts = mapping
        await self._target.update_message(target_channel, target_ts, action.text or " ")

    async def _delete(self, action: TargetAction) -> None:
        mapping = await self._store.get_target_ts(action.source_channel, action.source_ts)
        if not mapping:
            return
        target_channel, target_ts = mapping
        await self._target.delete_message(target_channel, target_ts)
        await self._store.delete_mapping(action.source_channel, action.source_ts)

    async def _reaction_add(self, action: TargetAction) -> None:
        mapping = await self._store.get_target_ts(action.source_channel, action.source_ts)
        if not mapping or not action.reaction:
            return
        target_channel, target_ts = mapping
        try:
            await self._target.add_reaction(target_channel, target_ts, action.reaction)
        except Exception as exc:  # emoji may not exist in target workspace
            log.warning("reaction add failed (%s): %s", action.reaction, exc)

    async def _reaction_remove(self, action: TargetAction) -> None:
        mapping = await self._store.get_target_ts(action.source_channel, action.source_ts)
        if not mapping or not action.reaction:
            return
        target_channel, target_ts = mapping
        try:
            await self._target.remove_reaction(target_channel, target_ts, action.reaction)
        except Exception as exc:
            log.warning("reaction remove failed (%s): %s", action.reaction, exc)

    async def _mirror_files(self, action: TargetAction, *, thread_ts: str) -> None:
        for f in action.files:
            try:
                content = await self._hs.download_file(f.url_private)
                await self._target.upload_file(
                    action.channel,
                    f.name,
                    content,
                    thread_ts=thread_ts,
                    title=f.name,
                )
            except Exception as exc:
                # Degrade gracefully: note the file rather than dropping silently.
                log.warning("file mirror failed (%s): %s", f.name, exc)
                await self._target.post_message(
                    action.channel,
                    f"_(attachment not mirrored: {f.name})_",
                    username=action.username,
                    icon_url=action.icon_url,
                    thread_ts=thread_ts,
                )
