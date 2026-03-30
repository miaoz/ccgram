"""Telegram notifications for inter-agent messaging.

Shows silent notifications in Telegram topics when agents send messages
to each other. Handles sent/delivered/reply/broadcast/shell-pending
notifications and loop detection alerts with inline keyboard controls.

Key functions:
  - notify_message_sent: compact line in sender's topic
  - notify_message_delivered: compact line in recipient's topic
  - notify_messages_delivered: grouped notification for multiple messages
  - notify_reply_received: reply notification in original sender's topic
  - notify_pending_shell: pending message display in shell topic
  - notify_broadcast_sent: single summary in sender's topic
  - notify_loop_detected: alert with [Pause] [Allow] keyboard
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from telegram import (
    Bot,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import ContextTypes

from ..thread_router import thread_router
from .callback_registry import register
from .message_sender import rate_limit_send_message

if TYPE_CHECKING:
    from ..mailbox import Message

logger = structlog.get_logger()

# Callback data prefixes for loop alert buttons
CB_MSG_LOOP_PAUSE = "ml:p:"
CB_MSG_LOOP_ALLOW = "ml:a:"

_WINDOW_KEY_PARTS = 2

_SUBJECT_MAX_LEN = 40
_BODY_PREVIEW_LEN = 100
_MAX_DISPLAYED_RECIPIENTS = 5

# Map short hash ��� (window_a, window_b) for loop alert callback data.
# Telegram limits callback_data to 64 bytes; qualified IDs can be long.
_loop_alert_pairs: dict[str, tuple[str, str]] = {}


def _extract_window_id(qualified_id: str) -> str:
    """Extract bare window ID from qualified ID (e.g. 'ccgram:@0' -> '@0')."""
    parts = qualified_id.rsplit(":", 1)
    if len(parts) < _WINDOW_KEY_PARTS:
        return qualified_id
    return parts[1]


def _resolve_topic(
    qualified_id: str,
) -> tuple[int, int, int, str] | None:
    """Find the Telegram topic for a qualified window ID.

    Returns (user_id, thread_id, chat_id, window_id) or None.
    """
    window_id = _extract_window_id(qualified_id)
    for user_id, thread_id, bound_wid in thread_router.iter_thread_bindings():
        if bound_wid == window_id:
            chat_id = thread_router.resolve_chat_id(user_id, thread_id)
            return user_id, thread_id, chat_id, window_id
    return None


def _display_name(qualified_id: str) -> str:
    """Get display name for a qualified window ID."""
    window_id = _extract_window_id(qualified_id)
    return thread_router.get_display_name(window_id)


def _format_subject(subject: str) -> str:
    """Truncate subject for inline display."""
    if not subject:
        return ""
    if len(subject) > _SUBJECT_MAX_LEN:
        return subject[: _SUBJECT_MAX_LEN - 3] + "..."
    return subject


async def notify_message_sent(
    bot: Bot,
    from_window: str,
    to_window: str,
    message: Message,
) -> None:
    """Send a compact notification in the sender's Telegram topic.

    Format: -> @5 (api-gateway) [request] API contract query
    """
    topic = _resolve_topic(from_window)
    if topic is None:
        return

    _, thread_id, chat_id, _ = topic
    to_name = _display_name(to_window)
    to_wid = _extract_window_id(to_window)
    subj = _format_subject(message.subject)
    subj_part = f" {subj}" if subj else ""

    text = f"\u2192 {to_wid} ({to_name}) [{message.type}]{subj_part}"

    await rate_limit_send_message(
        bot,
        chat_id,
        text,
        message_thread_id=thread_id,
        disable_notification=True,
    )


async def notify_message_delivered(
    bot: Bot,
    from_window: str,
    to_window: str,
    message: Message,
) -> None:
    """Send a compact notification in the recipient's Telegram topic.

    Format: <- @0 (payment-svc) [request] API contract query
    """
    topic = _resolve_topic(to_window)
    if topic is None:
        return

    _, thread_id, chat_id, _ = topic
    from_name = _display_name(from_window)
    from_wid = _extract_window_id(from_window)
    subj = _format_subject(message.subject)
    subj_part = f" {subj}" if subj else ""

    text = f"\u2190 {from_wid} ({from_name}) [{message.type}]{subj_part}"

    await rate_limit_send_message(
        bot,
        chat_id,
        text,
        message_thread_id=thread_id,
        disable_notification=True,
    )


async def notify_messages_delivered(
    bot: Bot,
    to_window: str,
    messages: list[Message],
) -> None:
    """Send a grouped notification for multiple delivered messages.

    Merges multiple messages into a single Telegram notification.
    """
    if not messages:
        return

    topic = _resolve_topic(to_window)
    if topic is None:
        return

    _, thread_id, chat_id, _ = topic

    if len(messages) == 1:
        msg = messages[0]
        from_name = _display_name(msg.from_id)
        from_wid = _extract_window_id(msg.from_id)
        subj = _format_subject(msg.subject)
        subj_part = f" {subj}" if subj else ""
        text = f"\u2190 {from_wid} ({from_name}) [{msg.type}]{subj_part}"
    else:
        lines = [f"\u2190 {len(messages)} messages delivered:"]
        for msg in messages:
            from_name = _display_name(msg.from_id)
            from_wid = _extract_window_id(msg.from_id)
            subj = _format_subject(msg.subject)
            subj_part = f" {subj}" if subj else ""
            lines.append(f"  {from_wid} ({from_name}) [{msg.type}]{subj_part}")
        text = "\n".join(lines)

    await rate_limit_send_message(
        bot,
        chat_id,
        text,
        message_thread_id=thread_id,
        disable_notification=True,
    )


async def notify_reply_received(
    bot: Bot,
    original_msg: Message,
    reply_msg: Message,
) -> None:
    """Notify the original sender's topic that a reply was received.

    Format: Reply received from @5 (api-gateway) for: API contract query
    """
    topic = _resolve_topic(original_msg.from_id)
    if topic is None:
        return

    _, thread_id, chat_id, _ = topic
    from_name = _display_name(reply_msg.from_id)
    from_wid = _extract_window_id(reply_msg.from_id)
    subj = _format_subject(original_msg.subject)
    subj_part = f" for: {subj}" if subj else ""

    text = f"\u2713 Reply received from {from_wid} ({from_name}){subj_part}"

    await rate_limit_send_message(
        bot,
        chat_id,
        text,
        message_thread_id=thread_id,
        disable_notification=True,
    )


async def notify_pending_shell(
    bot: Bot,
    window_id: str,
    message: Message,
) -> None:
    """Show a pending message in a shell topic (send_keys is skipped).

    Format: Pending message from @0 (payment-svc) [request]: ...body preview
    """
    topic = _resolve_topic(window_id)
    if topic is None:
        return

    _, thread_id, chat_id, _ = topic
    from_name = _display_name(message.from_id)
    from_wid = _extract_window_id(message.from_id)
    subj = _format_subject(message.subject)
    subj_part = f" {subj}:" if subj else ":"

    body_preview = message.body[:_BODY_PREVIEW_LEN]
    if len(message.body) > _BODY_PREVIEW_LEN:
        body_preview += "..."

    text = (
        f"\u2709 Pending from {from_wid} ({from_name})"
        f" [{message.type}]{subj_part} {body_preview}"
    )

    await rate_limit_send_message(
        bot,
        chat_id,
        text,
        message_thread_id=thread_id,
        disable_notification=True,
    )


async def notify_broadcast_sent(
    bot: Bot,
    from_window: str,
    recipients: list[str],
    message: Message,
) -> None:
    """Send a single summary in the sender's topic listing broadcast recipients.

    Format: Broadcast [notify] to 3 peers: @5, @8, @12
    """
    topic = _resolve_topic(from_window)
    if topic is None:
        return

    _, thread_id, chat_id, _ = topic
    subj = _format_subject(message.subject)
    subj_part = f" {subj}" if subj else ""

    recipient_names = [
        f"{_extract_window_id(r)} ({_display_name(r)})"
        for r in recipients[:_MAX_DISPLAYED_RECIPIENTS]
    ]
    overflow = len(recipients) - _MAX_DISPLAYED_RECIPIENTS
    extra = f" (+{overflow} more)" if overflow > 0 else ""
    peers = ", ".join(recipient_names) + extra

    text = (
        f"\u2192 Broadcast [{message.type}]{subj_part}"
        f" to {len(recipients)} peers: {peers}"
    )

    await rate_limit_send_message(
        bot,
        chat_id,
        text,
        message_thread_id=thread_id,
        disable_notification=True,
    )


async def notify_loop_detected(
    bot: Bot,
    window_a: str,
    window_b: str,
) -> None:
    """Alert that a messaging loop was detected, with control buttons.

    Posts in the topic of window_a with [Pause Messaging] [Allow 5 more].
    Uses a short hash in callback_data to stay within Telegram's 64-byte limit.
    """
    import hashlib

    topic = _resolve_topic(window_a)
    if topic is None:
        return

    _, thread_id, chat_id, _ = topic
    name_a = _display_name(window_a)
    name_b = _display_name(window_b)
    wid_a = _extract_window_id(window_a)
    wid_b = _extract_window_id(window_b)

    # Hash the pair to fit within 64-byte callback_data limit
    pair_full = f"{window_a}|{window_b}"
    pair_hash = hashlib.md5(pair_full.encode()).hexdigest()[:12]  # noqa: S324
    _loop_alert_pairs[pair_hash] = (window_a, window_b)

    text = (
        f"\u26a0 Messaging loop detected between"
        f" {wid_a} ({name_a}) and {wid_b} ({name_b})"
    )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Pause Messaging",
                    callback_data=f"{CB_MSG_LOOP_PAUSE}{pair_hash}",
                ),
                InlineKeyboardButton(
                    "Allow 5 more",
                    callback_data=f"{CB_MSG_LOOP_ALLOW}{pair_hash}",
                ),
            ]
        ]
    )

    await rate_limit_send_message(
        bot,
        chat_id,
        text,
        message_thread_id=thread_id,
        disable_notification=True,
        reply_markup=keyboard,
    )


# ── Callback handlers for loop alert buttons ─────────────────────────


@register(CB_MSG_LOOP_PAUSE, CB_MSG_LOOP_ALLOW)
async def _handle_loop_alert(
    update: Update, _context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle [Pause Messaging] / [Allow 5 more] button presses."""
    from .msg_broker import delivery_strategy

    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()

    data = query.data
    if data.startswith(CB_MSG_LOOP_PAUSE):
        pair_hash = data[len(CB_MSG_LOOP_PAUSE) :]
        pair = _loop_alert_pairs.get(pair_hash)
        if pair:
            delivery_strategy.pause_peer(pair[0], pair[1])
            delivery_strategy.pause_peer(pair[1], pair[0])
        await _safe_edit_text(query, "\u23f8 Messaging paused between these windows")
    elif data.startswith(CB_MSG_LOOP_ALLOW):
        pair_hash = data[len(CB_MSG_LOOP_ALLOW) :]
        pair = _loop_alert_pairs.get(pair_hash)
        if pair:
            delivery_strategy.allow_more(pair[0], pair[1])
        await _safe_edit_text(query, "\u25b6 Allowing 5 more exchanges")


async def _safe_edit_text(query: CallbackQuery, text: str) -> None:
    """Edit callback query message text, ignoring errors."""
    import contextlib

    from telegram.error import TelegramError

    with contextlib.suppress(TelegramError):
        await query.edit_message_text(text)
