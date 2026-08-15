"""Live visibility for a scheduled job while it runs.

A cron job used to be observable only after it settled: the archived ``.md``
transcript and ``cron.completed`` both land at the end. These tests cover the
in-flight half — the run record a job publishes while executing, the events
that bracket it, and the ``cron.output`` view that serves a partial run to a
watching UI over any transport.
"""

import asyncio
import json

import pytest

from flowly.channels import feature_rpc
from flowly.cron.service import CronService
from flowly.cron.types import CronSchedule

EVERY_MINUTE = CronSchedule(kind="every", every_ms=60_000)


@pytest.fixture
def svc(tmp_path):
    return CronService(store_path=tmp_path / "cron.json")


@pytest.fixture
def wired(svc, monkeypatch):
    """Serve the feature RPCs from this test's service."""
    monkeypatch.setattr(feature_rpc, "_cron_provider", lambda: svc)
    return svc


# ── run tracking ────────────────────────────────────────────────────


class TestRunTracking:
    async def test_idle_service_reports_no_runs(self, svc):
        job = svc.add_job("idle", EVERY_MINUTE, "msg")
        assert svc.running_runs() == []
        assert svc.current_run(job.id) is None

    async def test_run_is_visible_while_the_callback_is_awaited(self, svc):
        job = svc.add_job("live", EVERY_MINUTE, "msg")
        seen: dict = {}

        async def on_job(j):
            seen["run"] = svc.current_run(j.id)
            seen["all"] = svc.running_runs()
            return "done"

        svc.on_job = on_job
        await svc.run_job(job.id)

        run = seen["run"]
        assert run is not None
        assert run["jobId"] == job.id
        assert run["jobName"] == "live"
        assert run["sessionKey"] == f"cron:{job.id}"
        assert run["scheduleKind"] == "every"
        assert run["runId"]
        assert run["startedAtMs"] > 0
        assert [r["runId"] for r in seen["all"]] == [run["runId"]]

    async def test_run_record_is_retired_when_the_job_settles(self, svc):
        job = svc.add_job("settles", EVERY_MINUTE, "msg")
        svc.on_job = lambda j: asyncio.sleep(0, result="ok")

        await svc.run_job(job.id)

        assert svc.current_run(job.id) is None
        assert svc.running_runs() == []

    async def test_run_record_is_retired_when_the_agent_raises(self, svc):
        job = svc.add_job("boom", EVERY_MINUTE, "msg")

        async def on_job(_j):
            raise RuntimeError("provider down")

        svc.on_job = on_job
        await svc.run_job(job.id)

        assert svc.current_run(job.id) is None
        # The failure still lands on the job the ordinary way.
        assert svc.list_jobs()[0].state.last_status == "error"

    async def test_run_record_is_retired_when_the_callback_is_cancelled(self, svc):
        """A cancelled run must not leave a job pinned as 'running' forever."""
        job = svc.add_job("cancelled", EVERY_MINUTE, "msg")
        started = asyncio.Event()

        async def on_job(_j):
            started.set()
            await asyncio.sleep(3600)

        svc.on_job = on_job
        task = asyncio.create_task(svc.run_job(job.id))
        await started.wait()
        assert svc.current_run(job.id) is not None

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert svc.current_run(job.id) is None

    async def test_records_are_copies(self, svc):
        """A caller mutating what it got back can't corrupt the bookkeeping."""
        job = svc.add_job("copies", EVERY_MINUTE, "msg")
        captured: dict = {}

        async def on_job(j):
            got = svc.current_run(j.id)
            got["jobName"] = "tampered"
            captured["after"] = svc.current_run(j.id)
            return "ok"

        svc.on_job = on_job
        await svc.run_job(job.id)

        assert captured["after"]["jobName"] == "copies"

    async def test_concurrent_runs_are_tracked_per_job(self, svc):
        """Two jobs running at once each keep their own marker."""
        a = svc.add_job("a", EVERY_MINUTE, "msg")
        b = svc.add_job("b", EVERY_MINUTE, "msg")
        release = asyncio.Event()

        async def on_job(_j):
            await release.wait()
            return "ok"

        svc.on_job = on_job
        ta = asyncio.create_task(svc.run_job(a.id))
        tb = asyncio.create_task(svc.run_job(b.id))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert {r["jobId"] for r in svc.running_runs()} == {a.id, b.id}

        release.set()
        await asyncio.gather(ta, tb)
        assert svc.running_runs() == []

    async def test_running_runs_are_ordered_oldest_first(self, svc):
        a = svc.add_job("first", EVERY_MINUTE, "msg")
        b = svc.add_job("second", EVERY_MINUTE, "msg")
        release = asyncio.Event()

        async def on_job(_j):
            await release.wait()
            return "ok"

        svc.on_job = on_job
        ta = asyncio.create_task(svc.run_job(a.id))
        await asyncio.sleep(0.01)
        tb = asyncio.create_task(svc.run_job(b.id))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        order = [r["jobId"] for r in svc.running_runs()]
        release.set()
        await asyncio.gather(ta, tb)

        assert order == [a.id, b.id]


