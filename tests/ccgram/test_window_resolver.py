"""Tests for window_resolver — ID format helpers and startup migration."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ccgram.window_resolver import (
    LiveWindow,
    is_foreign_window,
    is_window_id,
    resolve_stale_ids,
)


class TestIsWindowId:
    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            pytest.param("@0", True, id="at_zero"),
            pytest.param("@12", True, id="at_multi_digit"),
            pytest.param("@", False, id="at_only"),
            pytest.param("0", False, id="no_at"),
            pytest.param("", False, id="empty"),
            pytest.param("mywindow", False, id="name"),
            pytest.param("emdash-claude-main-abc:@0", False, id="foreign_qualified"),
        ],
    )
    def test_is_window_id(self, key: str, expected: bool) -> None:
        assert is_window_id(key) == expected


class TestIsForeignWindow:
    @pytest.mark.parametrize(
        ("window_id", "expected"),
        [
            pytest.param("emdash-claude-main-abc:@0", True, id="emdash_qualified"),
            pytest.param("some-session:@5", True, id="generic_qualified"),
            pytest.param("@0", False, id="local_id"),
            pytest.param("@12", False, id="local_id_multi"),
            pytest.param("mywindow", False, id="bare_name"),
            pytest.param("@0:extra", False, id="starts_with_at"),
        ],
    )
    def test_is_foreign_window(self, window_id: str, expected: bool) -> None:
        assert is_foreign_window(window_id) == expected


def _ws(name: str) -> SimpleNamespace:
    """Minimal WindowState stand-in with mutable window_name."""
    return SimpleNamespace(window_name=name)


class TestResolveStaleIds:
    def test_no_changes_when_ids_still_live(self) -> None:
        live = [LiveWindow("@0", "proj")]
        window_states = {"@0": _ws("proj")}
        thread_bindings: dict = {100: {42: "@0"}}
        offsets: dict = {100: {"@0": 10}}
        display_names = {"@0": "proj"}

        changed = resolve_stale_ids(
            live, window_states, thread_bindings, offsets, display_names
        )

        assert not changed
        assert "@0" in window_states
        assert thread_bindings[100][42] == "@0"

    def test_stale_id_remapped_via_display_name(self) -> None:
        # @0 is gone; tmux restarted and the same window is now @1.
        # window_states is remapped to @1. Thread binding lookup uses display_names
        # which has already had "@0" removed by _resolve_window_states, so the thread
        # binding stays as "@0" (dead window preserved for /restore).
        live = [LiveWindow("@1", "proj")]
        window_states = {"@0": _ws("proj")}
        thread_bindings: dict = {100: {42: "@0"}}
        offsets: dict = {}
        display_names = {"@0": "proj"}

        changed = resolve_stale_ids(
            live, window_states, thread_bindings, offsets, display_names
        )

        assert changed
        assert "@1" in window_states
        assert "@0" not in window_states
        assert display_names.get("@1") == "proj"
        assert "@0" not in display_names
        # Thread binding keeps stale @0 — _resolve_window_states removed it from
        # display_names before thread resolution runs, so the dead binding is preserved.
        assert thread_bindings[100][42] == "@0"

    def test_dead_window_preserved_without_live_match(self) -> None:
        # Stale ID with no live window of that name — keep for /restore
        live: list[LiveWindow] = []
        window_states = {"@0": _ws("dead-proj")}
        thread_bindings: dict = {100: {42: "@0"}}
        offsets: dict = {}
        display_names: dict = {}

        changed = resolve_stale_ids(
            live, window_states, thread_bindings, offsets, display_names
        )

        assert not changed
        assert "@0" in window_states
        assert thread_bindings[100][42] == "@0"

    def test_old_format_name_key_migrated_to_window_id(self) -> None:
        # Pre-migration state: window_states keyed by name instead of @id
        live = [LiveWindow("@3", "myproject")]
        window_states = {"myproject": _ws("myproject")}
        thread_bindings: dict = {100: {7: "myproject"}}
        offsets: dict = {}
        display_names: dict = {}

        changed = resolve_stale_ids(
            live, window_states, thread_bindings, offsets, display_names
        )

        assert changed
        assert "@3" in window_states
        assert "myproject" not in window_states
        assert thread_bindings[100][7] == "@3"
        assert display_names.get("@3") == "myproject"

    def test_old_format_name_key_dropped_when_no_live_match(self) -> None:
        live: list[LiveWindow] = []
        window_states = {"oldname": _ws("oldname")}
        thread_bindings: dict = {}
        offsets: dict = {}
        display_names: dict = {}

        changed = resolve_stale_ids(
            live, window_states, thread_bindings, offsets, display_names
        )

        assert changed
        assert "oldname" not in window_states

    def test_foreign_window_preserved_unchanged(self) -> None:
        live: list[LiveWindow] = []
        foreign_id = "emdash-claude-main-abc:@0"
        ws = _ws("emdash-proj")
        window_states = {foreign_id: ws}
        thread_bindings: dict = {100: {1: foreign_id}}
        offsets: dict = {100: {foreign_id: 5}}
        display_names: dict = {}

        changed = resolve_stale_ids(
            live, window_states, thread_bindings, offsets, display_names
        )

        assert not changed
        assert foreign_id in window_states
        assert thread_bindings[100][1] == foreign_id
        assert offsets[100][foreign_id] == 5

    def test_empty_user_bindings_pruned(self) -> None:
        # After migration drops the only binding for a user, that user is removed
        live: list[LiveWindow] = []
        window_states: dict = {}
        thread_bindings: dict = {100: {42: "oldname"}}
        offsets: dict = {}
        display_names: dict = {}

        changed = resolve_stale_ids(
            live, window_states, thread_bindings, offsets, display_names
        )

        assert changed
        assert 100 not in thread_bindings

    def test_offsets_dropped_when_display_name_already_remapped(self) -> None:
        # _resolve_window_states runs first and removes "@0" from display_names,
        # replacing it with "@2". When _resolve_offsets runs, it can't find a live
        # match for "@0" (display_names no longer has it) so the offset is dropped.
        # This is intentional — read offsets are best-effort, not critical for recovery.
        live = [LiveWindow("@2", "proj")]
        window_states = {"@0": _ws("proj")}
        thread_bindings: dict = {}
        offsets: dict = {100: {"@0": 99}}
        display_names = {"@0": "proj"}

        changed = resolve_stale_ids(
            live, window_states, thread_bindings, offsets, display_names
        )

        assert changed
        assert "@2" in window_states
        assert offsets[100] == {}

    def test_returns_false_with_empty_state(self) -> None:
        changed = resolve_stale_ids([], {}, {}, {}, {})
        assert not changed
