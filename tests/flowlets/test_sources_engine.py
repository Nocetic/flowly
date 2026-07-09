"""The source refresh engine — resolve an agent source into state, due/backoff."""

from __future__ import annotations

from datetime import timezone

from flowly.flowlets.sources import SourceEngine, _coerce_into, _extract_json
from flowly.flowlets.store import now_ms

UTC = timezone.utc

REPO = {
    "catalog": 1, "name": "Repo",
    "state": {
        "repo": {"type": "string", "default": "Nocetic/flowly"},
        "commits": {"type": "list", "item": {"title": "string", "who": "string"}, "source": True},
    },
    "sources": {
        "commits": {"kind": "agent", "prompt": "last commits to {repo}",
                    "into": "commits", "refresh": "15m", "limit": 5},
    },
    "layout": [{"type": "repeater", "source": "commits",
                "item": {"type": "text", "text": "{$.title}"}}],
}


class _Runner:
    def __init__(self, reply):
        self.reply = reply
        self.prompts: list[str] = []

    async def __call__(self, flowlet, prompt):
        self.prompts.append(prompt)
        return self.reply(prompt) if callable(self.reply) else self.reply


# ── extraction / coercion units ───────────────────────────────────────────────

def test_extract_json_variants():
    assert _extract_json('[{"a":1}]') == [{"a": 1}]
    assert _extract_json('```json\n[{"a":1}]\n```') == [{"a": 1}]
    assert _extract_json('Here you go:\n[{"a":1}, {"a":2}]\nDone') == [{"a": 1}, {"a": 2}]
    assert _extract_json("23.5") == 23.5


def test_coerce_into_list_drops_bad_rows_and_caps():
    spec = {"type": "list", "item": {"title": "string", "n": "number"}}
    data = [
        {"title": "a", "n": 3},
        {"title": "b", "n": "notnum"},   # n drops, row kept (has title)
        "garbage",                        # dropped
        {},                               # empty → dropped
        {"title": "c", "n": 5},
    ]
    out = _coerce_into(spec, data, limit=2)
    assert len(out) == 2                  # capped
    assert out[0]["title"] == "a" and out[0]["n"] == 3.0 and out[0]["id"]
    assert "n" not in out[1]              # bad number dropped, title kept


# ── engine end-to-end ─────────────────────────────────────────────────────────

async def test_agent_source_writes_list_and_broadcasts(store):
    events = []

    async def broadcast(name, data):
        events.append((name, data))

    runner = _Runner('[{"title":"fix bug","who":"hakan"},{"title":"add test","who":"claude"}]')
    eng = SourceEngine(store, broadcast=broadcast, agent_runner=runner, tz=UTC)
    f = store.create("Repo", REPO)
    fid = f["id"]

    n = await eng.refresh_flowlet(fid, force=True)
    assert n == 1
    commits = store.get_state(fid)["commits"]
    assert [c["title"] for c in commits] == ["fix bug", "add test"]
    assert all(c["id"].startswith("src_") for c in commits)
    # the prompt was templated with the live `repo` value
    assert "Nocetic/flowly" in runner.prompts[0]
    # a flowlet.state broadcast carried the fresh values
    assert events and events[0][0] == "flowlet.state"
    assert len(events[0][1]["values"]["commits"]) == 2
    # source state recorded a success
    ss = store.get_source_state(fid)["commits"]
    assert ss["last_ok_ms"] is not None and ss["fail_count"] == 0


async def test_scalar_source(store):
    d = {
        "catalog": 1, "name": "W",
        "state": {"temp": {"type": "number", "source": True}},
        "sources": {"temp": {"kind": "agent", "prompt": "temp", "into": "temp", "refresh": "30m"}},
        "layout": [{"type": "metric", "value": "temp"}],
    }
    eng = SourceEngine(store, agent_runner=_Runner("21.5"), tz=UTC)
    f = store.create("W", d)
    await eng.refresh_flowlet(f["id"], force=True)
    assert store.get_state(f["id"])["temp"] == 21.5


async def test_failure_backs_off_and_keeps_stale(store):
    good = _Runner('[{"title":"ok","who":"x"}]')
    eng = SourceEngine(store, agent_runner=good, tz=UTC)
    f = store.create("Repo", REPO)
    fid = f["id"]
    await eng.refresh_flowlet(fid, force=True)              # seed data
    assert len(store.get_state(fid)["commits"]) == 1

    async def boom(flowlet, prompt):
        raise RuntimeError("api down")

    eng._agent = boom
    ok = await eng._resolve(f, "commits", REPO["sources"]["commits"], now_ms())
    assert ok is False
    # stale data preserved, failure recorded
    assert len(store.get_state(fid)["commits"]) == 1
    ss = store.get_source_state(fid)["commits"]
    assert ss["fail_count"] == 1 and ss["last_error"]


def test_due_logic():
    now = now_ms()
    spec15 = {"refresh": "15m"}
    # never run → due
    assert SourceEngine._is_due(spec15, {}, now, on_open=False) is True
    # fresh → not due
    assert SourceEngine._is_due(spec15, {"last_ok_ms": now - 60_000}, now, False) is False
    # stale → due
    assert SourceEngine._is_due(spec15, {"last_ok_ms": now - 20 * 60_000}, now, False) is True
    # manual → only on first open
    man = {"refresh": "manual"}
    assert SourceEngine._is_due(man, {}, now, on_open=True) is True
    assert SourceEngine._is_due(man, {}, now, on_open=False) is False
    assert SourceEngine._is_due(man, {"last_ok_ms": now}, now, on_open=True) is False
    # backoff after failures: not due right after an error
    assert SourceEngine._is_due(spec15, {"fail_count": 2, "last_err_ms": now - 1000}, now, False) is False


async def test_no_runner_is_noop(store):
    eng = SourceEngine(store, agent_runner=None, tz=UTC)
    f = store.create("Repo", REPO)
    assert await eng.refresh_flowlet(f["id"], force=True) == 0