# ── lifecycle events ────────────────────────────────────────────────


class TestLifecycleEvents:
    async def test_started_fires_before_the_job_body(self, svc):
        job = svc.add_job("evented", EVERY_MINUTE, "msg")
        order: list[str] = []
        events: list[tuple[str, dict]] = []

        async def on_run_start(name, data):
            order.append("start")
            events.append((name, data))

        async def on_job(_j):
            order.append("body")
            return "ok"

        svc.on_run_start = on_run_start
        svc.on_job = on_job
        await svc.run_job(job.id)

        assert order == ["start", "body"]
        name, data = events[0]
        assert name == "cron.started"
        assert data["jobId"] == job.id
        assert data["sessionKey"] == f"cron:{job.id}"
        assert data["runId"]

    async def test_started_and_completed_share_a_run_id(self, svc):
        job = svc.add_job("paired", EVERY_MINUTE, "msg")
        seen: dict[str, dict] = {}

        async def record(name, data):
            seen[name] = data

        svc.on_run_start = record
        svc.on_complete = record
        svc.on_job = lambda _j: asyncio.sleep(0, result="ok")
        await svc.run_job(job.id)

        assert seen["cron.started"]["runId"] == seen["cron.completed"]["runId"]

    async def test_a_broken_start_callback_never_blocks_the_run(self, svc):
        job = svc.add_job("resilient", EVERY_MINUTE, "msg")
        ran = asyncio.Event()

        async def on_run_start(_name, _data):
            raise RuntimeError("socket closed")

        async def on_job(_j):
            ran.set()
            return "ok"

        svc.on_run_start = on_run_start
        svc.on_job = on_job
        await svc.run_job(job.id)

        assert ran.is_set()
        assert svc.list_jobs()[0].state.last_status == "ok"

    async def test_started_is_not_required(self, svc):
        """No callback wired is the default; the run still tracks itself."""
        job = svc.add_job("silent", EVERY_MINUTE, "msg")
        seen: dict = {}

        async def on_job(j):
            seen["run"] = svc.current_run(j.id)
            return "ok"

        svc.on_job = on_job
        await svc.run_job(job.id)

        assert seen["run"] is not None


# ── status / health surfaces ────────────────────────────────────────


class TestStatusSurfaces:
    async def test_status_and_health_carry_the_live_run(self, svc):
        job = svc.add_job("watched", EVERY_MINUTE, "msg")
        seen: dict = {}

        async def on_job(_j):
            seen["status"] = svc.status()
            seen["health"] = svc.health_report()
            return "ok"

        svc.on_job = on_job
        await svc.run_job(job.id)

        assert [r["jobId"] for r in seen["status"]["running"]] == [job.id]
        assert [r["jobId"] for r in seen["health"]["running"]] == [job.id]

    async def test_health_reports_no_runs_when_idle(self, svc):
        svc.add_job("quiet", EVERY_MINUTE, "msg")
        assert svc.health_report()["running"] == []


# ── cron.list RPC ───────────────────────────────────────────────────


class TestCronListRpc:
    async def test_idle_job_is_not_running(self, wired):
        job = wired.add_job("idle", EVERY_MINUTE, "msg")
        res = feature_rpc.cron_list({})

        assert res["running"] == []
        (entry,) = res["jobs"]
        assert entry["state"]["running"] is False
        assert entry["state"]["runId"] is None
        assert entry["state"]["runStartedAtMs"] is None
        assert entry["sessionKey"] == f"cron:{job.id}"

    async def test_executing_job_is_marked_on_its_row(self, wired):
        job = wired.add_job("busy", EVERY_MINUTE, "msg")
        seen: dict = {}

        async def on_job(_j):
            seen["res"] = feature_rpc.cron_list({})
            return "ok"

        wired.on_job = on_job
        await wired.run_job(job.id)

        res = seen["res"]
        (entry,) = res["jobs"]
        assert entry["state"]["running"] is True
        assert entry["state"]["runId"] == res["running"][0]["runId"]
        assert entry["state"]["runStartedAtMs"] > 0

    async def test_only_the_executing_job_is_marked(self, wired):
        busy = wired.add_job("busy", EVERY_MINUTE, "msg")
        wired.add_job("idle", EVERY_MINUTE, "msg")
        seen: dict = {}

        async def on_job(_j):
            seen["jobs"] = feature_rpc.cron_list({})["jobs"]
            return "ok"

        wired.on_job = on_job
        await wired.run_job(busy.id)

        by_id = {j["id"]: j for j in seen["jobs"]}
        assert by_id[busy.id]["state"]["running"] is True
        assert all(
            j["state"]["running"] is False for jid, j in by_id.items() if jid != busy.id
        )


