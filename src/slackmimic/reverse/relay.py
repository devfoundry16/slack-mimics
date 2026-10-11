"""Socket Mode app for the reverse relay (vanta-core → HeartStamp, with approval).

One Socket Mode connection carries both detection (any member's `message`
events → a pending card in the owner's DM) and interaction (Review button →
editable modal → send/discard).
`slack_bolt` is imported lazily so the rest of the package doesn't depend on it.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from ..client_hs import AuthError, HeartStampClient
from ..config import Config
from ..sink.client_target import TargetClient
from ..state.store import PendingOutbound, StateStore
from . import cards
from .detector import is_eligible

log = logging.getLogger(__name__)


@dataclass
class SendResult:
    # HS ts of the relayed message; None when an upload's message wasn't found.
    hs_ts: Optional[str]
    # Names of files that couldn't be sent.
    failed: list[str] = field(default_factory=list)


def _event_files(event: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    """(name, url) of each downloadable file on a vanta-core message."""
    out = []
    for f in event.get("files") or []:
        if not isinstance(f, dict):
            continue
        url = f.get("url_private_download") or f.get("url_private") or ""
        if url:
            out.append((str(f.get("name") or f.get("title") or "file"), str(url)))
    return tuple(out)


async def send_pending(
    pending: PendingOutbound,
    text: str,
    *,
    store: StateStore,
    hs: HeartStampClient,
    target: TargetClient,
    hs_thread_ts: Optional[str],
) -> SendResult:
    """Post an approved message, with its files, to HeartStamp as the owner.

    Files are downloaded from vanta-core and uploaded to HS; text and files go
    out as one message. Files that fail are reported, not fatal. Raises
    ValueError when there is nothing left to send.
    """
    uploads: list[tuple[str, str]] = []
    failed: list[str] = []
    for name, url in pending.files:
        try:
            content = await target.download_file(url)
            uploads.append((await hs.reserve_upload(name, content), name))
        except AuthError:
            raise
        except Exception as exc:
            log.warning("reverse relay: attachment %s not sent: %s", name, exc)
            failed.append(name)

    source = pending.source_channel
    if uploads:
        # Before sharing: the forward mirror may see the message before its ts
        # is known, and recognises it by these ids instead.
        await store.record_reverse_files(source, [fid for fid, _ in uploads])
        await hs.share_uploads(source, uploads, text=text, thread_ts=hs_thread_ts)
        hs_ts = await hs.find_file_message(source, uploads[0][0], thread_ts=hs_thread_ts)
    elif text.strip():
        hs_ts = await hs.chat_post_message(source, text, thread_ts=hs_thread_ts)
    else:
        raise ValueError(f"nothing to send ({len(failed)} attachment(s) failed)")

    if hs_ts:
        # Anti-echo, and the mapping links the HS copy to the original
        # vanta-core message, so replies on either side thread under it.
        await store.record_reverse_post(source, hs_ts)
        await store.record_mapping(source, hs_ts, pending.target_channel, pending.target_ts)
        # The ts now marks the echo; file ids would also match later reshares.
        await store.forget_reverse_files(source, [fid for fid, _ in uploads])
    return SendResult(hs_ts=hs_ts, failed=failed)


async def resolve_hs_thread(store: StateStore, pending: PendingOutbound) -> Optional[str]:
    """HeartStamp thread ts for a vanta-core thread reply, or None to post top-level.

    The vanta-core thread root is found in the message map when it's a
    mirrored HS message or a message this relay already sent to HS.
    """
    if not pending.thread_ts:
        return None
    mapping = await store.get_source_ts(pending.target_channel, pending.thread_ts)
    if not mapping or mapping[0] != pending.source_channel:
        return None
    return mapping[1]


def build_relay_app(
    config: Config, store: StateStore, hs_client: HeartStampClient, target: TargetClient
):
    """Build the Bolt AsyncApp with all reverse-relay handlers wired up."""
    from slack_bolt.async_app import AsyncApp

    app = AsyncApp(token=config.secrets.target_bot_token)

    async def _update_card(client, pending: PendingOutbound, label: str, *,
                           sent: bool, text: Optional[str] = None,
                           note: str = "", error: str = "") -> None:
        if not pending.card_channel or not pending.card_ts:
            return
        await client.chat_update(
            channel=pending.card_channel,
            ts=pending.card_ts,
            blocks=cards.resolved_card_blocks(
                label, text if text is not None else pending.text,
                sent=sent, note=note, error=error,
            ),
            text="Reverse relay resolved",
        )

    @app.event("message")
    async def on_message(event: dict[str, Any], client, logger) -> None:
        channel = str(event.get("channel", ""))
        if not is_eligible(event):
            log.info(
                "reverse relay: ignored message in %s (subtype=%s bot=%s app=%s user=%s)",
                channel,
                event.get("subtype"),
                bool(event.get("bot_id")),
                bool(event.get("app_id")),
                bool(event.get("user")),
            )
            return
        source = config.source_for(channel)
        if not source:
            log.info("reverse relay: ignored message in unmapped channel %s", channel)
            return
        thread_ts = str(event.get("thread_ts") or "")
        if thread_ts == str(event["ts"]):
            thread_ts = ""  # a thread's own root isn't a reply
        pending = PendingOutbound(
            id=uuid.uuid4().hex,
            target_channel=str(event["channel"]),
            target_ts=str(event["ts"]),
            source_channel=source,
            text=str(event.get("text", "")),
            status="pending",
            card_channel="",
            card_ts="",
            created_at=time.time(),
            thread_ts=thread_ts,
            files=_event_files(event),
        )
        await store.create_pending(pending)
        hs_thread_ts = await resolve_hs_thread(store, pending)
        label = config.label_for_target(pending.target_channel)
        dm = await client.conversations_open(users=config.owner_member_id)
        card_channel = dm["channel"]["id"]
        res = await client.chat_postMessage(
            channel=card_channel,
            blocks=cards.pending_card_blocks(
                pending,
                label,
                author_id=str(event.get("user", "")),
                hs_thread_ts=hs_thread_ts or "",
            ),
            text=f"Message pending approval for #{label}",
        )
        await store.set_pending_card(pending.id, card_channel, str(res["ts"]))
        log.info("reverse relay: approval card posted for #%s (%s)", label, pending.target_ts)

    @app.action(cards.ACTION_REVIEW)
    async def on_review(ack, body, client) -> None:
        await ack()
        pending = await store.get_pending(body["actions"][0]["value"])
        if not pending or pending.status != "pending":
            return
        label = config.label_for_target(pending.target_channel)
        await client.views_open(
            trigger_id=body["trigger_id"],
            view=cards.review_modal_view(pending, label),
        )

    @app.action(cards.ACTION_DISCARD)
    async def on_discard(ack, body, client) -> None:
        await ack()
        pending = await store.get_pending(body["actions"][0]["value"])
        if not pending or pending.status != "pending":
            return
        await store.set_pending_status(pending.id, "rejected")
        label = config.label_for_target(pending.target_channel)
        await _update_card(client, pending, label, sent=False)

    @app.view(cards.MODAL_CALLBACK)
    async def on_submit(ack, body, view, client) -> None:
        await ack()
        pending = await store.get_pending(view["private_metadata"])
        if not pending or pending.status != "pending":
            return
        text = (
            view["state"]["values"][cards.MODAL_BLOCK][cards.MODAL_INPUT].get("value")
            or ""
        )
        label = config.label_for_target(pending.target_channel)
        hs_thread_ts = await resolve_hs_thread(store, pending)
        try:
            result = await send_pending(
                pending, text, store=store, hs=hs_client, target=target,
                hs_thread_ts=hs_thread_ts,
            )
        except Exception as exc:
            # Tell the owner instead of leaving the card looking pending.
            log.exception("reverse relay: sending %s to #%s failed", pending.id, label)
            await store.set_pending_status(pending.id, "failed")
            await _update_card(client, pending, label, sent=False, text=text, error=str(exc))
            return
        await store.set_pending_status(pending.id, "sent")
        note = ""
        if result.failed:
            note = "⚠️ Not attached: " + ", ".join(result.failed)
        await _update_card(client, pending, label, sent=True, text=text, note=note)

    return app


async def run_relay(
    config: Config, store: StateStore, hs_client: HeartStampClient, target: TargetClient
) -> None:
    """Run the Socket Mode connection until cancelled."""
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

    if not config.secrets.target_app_token:
        raise RuntimeError("reverse relay requires TARGET_APP_TOKEN (xapp-…)")
    if not config.owner_member_id:
        raise RuntimeError("reverse relay requires owner_member_id in config.yaml")

    app = build_relay_app(config, store, hs_client, target)
    handler = AsyncSocketModeHandler(app, config.secrets.target_app_token)
    log.info("reverse relay: connecting via Socket Mode")
    await handler.start_async()
