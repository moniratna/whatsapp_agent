from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI

import app.main as main


class FakeProcess:
    returncode: int | None = None
    pid = 1234

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode or 0


def test_embedded_mode_starts_all_worker_modules_and_stops_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main.settings, "app_env", "development")
    monkeypatch.setattr(main.settings, "run_workers_in_api", True)
    started: list[str] = []
    processes: list[FakeProcess] = []

    async def start_process(*args: Any, **kwargs: Any) -> FakeProcess:
        started.append(str(args[2]))
        process = FakeProcess()
        processes.append(process)
        return process

    monkeypatch.setattr(main.asyncio, "create_subprocess_exec", start_process)
    app = FastAPI()

    async def exercise() -> None:
        async with main.lifespan(app):
            assert len(app.state.worker_processes) == 7

    asyncio.run(exercise())

    assert started == [
        "app.workers.conversation",
        "app.workers.outbound",
        "app.workers.crm_sync",
        "app.workers.exports",
        "app.workers.skill_reviews",
        "app.workers.skill_updates",
        "app.workers.reviewer_updates",
    ]
    assert all(process.returncode == 0 for process in processes)


def test_embedded_mode_rejects_non_local_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main.settings, "app_env", "production")
    monkeypatch.setattr(main.settings, "run_workers_in_api", True)
    monkeypatch.setattr(
        main.asyncio,
        "create_subprocess_exec",
        lambda *args, **kwargs: pytest.fail("must reject before spawning"),
    )
    async def exercise() -> None:
        async with main.lifespan(FastAPI()):
            pass

    with pytest.raises(RuntimeError, match="restricted to local development and test"):
        asyncio.run(exercise())
