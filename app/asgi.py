"""ASGI entrypoint for running the Flask app with Uvicorn."""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from tempfile import SpooledTemporaryFile

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

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            raise ValueError("WSGI wrapper received a non-HTTP scope")
        self.scope = scope
        self.async_send = send
        with SpooledTemporaryFile(max_size=65536) as body:
            while True:
                message = await receive()
                if message["type"] != "http.request":
                    raise ValueError("WSGI wrapper received a non-HTTP-request message")
                body.write(message.get("body", b""))
                if not message.get("more_body"):
                    break
            body.seek(0)
            await self.run_wsgi_app(body)

    async def run_wsgi_app(self, body):
        loop = asyncio.get_running_loop()
        response_start, response_chunks = await loop.run_in_executor(
            _wsgi_executor,
            self._run_wsgi_app_sync,
            body,
        )
        await self.async_send(response_start)
        for chunk in response_chunks:
            await self.async_send(
                {"type": "http.response.body", "body": chunk, "more_body": True}
            )
        await self.async_send({"type": "http.response.body"})

    def _run_wsgi_app_sync(self, body):
        """Collect a WSGI response without blocking a worker on ASGI sends.

        Calling ``AsyncToSync(send)`` from every WSGI worker can exhaust the
        executor under sustained concurrency. The event-loop thread now owns
        all ASGI sends; workers only execute Flask and return bounded chunks.
        """
        try:
            environ = self.build_environ(self.scope, body)
        except ValueError:
            return (
                {
                    "type": "http.response.start",
                    "status": 400,
                    "headers": [(b"content-type", b"text/plain")],
                },
                [b"Bad Request: too many duplicate headers"],
            )

        response_chunks = []
        bytes_sent = 0
        iterable = self.wsgi_application(environ, self.start_response)
        try:
            for output in iterable:
                if self.response_content_length is not None:
                    bytes_allowed = self.response_content_length - bytes_sent
                    if len(output) > bytes_allowed:
                        output = output[:bytes_allowed]
                if output:
                    response_chunks.append(output)
                    bytes_sent += len(output)
                if (
                    self.response_content_length is not None
                    and bytes_sent >= self.response_content_length
                ):
                    break
        finally:
            close = getattr(iterable, "close", None)
            if close:
                close()

        if not hasattr(self, "response_start"):
            raise RuntimeError("WSGI application did not call start_response")
        return self.response_start, response_chunks


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
