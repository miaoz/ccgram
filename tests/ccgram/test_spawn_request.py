"""Tests for spawn_request — SpawnRequest dataclass, CRUD, and rate limiting."""

from __future__ import annotations

import json
import time

import pytest

from ccgram.spawn_request import (
    SpawnRequest,
    check_max_windows,
    check_spawn_rate,
    create_spawn_request,
    get_pending,
    iter_pending,
    pop_pending,
    record_spawn,
    register_pending,
    reset_spawn_state,
    scan_spawn_requests,
    spawns_dir,
)


@pytest.fixture(autouse=True)
def _clean_state(tmp_path, monkeypatch):
    reset_spawn_state()
    monkeypatch.setattr("ccgram.spawn_request.ccgram_dir", lambda: tmp_path)
    yield
    reset_spawn_state()


class TestSpawnRequestDataclass:
    def test_round_trip_full(self) -> None:
        req = SpawnRequest(
            id="123-abc",
            requester_window="ccgram:@0",
            provider="claude",
            cwd="/tmp/proj",
            prompt="fix tests",
            context_file="/tmp/ctx.md",
            auto=True,
            created_at=1700000000.0,
        )
        loaded = SpawnRequest.from_dict(req.to_dict())
        assert loaded == req

    def test_from_dict_defaults(self) -> None:
        req = SpawnRequest.from_dict({"id": "x", "requester_window": "@0", "cwd": "/p"})
        assert req.provider == "claude"
        assert req.prompt == ""
        assert req.context_file is None
        assert req.auto is False
        assert req.created_at == 0.0

    def test_is_expired_fresh(self) -> None:
        req = SpawnRequest(
            id="x", requester_window="@0", provider="claude", cwd="/p", prompt=""
        )
        assert req.is_expired(timeout=300) is False

    def test_is_expired_old(self) -> None:
        req = SpawnRequest(
            id="x",
            requester_window="@0",
            provider="claude",
            cwd="/p",
            prompt="",
            created_at=time.time() - 400,
        )
        assert req.is_expired(timeout=300) is True


class TestPendingRegistry:
    def test_register_and_get(self) -> None:
        req = SpawnRequest(
            id="r1", requester_window="@0", provider="claude", cwd="/p", prompt=""
        )
        register_pending(req)
        assert get_pending("r1") is req

    def test_get_missing_returns_none(self) -> None:
        assert get_pending("nope") is None

    def test_pop_removes(self) -> None:
        req = SpawnRequest(
            id="r2", requester_window="@0", provider="claude", cwd="/p", prompt=""
        )
        register_pending(req)
        popped = pop_pending("r2")
        assert popped is req
        assert get_pending("r2") is None

    def test_pop_missing_returns_none(self) -> None:
        assert pop_pending("nope") is None

    def test_iter_pending_yields_all(self) -> None:
        r1 = SpawnRequest(
            id="a", requester_window="@0", provider="claude", cwd="/p", prompt=""
        )
        r2 = SpawnRequest(
            id="b", requester_window="@1", provider="shell", cwd="/q", prompt=""
        )
        register_pending(r1)
        register_pending(r2)
        ids = {rid for rid, _ in iter_pending()}
        assert ids == {"a", "b"}


class TestCheckMaxWindows:
    def test_under_limit_returns_true(self) -> None:
        assert (
            check_max_windows({"@0": object(), "@1": object()}, max_windows=5) is True
        )

    def test_at_limit_returns_false(self) -> None:
        states = {f"@{i}": object() for i in range(5)}
        assert check_max_windows(states, max_windows=5) is False

    def test_empty_returns_true(self) -> None:
        assert check_max_windows({}, max_windows=1) is True


class TestSpawnRateLimit:
    def test_under_rate_returns_true(self, tmp_path) -> None:
        assert check_spawn_rate("@0", max_rate=3) is True

    def test_exceeds_rate_returns_false(self, tmp_path) -> None:
        for _ in range(3):
            record_spawn("@0")
        assert check_spawn_rate("@0", max_rate=3) is False

    def test_different_windows_isolated(self, tmp_path) -> None:
        for _ in range(3):
            record_spawn("@0")
        assert check_spawn_rate("@1", max_rate=3) is True

    def test_record_creates_rate_log(self, tmp_path) -> None:
        record_spawn("@0")
        log_path = spawns_dir() / "rate_log.json"
        assert log_path.exists()
        data = json.loads(log_path.read_text())
        assert "@0" in data
        assert len(data["@0"]) == 1


class TestCreateSpawnRequest:
    def test_creates_file_and_registers(self, tmp_path) -> None:
        req = create_spawn_request(
            requester_window="@0",
            provider="claude",
            cwd=str(tmp_path),
            prompt="do work",
        )
        assert req.id in {rid for rid, _ in iter_pending()}
        spawn_file = spawns_dir() / f"{req.id}.json"
        assert spawn_file.exists()

    def test_invalid_cwd_raises(self) -> None:
        with pytest.raises(ValueError, match="cwd does not exist"):
            create_spawn_request(
                requester_window="@0",
                provider="claude",
                cwd="/this/does/not/exist/ccgram-test",
                prompt="x",
            )

    def test_optional_fields_persisted(self, tmp_path) -> None:
        req = create_spawn_request(
            requester_window="@0",
            provider="codex",
            cwd=str(tmp_path),
            prompt="hello",
            context_file="/tmp/ctx.md",
            auto=True,
        )
        assert req.provider == "codex"
        assert req.context_file == "/tmp/ctx.md"
        assert req.auto is True


class TestScanSpawnRequests:
    def test_returns_empty_when_no_dir(self) -> None:
        assert scan_spawn_requests() == []

    def test_loads_new_requests_from_disk(self, tmp_path) -> None:
        req = create_spawn_request(
            requester_window="@0",
            provider="claude",
            cwd=str(tmp_path),
            prompt="go",
        )
        reset_spawn_state()
        found = scan_spawn_requests()
        assert len(found) == 1
        assert found[0].id == req.id

    def test_skips_already_cached(self, tmp_path) -> None:
        create_spawn_request(
            requester_window="@0",
            provider="claude",
            cwd=str(tmp_path),
            prompt="go",
        )
        found_first = scan_spawn_requests()
        found_second = scan_spawn_requests()
        assert len(found_first) == 0
        assert len(found_second) == 0

    def test_evicts_expired_from_cache(self, tmp_path) -> None:
        req = SpawnRequest(
            id="old",
            requester_window="@0",
            provider="claude",
            cwd=str(tmp_path),
            prompt="",
            created_at=time.time() - 400,
        )
        register_pending(req)
        scan_spawn_requests(spawn_timeout=300)
        assert get_pending("old") is None

    def test_skips_expired_file(self, tmp_path) -> None:
        sdir = spawns_dir()
        sdir.mkdir(parents=True, exist_ok=True)
        req = SpawnRequest(
            id="expired-file",
            requester_window="@0",
            provider="claude",
            cwd=str(tmp_path),
            prompt="",
            created_at=time.time() - 400,
        )
        (sdir / f"{req.id}.json").write_text(json.dumps(req.to_dict()))
        reset_spawn_state()
        found = scan_spawn_requests(spawn_timeout=300)
        assert found == []
        assert not (sdir / f"{req.id}.json").exists()

    def test_skips_malformed_json(self, tmp_path) -> None:
        sdir = spawns_dir()
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "bad.json").write_text("{not valid json")
        assert scan_spawn_requests() == []

    def test_skips_rate_log_file(self, tmp_path) -> None:
        record_spawn("@0")
        reset_spawn_state()
        found = scan_spawn_requests()
        assert found == []
