"""Entry point for the TradingAgents web dashboard.

The FastAPI app itself lives in ``web/routes.py`` (job model, SSE progress
streaming, static-file mount); this module just wires up logging and the
uvicorn runner so ``python web_server.py`` keeps working exactly as before.

Binds to localhost by default — set ``TRADINGAGENTS_WEB_HOST=0.0.0.0`` to
expose it on the LAN, and ``TRADINGAGENTS_WEB_RELOAD=true`` to enable
uvicorn's autoreload for local development.
"""

import logging
import os

logging.basicConfig(level=logging.INFO)

from web.routes import app  # noqa: E402

__all__ = ["app"]

if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("TRADINGAGENTS_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("TRADINGAGENTS_WEB_PORT", "8000"))
    reload = os.environ.get("TRADINGAGENTS_WEB_RELOAD", "false").strip().lower() in (
        "true", "1", "yes", "on",
    )
    uvicorn.run("web_server:app", host=host, port=port, reload=reload)
