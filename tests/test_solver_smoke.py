"""Opt-in smoke test: needs Chrome + a network path to the target site.

Excluded by default (``addopts = -m 'not smoke'``); run it explicitly with the
target site configured through the environment::

    SOLVER_SMOKE_URL=https://example.com/login \
    SOLVER_SMOKE_SITEKEY=0x... pytest -m smoke tests/test_solver_smoke.py
"""

import os

import pytest

from solver.browser import TurnstileSolver

SITEURL = os.getenv("SOLVER_SMOKE_URL")
SITEKEY = os.getenv("SOLVER_SMOKE_SITEKEY")


@pytest.mark.skipif(
    not (SITEURL and SITEKEY),
    reason="set SOLVER_SMOKE_URL and SOLVER_SMOKE_SITEKEY to run the smoke test",
)
@pytest.mark.smoke
def test_real_turnstile_token():
    solver = TurnstileSolver(headless=False, timeout=90)
    try:
        result = solver.get_token(SITEURL, SITEKEY)
    finally:
        solver.close()
    assert len(result.token) > 20
    assert "Chrome" in result.user_agent