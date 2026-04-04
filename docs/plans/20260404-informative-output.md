# Informative Telegram Output

## Overview

Make ccgram's Telegram output show what agents are actually doing, without being chatty. Currently, topics show generic status labels ("📝 writing…") during execution and bare "✓ Ready" on completion — the user can't tell what Claude is working on or what it accomplished without taking screenshots.

Two-tier approach:

- **Tier 1 (static, always active):** Show full status text with emoji prefix, enrich "Ready" with last status + task checklist, improve tool batch display
- **Tier 2 (LLM-enhanced, when configured):** On completion, async LLM call summarizes what was accomplished → edits the Ready message in-place

Graceful fallback: no LLM configured = Tier 1 only, which is still a major improvement over current behavior.

## Context (from discovery)

**Files involved:**

- `src/ccgram/terminal_parser.py` — `format_status_display()` compresses status to generic labels (line 586)
- `src/ccgram/handlers/polling_coordinator.py` — chooses `display_label` over `raw_text` for single-line status (line 468)
- `src/ccgram/handlers/hook_events.py` — Stop handler sends bare "✓ Ready" (line 128), SessionEnd is silent (line 286)
- `src/ccgram/handlers/message_queue.py` — batch tool results truncated to 80 chars (line 552)
- `src/ccgram/llm/httpx_completer.py` — existing LLM infra with `_BaseCompleter._post_and_extract()`
- `src/ccgram/llm/base.py` — `CommandGenerator` protocol (command-specific, needs generic extension)
- `src/ccgram/llm/__init__.py` — `get_completer()` factory
- `src/ccgram/claude_task_state.py` — already tracks task checklists per window

**Key patterns:**

- Status flow: `parse_status_block()` → `format_status_display()` → `display_label` → `enqueue_status_update()`
- LLM infra: `get_completer()` returns `OpenAICompatCompleter` or `AnthropicCompleter`, both use `_post_and_extract()`
- Hook event Stop data: `stop_reason`, `num_turns` available but unused
- `claude_task_state` already has `ClaudeTaskSnapshot` with `done_count`, `open_count`, items with subjects

## Development Approach

- **Testing approach**: Regular (code first, then tests)
- Complete each task fully before moving to the next
- Make small, focused changes
- **CRITICAL: every task MUST include new/updated tests**
- **CRITICAL: all tests must pass before starting next task**
- **CRITICAL: update this plan file when scope changes during implementation**
- Run `make fmt && make test && make lint` after each change

## Testing Strategy

- **Unit tests**: Required for every task — test new functions, formatting, edge cases
- **Async tests**: Task 6 spawns `asyncio.create_task()` from a hook handler — needs targeted async tests verifying graceful completion
- **Integration tests**: Not needed — changes are internal formatting/display, no new handler registration or dispatch routing

## Progress Tracking

- Mark completed items with `[x]` immediately when done
- Add newly discovered tasks with ➕ prefix
- Document issues/blockers with ⚠️ prefix

## What Goes Where

- **Implementation Steps** (`[ ]` checkboxes): code changes, tests, documentation
- **Post-Completion** (no checkboxes): manual Telegram testing, visual verification

## Implementation Steps

### Task 1: Add generic `complete()` to LLM completers

Extend the existing LLM infrastructure with a generic completion method so the summarizer (and future features) can reuse the same httpx/auth/API logic without duplicating it. Keep `generate_command()` untouched — the new method is additive only.

**Files:**

- Modify: `src/ccgram/llm/base.py`
- Modify: `src/ccgram/llm/httpx_completer.py`
- Modify: `src/ccgram/llm/__init__.py`
- Create: `tests/ccgram/llm/test_completer_generic.py`

- [ ] Add `TextCompleter` protocol to `base.py` with `async complete(system_prompt: str, user_message: str) -> str` method
- [ ] Add `complete()` method to `OpenAICompatCompleter` — calls `_post_and_extract()` directly with caller-supplied system prompt and user message (no new abstract methods, no changes to `_request()`)
- [ ] Add `complete()` method to `AnthropicCompleter` — same pattern, Anthropic-specific payload
- [ ] Do NOT refactor `generate_command()` — leave it untouched to avoid risk to the existing shell provider
- [ ] Add `get_text_completer() -> TextCompleter | None` factory to `__init__.py` (returns the same completer instance as `get_completer()`, just typed as `TextCompleter`)
- [ ] Write tests for `complete()` method on both completer classes (mock httpx responses)
- [ ] Run tests — must pass before task 2

