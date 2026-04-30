"""ASGI entrypoint for running the Flask app with Uvicorn."""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

from asgiref.wsgi import WsgiToAsgiInstance

from app import app as flask_app


def resolve_wsgi_threadpool_workers(default=8):
    """Resolve how many concurrent WSGI requests Uvicorn may run."""
    try:
        workers = int(os.getenv("ASGI_WSGI_THREADPOOL_WORKERS", default))
    except (TypeError, ValueError):
        workers = default
    return min(max(workers, 1), 64)


_wsgi_executor = ThreadPoolExecutor(
    max_workers=resolve_wsgi_threadpool_workers(),
    thread_name_prefix="floppy-wsgi",
)


class ConcurrentWsgiToAsgiInstance(WsgiToAsgiInstance):
    """Run each Flask request in the shared executor instead of one serialized thread."""

    async def run_wsgi_app(self, body):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(_wsgi_executor, self._run_wsgi_app_sync, body)

    def _run_wsgi_app_sync(self, body):
        WsgiToAsgiInstance.run_wsgi_app.__wrapped__(self, body)


class ConcurrentWsgiToAsgi:
    """Small WSGI-to-ASGI adapter that allows concurrent sync Flask requests."""

    def __init__(self, wsgi_application, duplicate_header_limit=100):
        self.wsgi_application = wsgi_application
        self.duplicate_header_limit = duplicate_header_limit

    async def __call__(self, scope, receive, send):
        await ConcurrentWsgiToAsgiInstance(
            self.wsgi_application,
            self.duplicate_header_limit,
        )(scope, receive, send)


app = ConcurrentWsgiToAsgi(flask_app)
