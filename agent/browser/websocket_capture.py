"""WebSocket frame capture for per-context WebSocket connections.

Attaches to every page in a Playwright persistent context via
``page.on("websocket")`` and records each connection's URL, timing, and
frames.  Binary frame payloads are written to the shared ``bodies/``
directory using SHA-1 naming (same scheme as HTTP bodies) so they can be
referenced by hash and decrypted with external tools.  Text payloads are
stored inline.

Usage::

    ws_capture = WebSocketCapture(artifact_dir / "bodies")
    await ws_capture.attach_to_context(context)
    # ... detonation runs ...
    await ws_capture.drain()
    ws_data = ws_capture.finalize()   # list[dict], empty if no WS activity
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class _WSConnection:
    url: str
    page_url: str
    opened_at: str
    closed_at: str | None = None
    error: str | None = None
    frames: list[dict[str, Any]] = field(default_factory=list)


class WebSocketCapture:
    """Records WebSocket connections and their frames for every page in a context.

    Binary frame payloads are written to ``bodies/<sha1>.bin`` and deduplicated
    by hash.  Text payloads are stored inline in the output.

    The ``finalize()`` return value is a list of connection dicts::

        [
          {
            "url": "wss://...",
            "page_url": "https://...",
            "opened_at": "<ISO8601>",
            "closed_at": "<ISO8601> | null",
            "error": "<message> | null",
            "frames": [
              {
                "direction": "sent" | "received",
                "timestamp": "<ISO8601>",
                "type": "text" | "binary",
                "size": <int>,
                # text frames:
                "payload": "<string>",
                # binary frames:
                "body_sha1": "<hex>",
                "body_file": "bodies/<hex>.bin"
              },
              ...
            ]
          },
          ...
        ]
    """

    def __init__(self, bodies_dir: Path) -> None:
        self._bodies_dir = bodies_dir
        self._connections: list[_WSConnection] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self._saved_hashes: set[str] = set()
        self._context: Any = None
        self._drained = False

    async def attach_to_context(self, context: Any) -> None:
        """Subscribe to page-attach events and attach to all existing pages."""
        self._context = context
        context.on("page", self._on_page)
        for page in context.pages:
            self._register_page(page)

    def _on_page(self, page: Any) -> None:
        self._register_page(page)

    def _register_page(self, page: Any) -> None:
        page.on("websocket", lambda ws: self._on_websocket(ws, page))

    def _on_websocket(self, ws: Any, page: Any) -> None:
        conn = _WSConnection(
            url=ws.url,
            page_url=page.url,
            opened_at=datetime.now(UTC).isoformat(),
        )
        self._connections.append(conn)
        logger.debug("ws_capture: new WebSocket %s on %s", ws.url, page.url)

        ws.on("framereceived", lambda frame: self._schedule(self._record_frame(conn, frame, "received")))
        ws.on("framesent", lambda frame: self._schedule(self._record_frame(conn, frame, "sent")))
        ws.on("close", lambda _ws: self._on_close(conn))
        ws.on("socketerror", lambda error: self._on_error(conn, error))

    async def _record_frame(self, conn: _WSConnection, frame: Any, direction: str) -> None:
        try:
            payload = frame.get("payload", b"") if isinstance(frame, dict) else b""
            is_binary = isinstance(payload, bytes)
            ts = datetime.now(UTC).isoformat()
            entry: dict[str, Any] = {
                "direction": direction,
                "timestamp": ts,
                "type": "binary" if is_binary else "text",
                "size": len(payload),
            }
            if is_binary:
                sha1 = hashlib.sha1(payload).hexdigest()
                if sha1 not in self._saved_hashes:
                    self._saved_hashes.add(sha1)
                    body_path = self._bodies_dir / f"{sha1}.bin"
                    await asyncio.to_thread(body_path.write_bytes, payload)
                entry["body_sha1"] = sha1
                entry["body_file"] = f"bodies/{sha1}.bin"
            else:
                entry["payload"] = payload if isinstance(payload, str) else payload.decode("utf-8", errors="replace")
            conn.frames.append(entry)
        except Exception as exc:
            logger.debug("ws_capture: frame record error (%s): %s", direction, exc)

    def _on_close(self, conn: _WSConnection) -> None:
        conn.closed_at = datetime.now(UTC).isoformat()
        logger.debug("ws_capture: WebSocket closed %s", conn.url)

    def _on_error(self, conn: _WSConnection, error: Any) -> None:
        conn.error = str(error)
        logger.debug("ws_capture: WebSocket error %s: %s", conn.url, error)

    def _schedule(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Detach listeners and flush all in-flight body-write tasks.

        Idempotent — safe to call more than once.
        """
        if self._drained:
            return
        self._drained = True

        if self._context is not None:
            try:
                self._context.remove_listener("page", self._on_page)
            except Exception:
                pass

        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def finalize(self) -> list[dict[str, Any]]:
        """Return the collected WebSocket data as a serialisable list.

        Returns an empty list if no WebSocket connections were observed.
        """
        return [
            {
                "url": c.url,
                "page_url": c.page_url,
                "opened_at": c.opened_at,
                "closed_at": c.closed_at,
                "error": c.error,
                "frames": c.frames,
            }
            for c in self._connections
        ]
