"""Unit tests for the HTTP service (no real browser, no network)."""

import asyncio
import time

import httpx
import pytest

from solver.app import create_app
from solver.browser import SolveFailed, TokenResult


def make_client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://solver")


def post(client, url="/solve", payload=None, headers=None):
    body = payload if payload is not None else {"url": "https://example.com/", "sitekey": "0xKEY"}
    return asyncio.run(_post(client, url, body, headers))


async def _post(client, url, body, headers):
    return await client.post(url, json=body, headers=headers or {})


def test_root_describes_the_service():
    app = create_app(lambda: pytest.fail("solver must not be built"))

    async def go():
        async with make_client(app) as client:
            return await client.get("/")

    response = asyncio.run(go())
    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "turnstile solver"
    assert "version" in body
    assert "/solve" in body["endpoints"]


def test_healthz():
    app = create_app(lambda: pytest.fail("solver must not be built for /healthz"))

    async def go():
        async with make_client(app) as client:
            return await client.get("/healthz")

    response = asyncio.run(go())
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_version():
    app = create_app(lambda: pytest.fail("solver must not be built for /version"))

    async def go():
        async with make_client(app) as client:
            return await client.get("/version")

    response = asyncio.run(go())
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "solver"
    assert body["version"]


def test_solve_returns_token():
    class FakeSolver:
        def get_token(self, url, sitekey, theme="dark", size="normal",
                      appearance="always", action=None, c_data=None,
                      execute=False, timeout=None):
            return TokenResult(token="tok-1", user_agent="ua", elapsed_ms=12)

    app = create_app(lambda: FakeSolver())

    async def go():
        async with make_client(app) as client:
            return await client.post(
                "/solve", json={"url": "https://example.com/", "sitekey": "0xKEY"}
            )

    response = asyncio.run(go())
    assert response.status_code == 200
    assert response.json() == {"token": "tok-1", "userAgent": "ua", "elapsed_ms": 12}


def test_solve_passes_request_fields_to_solver():
    seen = {}

    class FakeSolver:
        def get_token(self, url, sitekey, theme="dark", size="normal",
                      appearance="always", action=None, c_data=None,
                      execute=False, timeout=None):
            seen.update(
                url=url, sitekey=sitekey, theme=theme, size=size,
                appearance=appearance, action=action, c_data=c_data,
                execute=execute, timeout=timeout,
            )
            return TokenResult("tok", "ua", 1)

    app = create_app(lambda: FakeSolver())

    async def go():
        async with make_client(app) as client:
            return await client.post(
                "/solve",
                json={
                    "url": "https://example.com/login",
                    "sitekey": "0xKEY",
                    "action": "login",
                    "cData": "eyJpZCI6MX0=",
                    "theme": "light",
                    "size": "compact",
                    "appearance": "interaction-only",
                    "execute": True,
                    "timeout": 12.5,
                },
            )

    assert asyncio.run(go()).status_code == 200
    assert seen == {
        "url": "https://example.com/login",
        "sitekey": "0xKEY",
        "theme": "light",
        "size": "compact",
        "appearance": "interaction-only",
        "action": "login",
        "c_data": "eyJpZCI6MX0=",
        "execute": True,
        "timeout": 12.5,
    }


def test_solve_failure_returns_502_with_stage():
    class FailingSolver:
        def get_token(self, *args, **kwargs):
            raise SolveFailed("no_widget", "widget never appeared")

    app = create_app(lambda: FailingSolver())

    async def go():
        async with make_client(app) as client:
            return await client.post(
                "/solve", json={"url": "https://example.com/", "sitekey": "0xKEY"}
            )

    response = asyncio.run(go())
    assert response.status_code == 502
    assert response.json() == {
        "detail": {"error": "widget never appeared", "stage": "no_widget"}
    }


def test_solve_system_exit_returns_502():
    class ExitingSolver:
        def get_token(self, *args, **kwargs):
            raise SystemExit("NOTE: You must install tkinter")

    app = create_app(lambda: ExitingSolver())

    async def go():
        async with make_client(app) as client:
            return await client.post(
                "/solve", json={"url": "https://example.com/", "sitekey": "0xKEY"}
            )

    response = asyncio.run(go())
    assert response.status_code == 502
    assert response.json()["detail"]["stage"] == "unexpected"


def test_solve_serialized_by_lock():
    state = {"active": 0, "peak": 0}

    class SlowSolver:
        def get_token(self, *args, **kwargs):
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            try:
                time.sleep(0.1)
                return TokenResult("tok", "ua", 100)
            finally:
                state["active"] -= 1

    app = create_app(lambda: SlowSolver())

    async def go():
        payload = {"url": "https://example.com/", "sitekey": "0xKEY"}
        async with make_client(app) as client:
            return await asyncio.gather(
                client.post("/solve", json=payload),
                client.post("/solve", json=payload),
            )

    responses = asyncio.run(go())
    assert [r.status_code for r in responses] == [200, 200]
    assert state["peak"] == 1


def test_busy_returns_429():
    class SlowSolver:
        def get_token(self, *args, **kwargs):
            time.sleep(0.3)
            return TokenResult("tok", "ua", 100)

    app = create_app(lambda: SlowSolver(), queue_timeout=0.05)

    async def go():
        payload = {"url": "https://example.com/", "sitekey": "0xKEY"}
        async with make_client(app) as client:
            return await asyncio.gather(
                client.post("/solve", json=payload),
                client.post("/solve", json=payload),
            )

    responses = asyncio.run(go())
    assert [r.status_code for r in responses] == [200, 429]
    busy = responses[1].json()
    assert busy["detail"]["stage"] == "busy"