### Task 2: Show full status text with emoji prefix

Replace the generic "📝 writing…" label with "📝 Writing tests for auth module" — the actual Claude status text with the matched emoji as prefix.

**Files:**

- Modify: `src/ccgram/terminal_parser.py`
- Modify: `src/ccgram/handlers/polling_coordinator.py`
- Modify: `tests/ccgram/test_terminal_parser.py`
- Modify: `tests/ccgram/handlers/test_polling_coordinator.py`

- [ ] Add `status_emoji_prefix(raw_status: str) -> str` function to `terminal_parser.py` — uses the same `_STATUS_KEYWORDS` table but returns only the emoji (e.g. `"📝"`, `"🧪"`, `"⚡"`), default `"⚙️"`. One table, no duplication.
- [ ] Refactor `format_status_display()` to call `status_emoji_prefix()` internally — keeps them in sync via the shared `_STATUS_KEYWORDS` table (DRY)
- [ ] Change `polling_coordinator.py` line 468: for single-line status, use `f"{status_emoji_prefix(headline)} {status.raw_text}"` instead of `status.display_label`
- [ ] Write tests for `status_emoji_prefix()` — keyword matching, default fallback, empty input
- [ ] Verify `format_status_display()` still produces same output after refactor
- [ ] Update polling coordinator tests for new status text format
- [ ] Run tests — must pass before task 3

### Task 3: Enrich "Ready" with last status + task checklist

When Claude finishes, show what it was last doing and the task checklist state instead of bare "✓ Ready". Store last-status state in `ClaudeTaskStateStore` (which already owns per-window state and handles cleanup via `clear_window()` + `@topic_state.register("window")`). Only modify `_handle_stop()` — NOT `_transition_to_idle()` — to avoid the race where polling overwrites the enriched Ready within 1 second.

**Files:**

- Modify: `src/ccgram/claude_task_state.py`
- Modify: `src/ccgram/handlers/hook_events.py`
- Modify: `src/ccgram/handlers/polling_coordinator.py`
- Modify: `tests/ccgram/test_claude_task_state.py`
- Modify: `tests/ccgram/handlers/test_hook_events.py`

- [ ] Add `_last_status: dict[str, str]` to `ClaudeTaskStateStore` with `set_last_status(window_id, text)`, `get_last_status(window_id) -> str | None` methods. Clear it in the existing `clear_window()` method (no new registry entry needed — already registered).
- [ ] Add `format_completion_text(window_id: str, num_turns: int = 0) -> str` method to `ClaudeTaskStateStore` — builds enriched Ready text:
  ```
  ✓ Ready
  ━━━━━━━━━━━━━━━━━━━━
  ✔ write unit tests
  ✔ run linter
  ✔ fix formatting
  3/3 tasks done · 12 turns
  ```
  Falls back to `"✓ Ready\nLast: <last_status>"` when no task checklist. Falls back to bare `"✓ Ready"` when neither available.
- [ ] Call `claude_task_state.set_last_status()` from `polling_coordinator.py` whenever a non-idle status is enqueued (inside `update_status_message` after `status_line` is computed)
- [ ] Modify `_handle_stop()` in `hook_events.py`: call `claude_task_state.format_completion_text(window_id, num_turns=event.data.get("num_turns", 0))` instead of bare `IDLE_STATUS_TEXT`
- [ ] Do NOT modify `_transition_to_idle()` — it still sends bare `IDLE_STATUS_TEXT`. The Stop hook fires first with the enriched text; the polling path may follow but the dedup check (`status_text == last_text`) will prevent overwrite if the text hasn't changed. If polling fires first (rare, no hook), bare Ready is acceptable.
- [ ] Write tests for `format_completion_text()` — with tasks, without tasks, with last status only, with num_turns, empty state
- [ ] Write tests for `set/get_last_status` lifecycle + verify `clear_window()` clears it
- [ ] Update hook_events tests for enriched Stop text
- [ ] Run tests — must pass before task 4

