"""Socket Mode app for the reverse relay (vanta-core → HeartStamp, with approval).

One Socket Mode connection carries both detection (owner's `message` events →
pending card) and interaction (Review button → editable modal → send/discard).
`slack_bolt` is imported lazily so the rest of the package doesn't depend on it.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Optional

from ..client_hs import HeartStampClient
from ..config import Config
from ..state.store import PendingOutbound, StateStore
from . import cards
from .detector import is_eligible

log = logging.getLogger(__name__)


def build_relay_app(config: Config, store: StateStore, hs_client: HeartStampClient):
    """Build the Bolt AsyncApp with all reverse-relay handlers wired up."""
    from slack_bolt.async_app import AsyncApp

    app = AsyncApp(token=config.secrets.target_bot_token)

    async def _update_card(client, pending: PendingOutbound, label: str, *,
                           sent: bool, text: Optional[str] = None) -> None:
        if not pending.card_channel or not pending.card_ts:
            return
        await client.chat_update(
            channel=pending.card_channel,
            ts=pending.card_ts,
            blocks=cards.resolved_card_blocks(
                label, text if text is not None else pending.text, sent=sent
            ),
            text="Reverse relay resolved",
        )

    @app.event("message")
    async def on_message(event: dict[str, Any], client, logger) -> None:
        if not is_eligible(event, config.owner_member_id):
            return
        source = config.source_for(event["channel"])
        if not source:
            return  # message in a channel that isn't mapped for relay
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
        )
        await store.create_pending(pending)
        label = config.label_for_target(pending.target_channel)
        dm = await client.conversations_open(users=config.owner_member_id)
        card_channel = dm["channel"]["id"]
        res = await client.chat_postMessage(
            channel=card_channel,
            blocks=cards.pending_card_blocks(pending, label),
            text=f"Message pending approval for #{label}",
        )
        await store.set_pending_card(pending.id, card_channel, str(res["ts"]))

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
        # Post to HeartStamp as the owner, then record for anti-echo so the
        # forward mirror doesn't bounce it back into vanta-core.
        hs_ts = await hs_client.chat_post_message(pending.source_channel, text)
        await store.record_reverse_post(pending.source_channel, hs_ts)
        await store.set_pending_status(pending.id, "sent")
        label = config.label_for_target(pending.target_channel)
        await _update_card(client, pending, label, sent=True, text=text)

    return app


async def run_relay(
    config: Config, store: StateStore, hs_client: HeartStampClient
) -> None:
    """Run the Socket Mode connection until cancelled."""
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

    if not config.secrets.target_app_token:
        raise RuntimeError("reverse relay requires TARGET_APP_TOKEN (xapp-…)")
    if not config.owner_member_id:
        raise RuntimeError("reverse relay requires owner_member_id in config.yaml")

    app = build_relay_app(config, store, hs_client)
    handler = AsyncSocketModeHandler(app, config.secrets.target_app_token)
    log.info("reverse relay: connecting via Socket Mode")
    await handler.start_async()
