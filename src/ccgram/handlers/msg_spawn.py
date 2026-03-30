"""Agent spawn request handling with Telegram approval.

Manages spawn requests from agents: validation, rate limiting,
approval/denial flow, window creation, and topic auto-creation.
Uses callback_registry self-registration for inline keyboard dispatch.

Key components:
  - SpawnRequest: dataclass for pending spawn requests
  - create_spawn_request: validate and store a new request
  - handle_spawn_approval: create window + topic on approval
  - handle_spawn_denial: reject and clean up
  - Telegram callback handlers for [Approve] / [Deny] buttons
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..providers import resolve_launch_command
from ..session import session_manager
from ..tmux_manager import tmux_manager
from .callback_registry import register
from .message_sender import rate_limit_send_message

if TYPE_CHECKING:
    from telegram import Bot

logger = structlog.get_logger()

CB_SPAWN_APPROVE = "sp:ok:"
CB_SPAWN_DENY = "sp:no:"

_SPAWN_RATE_WINDOW_SECONDS = 3600  # 1 hour


@dataclass
class SpawnRequest:
    id: str
    requester_window: str
    provider: str
    cwd: str
    prompt: str
    context_file: str | None = None
    auto: bool = False
    created_at: float = field(default_factory=time.monotonic)

    def is_expired(self, timeout: int = 300) -> bool:
        return time.monotonic() - self.created_at > timeout


@dataclass
class SpawnResult:
    window_id: str
    window_name: str


_pending_requests: dict[str, SpawnRequest] = {}
_spawn_rate_tracker: dict[str, list[float]] = {}


def reset_spawn_state() -> None:
    _pending_requests.clear()
    _spawn_rate_tracker.clear()


def clear_spawn_state(window_id: str) -> None:
    to_remove = [
        rid
        for rid, req in _pending_requests.items()
        if req.requester_window == window_id
    ]
    for rid in to_remove:
        del _pending_requests[rid]
    _spawn_rate_tracker.pop(window_id, None)


def check_max_windows(
    window_states: dict,
    max_windows: int,
) -> bool:
    return len(window_states) < max_windows


def check_spawn_rate(window_id: str, max_rate: int) -> bool:
    cutoff = time.monotonic() - _SPAWN_RATE_WINDOW_SECONDS
    timestamps = _spawn_rate_tracker.get(window_id, [])
    recent = [t for t in timestamps if t > cutoff]
    _spawn_rate_tracker[window_id] = recent
    return len(recent) < max_rate


def record_spawn(window_id: str) -> None:
    _spawn_rate_tracker.setdefault(window_id, []).append(time.monotonic())


def create_spawn_request(
    requester_window: str,
    provider: str,
    cwd: str,
    prompt: str,
    context_file: str | None = None,
    auto: bool = False,
) -> SpawnRequest:
    if not Path(cwd).is_dir():
        raise ValueError(f"cwd does not exist: {cwd}")

    request_id = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    req = SpawnRequest(
        id=request_id,
        requester_window=requester_window,
        provider=provider,
        cwd=cwd,
        prompt=prompt,
        context_file=context_file,
        auto=auto,
    )
    _pending_requests[request_id] = req
    return req


async def handle_spawn_approval(
    request_id: str,
    bot: Bot,
) -> SpawnResult | None:
    req = _pending_requests.pop(request_id, None)
    if req is None:
        logger.warning(
            "Spawn request %s not found (expired or already handled)", request_id
        )
        return None

    launch_command = resolve_launch_command(req.provider)

    success, message, window_name, window_id = await tmux_manager.create_window(
        req.cwd,
        launch_command=launch_command,
    )
    if not success:
        logger.error("Spawn window creation failed: %s", message)
        return None

    window_state = session_manager.get_window_state(window_id)
    window_state.cwd = req.cwd
    session_manager.set_window_provider(window_id, req.provider)

    record_spawn(req.requester_window)

    await _create_topic_for_spawn(bot, window_id, window_name, req)

    if req.prompt:
        prompt_text = req.prompt
        if req.context_file:
            prompt_text = f"{req.prompt} (context: {req.context_file})"
        await tmux_manager.send_keys(window_id, prompt_text)

    logger.info(
        "Spawned window %s (%s) for %s (provider=%s)",
        window_id,
        window_name,
        req.requester_window,
        req.provider,
    )

    return SpawnResult(window_id=window_id, window_name=window_name)


def handle_spawn_denial(request_id: str) -> None:
    req = _pending_requests.pop(request_id, None)
    if req is not None:
        logger.info("Spawn request %s denied", request_id)


async def post_spawn_approval_keyboard(
    bot: Bot,
    requester_window: str,
    request: SpawnRequest,
) -> None:
    from .msg_telegram import _resolve_topic

    topic = _resolve_topic(requester_window)
    if topic is None:
        return

    _, thread_id, chat_id, _ = topic

    text = (
        f"\U0001f680 Spawn request: {request.provider} at {request.cwd}\n"
        f"Prompt: {request.prompt}"
    )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Approve",
                    callback_data=f"{CB_SPAWN_APPROVE}{request.id}",
                ),
                InlineKeyboardButton(
                    "Deny",
                    callback_data=f"{CB_SPAWN_DENY}{request.id}",
                ),
            ]
        ]
    )

    await rate_limit_send_message(
        bot,
        chat_id,
        text,
        message_thread_id=thread_id,
        reply_markup=keyboard,
    )


async def _create_topic_for_spawn(
    bot: Bot,
    window_id: str,
    window_name: str,
    req: SpawnRequest,
) -> None:
    from .topic_orchestration import _collect_target_chats, _create_topic_in_chat

    target_chats = _collect_target_chats(window_id)
    for chat_id in target_chats:
        await _create_topic_in_chat(bot, chat_id, window_id, window_name)

    topic_info = _resolve_requester_topic(req.requester_window)
    if topic_info:
        _, thread_id, chat_id, _ = topic_info
        text = f"\u2705 Spawned {window_name} ({window_id}) for: {req.prompt}"
        await rate_limit_send_message(
            bot,
            chat_id,
            text,
            message_thread_id=thread_id,
            disable_notification=True,
        )


def _resolve_requester_topic(
    qualified_id: str,
) -> tuple[int, int, int, str] | None:
    from .msg_telegram import _resolve_topic

    return _resolve_topic(qualified_id)


# ── Callback handlers for spawn approval buttons ─────────────────────────


@register(CB_SPAWN_APPROVE, CB_SPAWN_DENY)
async def _handle_spawn_callback(
    update: Update,
    _context: ContextTypes.DEFAULT_TYPE,
) -> None:
    import contextlib

    from telegram.error import TelegramError

    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()

    data = query.data

    if data.startswith(CB_SPAWN_APPROVE):
        request_id = data[len(CB_SPAWN_APPROVE) :]
        bot = update.get_bot()
        result = await handle_spawn_approval(request_id, bot)
        if result:
            text = f"\u2705 Spawned: {result.window_name} ({result.window_id})"
        else:
            text = "\u274c Spawn failed (request expired or window creation error)"
        with contextlib.suppress(TelegramError):
            await query.edit_message_text(text)

    elif data.startswith(CB_SPAWN_DENY):
        request_id = data[len(CB_SPAWN_DENY) :]
        handle_spawn_denial(request_id)
        with contextlib.suppress(TelegramError):
            await query.edit_message_text("\u274c Spawn request denied")
