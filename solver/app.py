"""HTTP API around :class:`~solver.browser.TurnstileSolver`.

The service is a standalone, reusable Turnstile solver: callers only need an
HTTP client, never the Python package.  A single browser is shared by the whole
process, so solves are serialized with an ``asyncio.Lock``; a request that
cannot get the lock within ``SOLVER_QUEUE_TIMEOUT`` seconds gets ``429``
instead of queueing forever.  The blocking Selenium call runs in the
threadpool.

Auth is optional but recommended: set ``SOLVER_API_KEY`` and callers pass it in
``X-Solver-Key`` (or ``Authorization: Bearer <key>``).  When it is unset the
service logs a warning and accepts everything - bind it to loopback or a
private interface in that case.

Error responses all share one envelope::

    {"detail": {"stage": "<machine readable stage>", "error": "<human text>"}}
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
from enum import Enum
from typing import Callable, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import __version__
from .browser import SolveFailed, TurnstileSolver

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [solver] %(levelname)s %(message)s",
)
log = logging.getLogger("solver")

DEFAULT_QUEUE_TIMEOUT = 120.0


class Theme(str, Enum):
    auto = "auto"
    dark = "dark"
    light = "light"


class Size(str, Enum):
    normal = "normal"
    compact = "compact"
    flexible = "flexible"


class Appearance(str, Enum):
    always = "always"
    execute = "execute"
    interaction_only = "interaction-only"


class SolveRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    sitekey: str = Field(min_length=1, max_length=128)
    action: Optional[str] = Field(default=None, max_length=32)
    cData: Optional[str] = Field(default=None, max_length=255)
    theme: Theme = Theme.dark
    size: Size = Size.normal
    appearance: Appearance = Appearance.always
    execute: bool = False
    timeout: float = Field(default=60.0, ge=1.0, le=300.0)


def _queue_timeout() -> float:
    raw = os.getenv("SOLVER_QUEUE_TIMEOUT")
    if raw is None or not raw.strip():
        return DEFAULT_QUEUE_TIMEOUT
    try:
        return max(0.0, float(raw))
    except ValueError:
        log.warning("SOLVER_QUEUE_TIMEOUT=%r is not a number; using default", raw)
        return DEFAULT_QUEUE_TIMEOUT


def _configured_api_key() -> str:
    return os.getenv("SOLVER_API_KEY", "").strip()


def _provided_api_key(request: Request) -> str:
    header = request.headers.get("x-solver-key", "").strip()
    if header:
        return header
    auth = request.headers.get("authorization", "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def _error(status_code: int, stage: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"detail": {"stage": stage, "error": message}},
    )


def _default_solver() -> TurnstileSolver:
    headless = os.getenv("HEADLESS", "false").strip().lower() in {"1", "true", "yes", "on"}
    return TurnstileSolver(headless=headless)


def create_app(
    solver_factory: Callable[[], TurnstileSolver] | None = None,
    api_key: str | None = None,
    queue_timeout: float | None = None,
) -> FastAPI:
    app = FastAPI(
        title="turnstile solver",
        description="Standalone Cloudflare Turnstile solver exposed over HTTP.",
        version=__version__,
    )
    state: dict = {"solver": None}
    lock = asyncio.Lock()

    def expected_key() -> str:
        return _configured_api_key() if api_key is None else api_key.strip()

    def wait_limit() -> float:
        return _queue_timeout() if queue_timeout is None else queue_timeout

    def get_solver():
        if state["solver"] is None:
            factory = solver_factory or _default_solver
            state["solver"] = factory()
        return state["solver"]

    if api_key is None and not _configured_api_key():
        log.warning(
            "SOLVER_API_KEY is not set: /solve is unauthenticated. "
            "Bind the service to 127.0.0.1 (or a private interface) only."
        )

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(request: Request, exc: RequestValidationError):
        # FastAPI's default 422 body is a list; keep the documented envelope.
        return _error(400, "invalid_request", _format_validation_error(exc))

    @app.get("/")
    async def index():
        return {
            "service": "turnstile solver",
            "version": __version__,
            "endpoints": ["/solve", "/healthz", "/version"],
        }

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/version")
    async def version():
        return {"name": "solver", "version": __version__}

    @app.post("/solve")
    async def solve(request: Request, body: SolveRequest):
        key = expected_key()
        if key:
            provided = _provided_api_key(request)
            if not provided or not hmac.compare_digest(
                provided.encode("utf-8"), key.encode("utf-8")
            ):
                return _error(401, "unauthorized", "missing or invalid API key")

        bad_url = _validate_url(body.url)
        if bad_url:
            return _error(400, "invalid_request", bad_url)

        try:
            await asyncio.wait_for(lock.acquire(), timeout=wait_limit())
        except (asyncio.TimeoutError, TimeoutError):
            return _error(
                429,
                "busy",
                "the solver is busy; retry shortly or raise SOLVER_QUEUE_TIMEOUT",
            )
        try:
            try:
                solver = get_solver()
                result = await run_in_threadpool(
                    solver.get_token,
                    body.url,
                    body.sitekey,
                    theme=body.theme.value,
                    size=body.size.value,
                    appearance=body.appearance.value,
                    action=body.action,
                    c_data=body.cData,
                    execute=body.execute,
                    timeout=body.timeout,
                )
            except SolveFailed as exc:
                log.warning("solve failed (%s): %s", exc.stage, exc.message)
                return _error(502, exc.stage, exc.message)
            except (Exception, SystemExit) as exc:  # pragma: no cover - defensive
                log.exception("unexpected solver error")
                return _error(502, "unexpected", str(exc))
        finally:
            lock.release()

        return {
            "token": result.token,
            "userAgent": result.user_agent,
            "elapsed_ms": result.elapsed_ms,
        }

    return app


def _validate_url(url: str) -> str | None:
    """Return an error message for an unusable url, else ``None``."""
    candidate = url.strip()
    if not candidate.lower().startswith(("http://", "https://")):
        return "url must be an absolute http(s) URL"
    host = candidate.split("//", 1)[-1].split("/", 1)[0]
    if not host or host.startswith(":"):
        return "url must include a host"
    return None


def _format_validation_error(exc: RequestValidationError) -> str:
    parts = []
    for err in exc.errors()[:5]:
        location = ".".join(str(item) for item in err.get("loc", ()) if item != "body")
        message = err.get("msg", "invalid value")
        parts.append(f"{location or 'body'}: {message}")
    return "; ".join(parts) or "invalid request body"


app = create_app()
