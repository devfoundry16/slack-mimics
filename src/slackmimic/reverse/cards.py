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


def pending_card_blocks(pending: PendingOutbound, hs_label: str) -> list[dict]:
    """Card shown to the owner for a message awaiting approval."""
    return [
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
                "element": {
                    "type": "plain_text_input",
                    "action_id": MODAL_INPUT,
                    "multiline": True,
                    "initial_value": pending.text,
                },
            },
        ],
    }


def resolved_card_blocks(hs_label: str, text: str, *, sent: bool) -> list[dict]:
    """Card contents after the owner sends or discards."""
    status = f"✅ Sent to *#{hs_label}*" if sent else f"❌ Discarded (*#{hs_label}*)"
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"{status}\n\n>{_preview(text)}"},
        }
    ]
