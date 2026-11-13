#!/usr/bin/env python3
"""Solve one Turnstile widget and print the token.

Standalone client for the solver HTTP API - no local Python imports needed,
just ``requests`` (and no browser on this machine).

Example
-------
    python examples/solve_once.py \
        --url "https://example.com/login" \
        --sitekey "0x4AAAA..."

The API key is taken from the environment (or ``--api-key``):

    export SOLVER_API_KEY="..."
    export SOLVER_URL="http://127.0.0.1:8000"     # default
"""

from __future__ import annotations

import argparse
import os
import sys

import requests

DEFAULT_URL = os.getenv("SOLVER_URL", "http://127.0.0.1:8000")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Solve a single Turnstile widget via the HTTP API.")
    parser.add_argument("--url", required=True, help="absolute http(s) URL of the page that renders the widget")
    parser.add_argument("--sitekey", required=True, help="Turnstile sitekey used on that page")
    parser.add_argument("--action", default=None, help="optional Turnstile action (<= 32 chars)")
    parser.add_argument("--cdata", default=None, help="optional Turnstile cData (<= 255 chars)")
    parser.add_argument("--theme", default="dark", choices=["auto", "dark", "light"])
    parser.add_argument("--size", default="normal", choices=["normal", "compact", "flexible"])
    parser.add_argument("--appearance", default="always",
                        choices=["always", "execute", "interaction-only"])
    parser.add_argument("--execute", action="store_true",
                        help="call turnstile.execute() instead of waiting for the checkbox")
    parser.add_argument("--timeout", type=float, default=60.0, help="solve budget in seconds (1..300)")
    parser.add_argument("--api-key", default=os.getenv("SOLVER_API_KEY"),
                        help="API key (defaults to the SOLVER_API_KEY environment variable)")
    parser.add_argument("--solver-url", default=DEFAULT_URL, help="base URL of the solver service")
    parser.add_argument("--request-timeout", type=float, default=None,
                        help="HTTP timeout (defaults to the solve budget + 30s)")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    payload = {
        "url": args.url,
        "sitekey": args.sitekey,
        "theme": args.theme,
        "size": args.size,
        "appearance": args.appearance,
        "execute": args.execute,
        "timeout": args.timeout,
    }
    if args.action:
        payload["action"] = args.action
    if args.cdata:
        payload["cData"] = args.cdata

    headers = {}
    if args.api_key:
        headers["X-Solver-Key"] = args.api_key

    http_timeout = args.request_timeout or (args.timeout + 30.0)
    try:
        response = requests.post(
            f"{args.solver_url.rstrip('/')}/solve",
            json=payload,
            headers=headers,
            timeout=http_timeout,
        )
    except requests.RequestException as exc:
        print(f"transport error: {exc}", file=sys.stderr)
        return 1

    body = None
    try:
        body = response.json()
    except ValueError:
        pass

    if response.status_code != 200:
        stage = ""
        message = response.text[:400]
        if isinstance(body, dict):
            detail = body.get("detail")
            if isinstance(detail, dict):
                stage = str(detail.get("stage") or "")
                message = str(detail.get("error") or message)
        label = f" [{stage}]" if stage else ""
        print(f"solver error {response.status_code}{label}: {message}", file=sys.stderr)
        if response.status_code == 429:
            print("the solver is busy - retry shortly or raise SOLVER_QUEUE_TIMEOUT", file=sys.stderr)
        return 2

    if not isinstance(body, dict) or not body.get("token"):
        print("solver returned no token", file=sys.stderr)
        return 3

    print(body["token"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())