# ── cron.output RPC ─────────────────────────────────────────────────


class TestCronOutputRpc:
    async def test_idle_job_has_no_live_run(self, wired):
        job = wired.add_job("idle", EVERY_MINUTE, "msg")
        res = feature_rpc.cron_output({"id": job.id})

        assert res["live"] is None
        assert res["outputs"] == []

    async def test_partial_output_is_served_while_the_run_is_open(self, wired):
        from flowly.agent import inflight

        job = wired.add_job("streaming", EVERY_MINUTE, "msg")
        seen: dict = {}

        async def on_job(j):
            run = wired.current_run(j.id)
            inflight.begin(run["sessionKey"], run["runId"], "the prompt")
            inflight.append(run["sessionKey"], run["runId"], "half a ")
            inflight.append(run["sessionKey"], run["runId"], "sentence")
            inflight.append_iteration(
                run["sessionKey"],
                run["runId"],
                {"role": "tool", "name": "web_search", "content": "3 results"},
            )
            seen["res"] = feature_rpc.cron_output({"id": j.id})
            inflight.finish(run["sessionKey"], run["runId"])
            return "ok"

        wired.on_job = on_job
        await wired.run_job(job.id)

        live = seen["res"]["live"]
        assert live["text"] == "half a sentence"
        assert live["sessionKey"] == f"cron:{job.id}"
        assert live["runId"]
        assert [i["name"] for i in live["iterations"]] == ["web_search"]

    async def test_a_stale_registry_entry_is_not_replayed(self, wired):
        """Output from a PREVIOUS run must never surface as the current one."""
        from flowly.agent import inflight

        job = wired.add_job("stale", EVERY_MINUTE, "msg")
        seen: dict = {}

        async def on_job(j):
            run = wired.current_run(j.id)
            # Left behind by an earlier run that never retired its entry.
            inflight.begin(run["sessionKey"], "an-older-run", "old prompt")
            inflight.append(run["sessionKey"], "an-older-run", "yesterday's answer")
            seen["res"] = feature_rpc.cron_output({"id": j.id})
            inflight.finish(run["sessionKey"], "an-older-run")
            return "ok"

        wired.on_job = on_job
        await wired.run_job(job.id)

        live = seen["res"]["live"]
        assert live["text"] == ""
        assert live["iterations"] == []

    async def test_archived_runs_are_still_returned(self, wired):
        job = wired.add_job("archived", EVERY_MINUTE, "hello")
        wired.on_job = lambda _j: asyncio.sleep(0, result="the answer")
        await wired.run_job(job.id)

        res = feature_rpc.cron_output({"id": job.id})
        assert res["live"] is None
        assert len(res["outputs"]) == 1
        assert "the answer" in res["outputs"][0]["content"]

    async def test_output_can_be_addressed_by_job_name(self, wired):
        job = wired.add_job("by-name", EVERY_MINUTE, "hello")
        wired.on_job = lambda _j: asyncio.sleep(0, result="named answer")
        await wired.run_job(job.id)

        res = feature_rpc.cron_output({"id": "by-name"})
        assert len(res["outputs"]) == 1
        assert "named answer" in res["outputs"][0]["content"]

    async def test_unknown_job_is_empty_not_an_error(self, wired):
        res = feature_rpc.cron_output({"id": "no-such-job"})
        assert res == {"outputs": [], "live": None, "artifacts": []}


# ── cron.run RPC ────────────────────────────────────────────────────


