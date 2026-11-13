"""``python -m solver`` - run the HTTP service with uvicorn.

Environment variables:

* ``SOLVER_HOST`` (default ``127.0.0.1``) - bind address.  Keep it on loopback
  unless the service sits behind a private network / reverse proxy.
* ``SOLVER_PORT`` (default ``8000``).
* ``LOG_LEVEL`` (default ``info``) - uvicorn log level.
"""

from __future__ import annotations

import os


def main() -> None:
    import uvicorn

    host = os.getenv("SOLVER_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = int(os.getenv("SOLVER_PORT", "8000"))
    log_level = os.getenv("LOG_LEVEL", "info").strip().lower() or "info"
    uvicorn.run("solver.app:app", host=host, port=port, log_level=log_level)


if __name__ == "__main__":
    main()