# --- validation ---------------------------------------------------------


def test_missing_sitekey_returns_400_envelope():
    app = create_app(lambda: FakeSolverOk())

    async def go():
        async with make_client(app) as client:
            return await client.post("/solve", json={"url": "https://example.com/"})

    response = asyncio.run(go())
    assert response.status_code == 400
    assert response.json()["detail"]["stage"] == "invalid_request"


def test_bad_url_returns_400_envelope():
    app = create_app(lambda: FakeSolverOk())

    async def go():
        async with make_client(app) as client:
            return await client.post(
                "/solve", json={"url": "ftp://example.com/", "sitekey": "0xKEY"}
            )

    response = asyncio.run(go())
    assert response.status_code == 400
    assert response.json()["detail"]["stage"] == "invalid_request"


def test_bad_theme_returns_400_envelope():
    app = create_app(lambda: FakeSolverOk())

    async def go():
        async with make_client(app) as client:
            return await client.post(
                "/solve",
                json={"url": "https://example.com/", "sitekey": "0xK", "theme": "neon"},
            )

    response = asyncio.run(go())
    assert response.status_code == 400
    assert response.json()["detail"]["stage"] == "invalid_request"


def test_timeout_out_of_range_returns_400_envelope():
    app = create_app(lambda: FakeSolverOk())

    async def go():
        async with make_client(app) as client:
            return await client.post(
                "/solve",
                json={"url": "https://example.com/", "sitekey": "0xK", "timeout": 0.1},
            )

    response = asyncio.run(go())
    assert response.status_code == 400
    assert response.json()["detail"]["stage"] == "invalid_request"


def test_action_too_long_returns_400_envelope():
    app = create_app(lambda: FakeSolverOk())

    async def go():
        async with make_client(app) as client:
            return await client.post(
                "/solve",
                json={
                    "url": "https://example.com/",
                    "sitekey": "0xK",
                    "action": "a" * 33,
                },
            )

    response = asyncio.run(go())
    assert response.status_code == 400
    assert response.json()["detail"]["stage"] == "invalid_request"


class FakeSolverOk:
    def get_token(self, *args, **kwargs):
        return TokenResult("tok", "ua", 1)


# --- authentication -----------------------------------------------------


def test_missing_api_key_returns_401():
    app = create_app(lambda: FakeSolverOk(), api_key="secret")

    async def go():
        async with make_client(app) as client:
            return await client.post(
                "/solve", json={"url": "https://example.com/", "sitekey": "0xKEY"}
            )

    response = asyncio.run(go())
    assert response.status_code == 401
    assert response.json()["detail"]["stage"] == "unauthorized"


def test_wrong_api_key_returns_401():
    app = create_app(lambda: FakeSolverOk(), api_key="secret")

    async def go():
        async with make_client(app) as client:
            return await client.post(
                "/solve",
                json={"url": "https://example.com/", "sitekey": "0xKEY"},
                headers={"X-Solver-Key": "nope"},
            )

    response = asyncio.run(go())
    assert response.status_code == 401
    assert response.json()["detail"]["stage"] == "unauthorized"


def test_valid_api_key_header_passes():
    app = create_app(lambda: FakeSolverOk(), api_key="secret")

    async def go():
        async with make_client(app) as client:
            return await client.post(
                "/solve",
                json={"url": "https://example.com/", "sitekey": "0xKEY"},
                headers={"X-Solver-Key": "secret"},
            )

    response = asyncio.run(go())
    assert response.status_code == 200


def test_valid_bearer_token_passes():
    app = create_app(lambda: FakeSolverOk(), api_key="secret")

    async def go():
        async with make_client(app) as client:
            return await client.post(
                "/solve",
                json={"url": "https://example.com/", "sitekey": "0xKEY"},
                headers={"Authorization": "Bearer secret"},
            )

    response = asyncio.run(go())
    assert response.status_code == 200


def test_no_api_key_configured_allows_everything(monkeypatch):
    monkeypatch.delenv("SOLVER_API_KEY", raising=False)
    app = create_app(lambda: FakeSolverOk())

    async def go():
        async with make_client(app) as client:
            return await client.post(
                "/solve", json={"url": "https://example.com/", "sitekey": "0xKEY"}
            )

    assert asyncio.run(go()).status_code == 200


def test_api_key_taken_from_environment(monkeypatch):
    monkeypatch.setenv("SOLVER_API_KEY", "secret")

    app = create_app(lambda: FakeSolverOk())

    async def go():
        async with make_client(app) as client:
            good = await client.post(
                "/solve",
                json={"url": "https://example.com/", "sitekey": "0xKEY"},
                headers={"X-Solver-Key": "secret"},
            )
            bad = await client.post(
                "/solve", json={"url": "https://example.com/", "sitekey": "0xKEY"}
            )
        return good, bad

    good, bad = asyncio.run(go())
    assert good.status_code == 200
    assert bad.status_code == 401


def test_healthz_does_not_need_the_api_key():
    app = create_app(lambda: FakeSolverOk(), api_key="secret")

    async def go():
        async with make_client(app) as client:
            return await client.get("/healthz")

    assert asyncio.run(go()).status_code == 200