class TestCronRunRpc:
    async def test_blocking_run_still_waits(self, wired):
        job = wired.add_job("blocking", EVERY_MINUTE, "msg")
        done = asyncio.Event()

        async def on_job(_j):
            await asyncio.sleep(0.01)
            done.set()
            return "ok"

        wired.on_job = on_job
        res = await feature_rpc.cron_run({"id": job.id})

        assert res == {"ok": True}
        assert done.is_set()

    async def test_detached_run_returns_before_the_job_finishes(self, wired):
        job = wired.add_job("detached", EVERY_MINUTE, "msg")
        release = asyncio.Event()
        finished = asyncio.Event()

        async def on_job(_j):
            await release.wait()
            finished.set()
            return "ok"

        wired.on_job = on_job
        res = await feature_rpc.cron_run({"id": job.id, "wait": False})

        assert res["ok"] is True
        assert res["started"] is True
        assert res["sessionKey"] == f"cron:{job.id}"
        assert not finished.is_set()

        # The run really is in flight, and settles on its own.
        await asyncio.sleep(0)
        assert wired.current_run(job.id) is not None
        release.set()
        await asyncio.wait_for(finished.wait(), timeout=1)

    async def test_detached_run_rejects_an_unknown_job(self, wired):
        with pytest.raises(feature_rpc.FeatureRpcError) as err:
            await feature_rpc.cron_run({"id": "nope", "wait": False})
        assert err.value.code == "NOT_FOUND"

    async def test_detached_run_rejects_a_job_already_running(self, wired):
        job = wired.add_job("double", EVERY_MINUTE, "msg")
        release = asyncio.Event()

        async def on_job(_j):
            await release.wait()
            return "ok"

        wired.on_job = on_job
        first = asyncio.create_task(wired.run_job(job.id))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        with pytest.raises(feature_rpc.FeatureRpcError) as err:
            await feature_rpc.cron_run({"id": job.id, "wait": False})
        assert err.value.code == "BUSY"

        release.set()
        await first

    async def test_detached_run_respects_force_false_on_a_disabled_job(self, wired):
        job = wired.add_job("off", EVERY_MINUTE, "msg")
        wired.enable_job(job.id, enabled=False)
        ran = asyncio.Event()

        async def on_job(_j):
            ran.set()
            return "ok"

        wired.on_job = on_job
        res = await feature_rpc.cron_run({"id": job.id, "wait": False, "force": False})

        assert res == {"ok": False}
        await asyncio.sleep(0)
        assert not ran.is_set()

    async def test_cron_run_is_declared_long_running(self):
        """The blocking form spans a whole agent turn — transports must not
        serve it on their receive loop."""
        assert "cron.run" in feature_rpc.LONG_RUNNING_METHODS


# ── archived actions ────────────────────────────────────────────────


class TestRunActions:
    async def test_actions_land_in_the_archived_transcript(self, wired):
        """A finished run records what it DID, not only what it concluded."""
        job = wired.add_job("acting", EVERY_MINUTE, "go")

        async def on_job(j):
            wired.record_run_actions(j.id, ["web_search", "exec"])
            return "all done"

        wired.on_job = on_job
        await wired.run_job(job.id)

        content = feature_rpc.cron_output({"id": job.id})["outputs"][0]["content"]
        assert "## Actions" in content
        assert "- web_search" in content
        assert "- exec" in content

    async def test_a_run_with_no_tools_has_no_actions_section(self, wired):
        job = wired.add_job("quiet", EVERY_MINUTE, "go")
        wired.on_job = lambda _j: asyncio.sleep(0, result="just an answer")
        await wired.run_job(job.id)

        content = feature_rpc.cron_output({"id": job.id})["outputs"][0]["content"]
        assert "## Actions" not in content

    async def test_actions_do_not_leak_into_the_next_run(self, svc):
        """Each run's record is its own; a late report can't attach to a
        run that has already been retired."""
        job = svc.add_job("sequential", EVERY_MINUTE, "go")
        calls: list[int] = []

        async def on_job(j):
            calls.append(1)
            if len(calls) == 1:
                svc.record_run_actions(j.id, ["web_search"])
            return "ok"

        svc.on_job = on_job
        await svc.run_job(job.id)
        # Reported after the first run retired — must be dropped, not carried.
        svc.record_run_actions(job.id, ["should_not_stick"])
        await svc.run_job(job.id)

        archives = sorted((svc.store_path.parent / "output" / job.id).glob("*.md"))
        latest = archives[-1].read_text()
        assert "should_not_stick" not in latest

    async def test_a_failed_run_still_records_its_actions(self, wired):
        job = wired.add_job("failing", EVERY_MINUTE, "go")

        async def on_job(j):
            wired.record_run_actions(j.id, ["exec"])
            raise RuntimeError("tool blew up")

        wired.on_job = on_job
        await wired.run_job(job.id)

        content = feature_rpc.cron_output({"id": job.id})["outputs"][0]["content"]
        assert "- exec" in content
        assert "(FAILED)" in content