### Task 4: Improve tool batch result display

Show more useful tool result content in batched mode — increase length limit, show Bash exit codes, highlight errors.

**Files:**

- Modify: `src/ccgram/handlers/message_queue.py`
- Modify: `tests/ccgram/handlers/test_message_queue.py`

- [ ] Increase `tool_result_text` truncation from `first_line[:80]` to `first_line[:200]` — this is set inside `_process_batch_task()` where `entry.tool_result_text` is assigned (around line 552)
- [ ] In the batch entry rendering logic (inside `format_batch_message()` / the entry formatting path): if `tool_result_text` contains error-indicating patterns (`error`, `FAILED`, `exit code [1-9]`, `Exception`, `Traceback`), prefix with `❌` instead of `⎿`; if contains success patterns (`passed`, `ok`, `success`, `exit code 0`), prefix with `✅`
- [ ] For Bash tool results in batch: extract and show exit code if present in result text (pattern: `Exit code: N` or similar from Claude's Bash tool)
- [ ] Write tests for increased truncation length
- [ ] Write tests for error/success prefix detection in batch entries
- [ ] Write tests for Bash exit code extraction
- [ ] Run tests — must pass before task 5

### Task 5: Create LLM summarizer module

New module that takes recent transcript context and produces a 1-2 line completion summary via the configured LLM.

**Files:**

- Create: `src/ccgram/llm/summarizer.py`
- Create: `tests/ccgram/llm/test_summarizer.py`

- [ ] Create `summarizer.py` with:
  - `_SUMMARY_SYSTEM_PROMPT` — instructs the LLM to produce a single-line factual summary (~120 chars max) of what was accomplished, mentioning specific files/tests/commands, noting errors
  - `_build_summary_context(transcript_path: str, max_entries: int = 30) -> str` — reads last N JSONL entries from the transcript file via `await asyncio.to_thread()` (avoid blocking event loop on large files), extracts tool names + key arguments + result snippets (first 200 chars), assistant text (last 500 chars). Returns a compact context string. Returns empty string if file doesn't exist or is empty.
  - `async def summarize_completion(transcript_path: str) -> str | None` — returns None immediately if `transcript_path` is empty or `get_text_completer()` returns None (no LLM configured). Calls `completer.complete()` with the summary prompt and context. Returns the summary text. Catches `RuntimeError` and returns None on LLM failure (logged at warning level).
- [ ] Keep the context window small (target ~800 tokens input) to minimize latency and cost — only include tool names, file paths, exit codes, final assistant text
- [ ] Write tests for `_build_summary_context()` with mock JSONL files — verify it extracts the right fields and respects limits
- [ ] Write tests for `summarize_completion()` — mock completer, verify prompt structure, test None return on no LLM, test error handling
- [ ] Run tests — must pass before task 6

### Task 6: Integrate LLM summary into completion flow

Wire the summarizer into the Stop hook — fire async, edit the Ready message when the summary arrives. Get `transcript_path` from `session_manager`, not from the event payload (Stop events only contain `stop_reason` and `num_turns`).

**Files:**

- Modify: `src/ccgram/handlers/hook_events.py`
- Modify: `src/ccgram/handlers/message_queue.py`
- Modify: `tests/ccgram/handlers/test_hook_events.py`

- [ ] In `_handle_stop()`: resolve `transcript_path` via `session_manager.get_window_state(window_id)` (may be None for sessions started before bot run — handle gracefully). After enqueuing the enriched Ready status, spawn `asyncio.create_task(_enhance_with_llm_summary(...))` with window_id, user_id, thread_id, transcript_path
- [ ] Implement `_enhance_with_llm_summary()` in `hook_events.py`:
  1. Call `summarize_completion(transcript_path)` — returns None if path empty, no LLM, or failure
  2. If result is None → return (static Ready is already showing)
  3. Build enhanced text: replace `"✓ Ready"` header with `"✓ Done — <summary>"`, keep task checklist below
  4. Enqueue the enhanced text as a `status_update` task via `enqueue_status_update()` (use the queue, NOT a direct `bot.edit_message_text()` bypass — this ensures `_status_msg_info` stays in sync and the dedup check sees the new text)
- [ ] Handle race condition: if the user sends a new message before LLM responds (status message already converted to content), the enqueued status_update creates a new status bubble — acceptable, the summary is still delivered
- [ ] Write async test verifying the task is spawned, completes gracefully with None summarizer, and doesn't raise unhandled exceptions
- [ ] Write tests for `_enhance_with_llm_summary()` — mock summarizer, verify enqueue call, verify no-op on None, verify error tolerance
- [ ] Run tests — must pass before task 7

### Task 7: Verify acceptance criteria

- [ ] Verify status display shows full text: "📝 Writing tests for auth module" not "📝 writing…"
- [ ] Verify completion shows enriched Ready with task checklist and turn count
- [ ] Verify tool batch results show 200 chars with error/success indicators
- [ ] Verify LLM summary edits Ready message when configured
- [ ] Verify graceful fallback when LLM is not configured (Tier 1 still works)
- [ ] Run full test suite: `make check`

### Task 8: [Final] Update documentation

- [ ] Update CLAUDE.md with new output behavior description
- [ ] Add LLM summarizer to the module inventory in `.claude/rules/architecture.md`
- [ ] Move this plan to `docs/plans/completed/`

## Technical Details

### Status display change

**Before (single-line):**

```
polling_coordinator:468 → status.display_label → "📝 writing…"
```

**After (single-line):**

```
polling_coordinator:468 → f"{status_emoji_prefix(headline)} {status.raw_text}" → "📝 Writing tests for auth module"
```

Multi-line status (with checklist) is unchanged — already uses `raw_text`.

### Enriched Ready format

```
✓ Ready
━━━━━━━━━━━━━━━━━━━━
✔ write unit tests
✔ run linter
✔ fix formatting
3/3 tasks done · 12 turns
```

When LLM enhances (async edit ~1-2s later):

```
✓ Done — wrote 3 test files for auth module, all 47 tests passing
━━━━━━━━━━━━━━━━━━━━
✔ write unit tests
✔ run linter
✔ fix formatting
3/3 tasks done · 12 turns
```

Fallback when no tasks tracked:

```
✓ Ready
Last: Running make test · 12 turns
```

### LLM summary prompt (draft)

```
You are a development assistant summarizing what a coding agent accomplished.
Given the recent activity log, write a single-line summary (max 120 chars).
Be specific: mention file names, test counts, command outcomes.
Examples:
- "Fixed auth bug in login.py, all 23 tests pass"
- "Added 3 API endpoints in src/api/, updated OpenAPI spec"
- "Refactored database module — 2 tests failing (test_connection, test_pool)"
Return ONLY the summary line, no quotes or formatting.
```

### Batch entry error detection

```python
_ERROR_INDICATORS = re.compile(r"\b(error|FAILED|fail|Exception|Traceback)\b", re.IGNORECASE)
_SUCCESS_INDICATORS = re.compile(r"\b(passed|success|ok|exit code 0)\b", re.IGNORECASE)
```

### Data flow for LLM summary

```
Stop hook fires
  → session_manager.get_window_state(window_id).transcript_path
  → claude_task_state.format_completion_text() → enriched "Ready" text
  → enqueue_status_update() → user sees static Ready immediately
  → asyncio.create_task(_enhance_with_llm_summary())
      → summarize_completion(transcript_path)
          → get_text_completer() → completer.complete(prompt, context)
      → enqueue_status_update(enhanced_text) → user sees "Done — <summary>" ~1-2s later
```

Note: using the queue (not direct edit) keeps `_status_msg_info` in sync with the actual message text, preventing the next poll cycle from overwriting the LLM summary due to stale dedup state.

## Post-Completion

**Manual verification:**

- Start a Claude Code session via Telegram, run a multi-step task
- Verify status text shows full context during execution
- Verify completion shows task checklist + turn count
- Verify LLM summary appears after ~1-2s (when configured)
- Verify no LLM = still improved static output
- Verify tool batches show longer results with error/success indicators
- Check that message rate limiting still holds (no Telegram flood)
- Visual check: messages are readable, not too long, not cluttered
