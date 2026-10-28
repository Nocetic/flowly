"""Schedule changes from the terminal while a gateway runs.

The running gateway holds the schedule in memory and saves it on its own, so
`flowly cron remove` editing jobs.json under it was undone at the gateway's
next save: the CLI said "archived" and the job kept firing.
"""

from __future__ import annotations

import asyncio
import json
import threading

from aiohttp import web
from typer.testing import CliRunner

import flowly.config.loader as config_loader
from flowly.channels import feature_rpc
from flowly.cli import live_gateway
from flowly.cli.cron_cmd import cron_app
from flowly.cron.service import CronService
from flowly.cron.types import CronSchedule


class _Gateway:
    """A running gateway's schedule: in memory, saved on its own, reached over its socket."""

    def __init__(self, store) -> None:
        self.cron = CronService(store)
        self.loop = asyncio.new_event_loop()
        self.port = 0
        ready = threading.Event()

        def serve() -> None:
            asyncio.set_event_loop(self.loop)
            app = web.Application()
            app.router.add_get("/ws", self._socket)
            self.runner = web.AppRunner(app)
            self.loop.run_until_complete(self.runner.setup())
            site = web.TCPSite(self.runner, "127.0.0.1", 0)
            self.loop.run_until_complete(site.start())
            self.port = site._server.sockets[0].getsockname()[1]
            ready.set()
            self.loop.run_forever()

        self.thread = threading.Thread(target=serve, daemon=True)
        self.thread.start()
        assert ready.wait(10)

    async def _socket(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for message in ws:
            frame = json.loads(message.data)
            try:
                result, _ = await feature_rpc.dispatch(frame["method"], frame["params"])
                await ws.send_json({"type": "rpc", "id": frame["id"], "result": result})
            except feature_rpc.FeatureRpcError as exc:
                await ws.send_json({"type": "rpc", "id": frame["id"], "error": {"code": exc.code, "message": exc.message}})
        return ws

    def stop(self) -> None:
        asyncio.run_coroutine_threadsafe(self.runner.cleanup(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)


def _running_gateway(tmp_path, monkeypatch) -> _Gateway:
    gateway = _Gateway(tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(feature_rpc, "_cron_provider", lambda: gateway.cron)
    monkeypatch.setattr(live_gateway, "_target", lambda port: (gateway.port, ""))
    # Where the CLI would write on its own: the same file the gateway owns.
    monkeypatch.setattr(config_loader, "get_data_dir", lambda: tmp_path)
    return gateway


def _on_disk(tmp_path, job_id: str):
    jobs = CronService(tmp_path / "cron" / "jobs.json").list_jobs(include_disabled=True, include_archived=True)
    return next((job for job in jobs if job.id == job_id), None)


def test_a_job_removed_from_the_terminal_stays_removed(tmp_path, monkeypatch) -> None:
    gateway = _running_gateway(tmp_path, monkeypatch)
    try:
        job = gateway.cron.add_job(name="ping", schedule=CronSchedule(kind="every", every_ms=60_000), message="hi")

        result = CliRunner().invoke(cron_app, ["remove", job.id])
        assert result.exit_code == 0, result.output
        assert "Archived" in result.output
        gateway.cron._save_store()  # the gateway's own next save
    finally:
        gateway.stop()

    assert _on_disk(tmp_path, job.id).lifecycle == "archived"


def test_a_job_added_from_the_terminal_is_scheduled_by_the_gateway(tmp_path, monkeypatch) -> None:
    gateway = _running_gateway(tmp_path, monkeypatch)
    try:
        result = CliRunner().invoke(cron_app, ["add", "--name", "daily", "--message", "hi", "--every", "60"])
        assert result.exit_code == 0, result.output
        scheduled = [job.name for job in gateway.cron.list_jobs()]
        gateway.cron._save_store()
    finally:
        gateway.stop()

    assert scheduled == ["daily"]


def test_a_job_paused_from_the_terminal_stays_paused(tmp_path, monkeypatch) -> None:
    gateway = _running_gateway(tmp_path, monkeypatch)
    try:
        job = gateway.cron.add_job(name="ping", schedule=CronSchedule(kind="every", every_ms=60_000), message="hi")

        result = CliRunner().invoke(cron_app, ["enable", job.id, "--disable"])
        assert result.exit_code == 0, result.output
        gateway.cron._save_store()
    finally:
        gateway.stop()

    assert _on_disk(tmp_path, job.id).enabled is False


def test_an_unknown_job_says_so(tmp_path, monkeypatch) -> None:
    gateway = _running_gateway(tmp_path, monkeypatch)
    try:
        result = CliRunner().invoke(cron_app, ["enable", "missing"])
    finally:
        gateway.stop()

    assert result.exit_code == 0
    assert "not found" in result.output


def test_without_a_gateway_the_terminal_edits_the_schedule_itself(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(live_gateway, "_target", lambda port: None)
    monkeypatch.setattr(config_loader, "get_data_dir", lambda: tmp_path)
    job = CronService(tmp_path / "cron" / "jobs.json").add_job(
        name="ping", schedule=CronSchedule(kind="every", every_ms=60_000), message="hi",
    )

    result = CliRunner().invoke(cron_app, ["remove", job.id])

    assert result.exit_code == 0, result.output
    assert _on_disk(tmp_path, job.id).lifecycle == "archived"


async def _create(tmp_path, *, channel: str, deliver: bool) -> str:
    from flowly.agent.tools.cron import CronTool

    tool = CronTool(CronService(tmp_path / "cron" / "jobs.json"))
    tool.set_context(channel, "direct")
    return await tool.execute(action="add", name="daily", message="summarize", schedule="every 1h", deliver=deliver)


async def test_a_job_made_from_the_terminal_says_nothing_will_show_its_results(tmp_path) -> None:
    reply = await _create(tmp_path, channel="cli", deliver=True)

    assert "Delivery: this terminal conversation only" in reply
    assert "flowly cron output" in reply


async def test_a_job_that_delivers_nowhere_says_where_its_results_are(tmp_path) -> None:
    reply = await _create(tmp_path, channel="desktop", deliver=False)

    assert "Delivery: not sent anywhere" in reply


async def test_a_job_made_in_an_app_says_it_is_sent_there(tmp_path) -> None:
    reply = await _create(tmp_path, channel="telegram", deliver=True)

    assert "Delivery: sent to telegram" in reply


def _health(payload: dict):
    from io import BytesIO

    class _Response(BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    return lambda url, timeout=0: _Response(json.dumps(payload).encode())


def test_a_gateway_serving_another_home_is_left_alone(tmp_path, monkeypatch) -> None:
    """The owner's own gateway, while a test or another profile runs the CLI."""
    from flowly.gateway.identity import GATEWAY_SERVICE_ID

    monkeypatch.setattr(live_gateway, "_call", lambda *a, **k: (_ for _ in ()).throw(AssertionError("asked it")))
    monkeypatch.setattr("urllib.request.urlopen", _health({"service_id": GATEWAY_SERVICE_ID, "home_id": "someone-else"}))

    assert live_gateway.call_running_gateway("cron.remove", {"id": "x"}, port=18790) == (False, None)


def test_a_gateway_that_does_not_say_its_home_is_left_alone(monkeypatch) -> None:
    from flowly.gateway.identity import GATEWAY_SERVICE_ID

    monkeypatch.setattr("urllib.request.urlopen", _health({"service_id": GATEWAY_SERVICE_ID}))

    assert live_gateway._target(18790) is None


def test_this_homes_gateway_is_asked(monkeypatch) -> None:
    from flowly.gateway.identity import GATEWAY_SERVICE_ID, home_id

    monkeypatch.setattr("urllib.request.urlopen", _health({"service_id": GATEWAY_SERVICE_ID, "home_id": home_id()}))

    target = live_gateway._target(18790)
    assert target is not None and target[0] == 18790
