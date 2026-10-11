"""Block Kit builders for the reverse-relay approval UI.

Pure functions (no Slack calls) so they can be unit-tested. Two surfaces:
a **pending card** (posted to the owner with Review/Discard buttons) and a
**review modal** (an editable text box + Submit/Cancel).
"""

from __future__ import annotations

from ..state.store import PendingOutbound

# Identifiers referenced by the Bolt handlers.
ACTION_REVIEW = "reverse_review"
ACTION_DISCARD = "reverse_discard"
MODAL_CALLBACK = "reverse_send"
MODAL_BLOCK = "reverse_msg_block"
MODAL_INPUT = "reverse_msg_input"


def _preview(text: str, limit: int = 300) -> str:
    text = text or "_(empty)_"
    return text if len(text) <= limit else text[:limit] + "…"


def pending_card_blocks(
    pending: PendingOutbound,
    hs_label: str,
    author_id: str = "",
    hs_thread_ts: str = "",
) -> list[dict]:
    """Card shown to the owner for a message awaiting approval.

    ``author_id`` names the vanta-core member who wrote it — the owner approves
    everyone's messages, so the card has to say whose words these are. Omitted
    when unknown; the relayed text itself is never altered.

    For a thread reply (``pending.thread_ts``), ``hs_thread_ts`` is the matching
    HeartStamp thread; when it's empty the card warns that the reply will land
    in the channel instead.

    Files on the message (``pending.files``) are sent with it; the card says
    how many.
    """
    notes: list[dict] = []
    if author_id:
        notes.append({"type": "mrkdwn", "text": f"From <@{author_id}>"})
    if pending.thread_ts:
        notes.append(
            {
                "type": "mrkdwn",
                "text": "↳ Reply in thread"
                if hs_thread_ts
                else "↳ Thread reply — its parent isn't in HeartStamp, "
                "so this will post to the channel",
            }
        )
    if pending.files:
        n = len(pending.files)
        notes.append(
            {"type": "mrkdwn", "text": f"📎 {n} {'attachment' if n == 1 else 'attachments'} included"}
        )
    context: list[dict] = [{"type": "context", "elements": notes}] if notes else []
    return context + [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Send to HeartStamp?* → *#{hs_label}*\n\n"
                    f">{_preview(pending.text)}"
                ),
            },
        },
        {
            "type": "actions",
            "block_id": f"reverse_actions_{pending.id}",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Review & send"},
                    "action_id": ACTION_REVIEW,
                    "value": pending.id,
                },
                {
                    "type": "button",
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "Discard"},
                    "action_id": ACTION_DISCARD,
                    "value": pending.id,
                },
            ],
        },
    ]


def review_modal_view(pending: PendingOutbound, hs_label: str) -> dict:
    """Modal with an editable message box + Submit (send) / Cancel."""
    element: dict = {"type": "plain_text_input", "action_id": MODAL_INPUT, "multiline": True}
    if pending.text:
        element["initial_value"] = pending.text  # Slack rejects an empty one
    return {
        "type": "modal",
        "callback_id": MODAL_CALLBACK,
        "private_metadata": pending.id,
        "title": {"type": "plain_text", "text": "Send to HeartStamp"},
        "submit": {"type": "plain_text", "text": "Send"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"Posting as you to *#{hs_label}*"}
                ],
            },
            {
                "type": "input",
                "block_id": MODAL_BLOCK,
                "label": {"type": "plain_text", "text": "Message"},
                # A message that is only files can be sent without text.
                "optional": bool(pending.files),
                "element": element,
            },
        ],
    }


def resolved_card_blocks(
    hs_label: str, text: str, *, sent: bool, note: str = "", error: str = ""
) -> list[dict]:
    """Card contents after the owner sends or discards, or after sending failed.

    ``note`` adds a line (e.g. attachments that couldn't be sent); ``error``
    marks a send that failed.
    """
    if error:
        status = f"⚠️ Not sent to *#{hs_label}*: {error}"
    elif sent:
        status = f"✅ Sent to *#{hs_label}*"
    else:
        status = f"❌ Discarded (*#{hs_label}*)"
    blocks: list[dict] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"{status}\n\n>{_preview(text)}"},
        }
    ]
    if note:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": note}]})
    return blocks