# ── files a run wrote ───────────────────────────────────────────────


class TestRunFiles:
    async def test_written_files_land_in_the_transcript(self, wired):
        """A job whose deliverable is a file leaves a trace of where."""
        job = wired.add_job("writer", EVERY_MINUTE, "write a report")

        async def on_job(j):
            wired.record_run_files(j.id, ["~/Desktop/text.md"])
            return "Masaüstünde `text.md` dosyası oluşturuldu."

        wired.on_job = on_job
        await wired.run_job(job.id)

        content = feature_rpc.cron_output({"id": job.id})["outputs"][0]["content"]
        assert "## Files" in content
        assert "- ~/Desktop/text.md" in content

    async def test_a_run_that_wrote_nothing_has_no_files_section(self, wired):
        job = wired.add_job("talker", EVERY_MINUTE, "just answer")
        wired.on_job = lambda _j: asyncio.sleep(0, result="an answer")
        await wired.run_job(job.id)

        content = feature_rpc.cron_output({"id": job.id})["outputs"][0]["content"]
        assert "## Files" not in content

    async def test_files_are_visible_before_the_run_finishes(self, wired):
        job = wired.add_job("early", EVERY_MINUTE, "write then think")
        seen: dict = {}

        async def on_job(j):
            wired.record_run_files(j.id, ["/tmp/out.md"])
            seen["live"] = feature_rpc.cron_output({"id": j.id})["live"]
            return "ok"

        wired.on_job = on_job
        await wired.run_job(job.id)

        assert seen["live"]["files"] == ["/tmp/out.md"]

    async def test_files_do_not_leak_into_the_next_run(self, svc):
        job = svc.add_job("sequential", EVERY_MINUTE, "go")
        svc.on_job = lambda _j: asyncio.sleep(0, result="ok")

        await svc.run_job(job.id)
        svc.record_run_files(job.id, ["/tmp/stale.md"])
        await svc.run_job(job.id)

        latest = sorted((svc.store_path.parent / "output" / job.id).glob("*.md"))[-1]
        assert "stale.md" not in latest.read_text()


# ── artifacts produced by a job ─────────────────────────────────────


class TestCronArtifacts:
    async def test_artifacts_are_absent_when_none_were_produced(self, wired):
        job = wired.add_job("no-files", EVERY_MINUTE, "go")
        assert feature_rpc.cron_output({"id": job.id})["artifacts"] == []

    async def test_artifacts_of_this_job_session_are_returned(self, wired, monkeypatch):
        job = wired.add_job("writer", EVERY_MINUTE, "go")
        seen: dict = {}

        class _Store:
            def list(self, *, session_key=None, limit=50, **_kw):
                seen["sessionKey"] = session_key
                return [
                    {
                        "id": "art-1",
                        "type": "markdown",
                        "title": "Weekly report",
                        "session_key": session_key,
                    }
                ]

        monkeypatch.setattr(feature_rpc, "_artifact_store", lambda: _Store())

        artifacts = feature_rpc.cron_output({"id": job.id})["artifacts"]

        assert seen["sessionKey"] == f"cron:{job.id}"
        assert [a["id"] for a in artifacts] == ["art-1"]
        assert artifacts[0]["title"] == "Weekly report"

    async def test_a_broken_artifact_store_never_fails_the_call(self, wired, monkeypatch):
        job = wired.add_job("resilient", EVERY_MINUTE, "go")

        def _boom():
            raise RuntimeError("store offline")

        monkeypatch.setattr(feature_rpc, "_artifact_store", _boom)

        res = feature_rpc.cron_output({"id": job.id})
        assert res["artifacts"] == []
        assert res["live"] is None


# ── persistence is untouched by run tracking ────────────────────────


class TestStoreIsUnaffected:
    async def test_run_state_is_not_persisted(self, svc, tmp_path):
        """In-flight state is memory-only — a restart must not resurrect it."""
        job = svc.add_job("mem", EVERY_MINUTE, "msg")
        svc.on_job = lambda _j: asyncio.sleep(0, result="ok")
        await svc.run_job(job.id)

        raw = json.loads((tmp_path / "cron.json").read_text())
        assert "running" not in raw["jobs"][0]["state"]
        assert "runId" not in raw["jobs"][0]["state"]

        restarted = CronService(store_path=tmp_path / "cron.json")
        assert restarted.running_runs() == []
