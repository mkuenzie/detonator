"""Authoritative network body capture layer for detonation runs.

Playwright's HAR writer silently drops bodies for main-frame document
responses and some POST responses.  This layer captures every fetchable
response (and outgoing request) body independently, content-addressed to
``bodies/<sha>.<ext>``, and writes a JSONL manifest that downstream
consumers merge with the HAR to fill the gaps.

The manifest is append-only JSONL (one JSON object per capture event),
written per response so a mid-run crash leaves everything up to the last
complete line intact.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

CaptureOutcome = Literal["ok", "truncated", "empty", "redirect", "aborted", "disposed", "error"]

_MIME_EXT: dict[str, str] = {
    "text/html": ".html",
    "application/xhtml+xml": ".html",
    "application/javascript": ".js",
    "text/javascript": ".js",
    "application/json": ".json",
    "text/css": ".css",
    "text/plain": ".txt",
    "text/xml": ".xml",
    "application/xml": ".xml",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
    "image/x-icon": ".ico",
    "font/woff": ".woff",
    "font/woff2": ".woff2",
    "application/wasm": ".wasm",
}

# Response headers captured verbatim (not PII).  set-cookie presence is
# recorded as a boolean flag; the value is never stored.
_RESP_HEADERS = frozenset({"content-type", "content-length", "content-encoding", "server", "referer"})
_REQ_HEADERS = frozenset({"content-type", "content-length", "referer"})


def _ext_for_mime(mime_type: str | None) -> str:
    if not mime_type:
        return ".bin"
    base = mime_type.split(";", 1)[0].strip().lower()
    return _MIME_EXT.get(base, ".bin")


@dataclass
class CaptureStats:
    """Per-run network capture summary included in DetonationResult.meta."""

    captured: int = 0
    truncated: int = 0
    empty: int = 0
    redirect: int = 0
    aborted: int = 0
    disposed: int = 0
    error: int = 0

    def _bump(self, outcome: CaptureOutcome) -> None:
        match outcome:
            case "ok":
                self.captured += 1
            case "truncated":
                self.captured += 1
                self.truncated += 1
            case "empty":
                self.empty += 1
            case "redirect":
                self.redirect += 1
            case "aborted":
                self.aborted += 1
            case "disposed":
                self.disposed += 1
            case _:
                self.error += 1

    def as_dict(self) -> dict[str, int]:
        return {
            "captured": self.captured,
            "truncated": self.truncated,
            "empty": self.empty,
            "redirect": self.redirect,
            "aborted": self.aborted,
            "disposed": self.disposed,
            "error": self.error,
        }


class NetworkCapture:
    """Authoritative per-run network body capture layer.

    Attach to a browser context via ``attach(context)``.  After detonation
    completes, call ``await drain()`` (before context.close()) then
    ``finalize()`` to get capture statistics.

    The JSONL manifest at ``bodies/manifest.jsonl`` contains one entry per
    capture event and is the primary input for ``load_capture_manifest``.
    """

    def __init__(
        self,
        bodies_dir: Path,
        *,
        max_body_bytes: int = 10 * 1024 * 1024,
        max_concurrent: int = 16,
    ) -> None:
        self._bodies_dir = bodies_dir
        self._max_body_bytes = max_body_bytes
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._tasks: set[asyncio.Task[None]] = set()
        self._saved_hashes: set[str] = set()
        self._stats = CaptureStats()
        self._manifest_path = bodies_dir / "manifest.jsonl"
        self._manifest_lock = asyncio.Lock()
        self._context: Any = None
        self._drained = False

    # ── Public interface ──────────────────────────────────────────

    def attach(self, context: Any) -> None:
        """Subscribe to browser context events to begin capture."""
        self._bodies_dir.mkdir(parents=True, exist_ok=True)
        self._context = context
        context.on("response", self._schedule_response)
        context.on("request", self._schedule_request)

    async def drain(self) -> None:
        """Detach event handlers and await all in-flight captures.

        Must be called before context.close() to avoid losing tail-end events.
        Idempotent — safe to call more than once.
        """
        if self._drained:
            return
        self._drained = True
        if self._context is not None:
            try:
                self._context.remove_listener("response", self._schedule_response)
            except Exception:
                pass
            try:
                self._context.remove_listener("request", self._schedule_request)
            except Exception:
                pass
        # Loop until empty: events fired between drain start and gather() complete
        # schedule new tasks that would otherwise be abandoned.
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def finalize(self) -> CaptureStats:
        """Return capture statistics after drain() has completed."""
        return self._stats

    # ── Scheduling ────────────────────────────────────────────────

    def _schedule_response(self, response: Any) -> None:
        task = asyncio.create_task(self._capture_response(response))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _schedule_request(self, request: Any) -> None:
        task = asyncio.create_task(self._capture_request(request))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ── Response capture ──────────────────────────────────────────

    async def _capture_response(self, response: Any) -> None:
        try:
            async with self._semaphore:
                await self._do_capture_response(response)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("capture_response unexpected error: %s", exc)
            self._stats._bump("error")

    async def _do_capture_response(self, response: Any) -> None:
        status = 0
        url = ""
        method = "GET"
        try:
            status = response.status
            url = response.url
            method = (response.request.method or "GET").upper() if response.request else "GET"
        except Exception:
            pass

        # Redirects carry no body worth capturing.
        if 300 <= status < 400:
            await self._append_manifest(
                self._base_entry("response", url, method, status, None, 0, None, "redirect")
            )
            self._stats._bump("redirect")
            return

        try:
            body = await response.body()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            msg = str(exc).lower()
            if "closed" in msg or "disposed" in msg or "target page" in msg:
                outcome: CaptureOutcome = "disposed"
            elif "aborted" in msg:
                outcome = "aborted"
            else:
                outcome = "error"
                logger.debug("response.body() failed for %s: %s", url, exc)
            await self._append_manifest(
                self._base_entry("response", url, method, status, None, 0, None, outcome)
            )
            self._stats._bump(outcome)
            return

        if not body:
            await self._append_manifest(
                self._base_entry("response", url, method, status, None, 0, None, "empty")
            )
            self._stats._bump("empty")
            return

        mime_type: str | None = None
        try:
            mime_type = (response.headers or {}).get("content-type")
        except Exception:
            pass

        size_actual = len(body)
        size_truncated: int | None = None
        outcome = "ok"
        if size_actual > self._max_body_bytes:
            body = body[: self._max_body_bytes]
            size_truncated = self._max_body_bytes
            outcome = "truncated"

        sha = hashlib.sha256(body).hexdigest()
        basename = f"{sha}{_ext_for_mime(mime_type)}"

        if sha not in self._saved_hashes:
            try:
                await asyncio.to_thread((self._bodies_dir / basename).write_bytes, body)
            except Exception as exc:
                logger.debug("body write failed %s: %s", basename, exc)
                self._stats._bump("error")
                return
            self._saved_hashes.add(sha)

        entry = self._base_entry(
            "response", url, method, status, mime_type, size_actual, size_truncated, outcome,
            basename=basename,
        )
        await self._enrich_response(entry, response)
        await self._append_manifest(entry)
        self._stats._bump(outcome)

    # ── Request capture ───────────────────────────────────────────

    async def _capture_request(self, request: Any) -> None:
        try:
            async with self._semaphore:
                await self._do_capture_request(request)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("capture_request unexpected error: %s", exc)

    async def _do_capture_request(self, request: Any) -> None:
        try:
            body = request.post_data_buffer
        except Exception:
            return
        if not body:
            return

        url = ""
        method = "POST"
        try:
            url = request.url
            method = (request.method or "POST").upper()
        except Exception:
            pass

        mime_type: str | None = None
        try:
            mime_type = (request.headers or {}).get("content-type")
        except Exception:
            pass

        size_actual = len(body)
        size_truncated: int | None = None
        outcome: CaptureOutcome = "ok"
        if size_actual > self._max_body_bytes:
            body = body[: self._max_body_bytes]
            size_truncated = self._max_body_bytes
            outcome = "truncated"

        sha = hashlib.sha256(body).hexdigest()
        basename = f"{sha}{_ext_for_mime(mime_type)}"

        if sha not in self._saved_hashes:
            try:
                await asyncio.to_thread((self._bodies_dir / basename).write_bytes, body)
            except Exception as exc:
                logger.debug("request body write failed %s: %s", basename, exc)
                return
            self._saved_hashes.add(sha)

        entry = self._base_entry(
            "request", url, method, 0, mime_type, size_actual, size_truncated, outcome,
            basename=basename,
        )
        await self._enrich_request(entry, request)
        await self._append_manifest(entry)
        self._stats._bump(outcome)

    # ── Entry construction ────────────────────────────────────────

    def _base_entry(
        self,
        direction: Literal["request", "response"],
        url: str,
        method: str,
        status: int,
        mime_type: str | None,
        size_actual: int,
        size_truncated: int | None,
        outcome: CaptureOutcome,
        *,
        basename: str | None = None,
    ) -> dict[str, Any]:
        return {
            "basename": basename,
            "url": url,
            "method": method,
            "direction": direction,
            "status": status,
            "mime_type": mime_type,
            "size_actual": size_actual,
            "size_truncated": size_truncated,
            "capture_outcome": outcome,
            "captured_at": datetime.now(UTC).isoformat(),
        }

    async def _enrich_response(self, entry: dict[str, Any], response: Any) -> None:
        """Best-effort: add resource_type, frame_url, remote_address, request_id, headers."""
        try:
            entry["resource_type"] = response.request.resource_type
        except Exception:
            pass

        try:
            frame = response.frame
            entry["frame_url"] = frame.url if frame else None
        except Exception:
            pass

        try:
            sa = await response.server_addr()
            if sa:
                entry["remote_address"] = f"{sa['ipAddress']}:{sa['port']}"
        except Exception:
            pass

        try:
            entry["request_id"] = str(id(response.request))
        except Exception:
            pass

        try:
            headers = response.headers or {}
            hs: dict[str, Any] = {k: headers[k] for k in _RESP_HEADERS if k in headers}
            hs["set_cookie_present"] = "set-cookie" in headers
            entry["response_headers"] = hs or None
        except Exception:
            pass

    async def _enrich_request(self, entry: dict[str, Any], request: Any) -> None:
        """Best-effort: add resource_type, frame_url, request_id, headers."""
        try:
            entry["resource_type"] = request.resource_type
        except Exception:
            pass

        try:
            frame = request.frame
            entry["frame_url"] = frame.url if frame else None
        except Exception:
            pass

        try:
            entry["request_id"] = str(id(request))
        except Exception:
            pass

        try:
            headers = request.headers or {}
            entry["request_headers"] = {k: headers[k] for k in _REQ_HEADERS if k in headers} or None
        except Exception:
            pass

    # ── Manifest I/O ──────────────────────────────────────────────

    async def _append_manifest(self, entry: dict[str, Any]) -> None:
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        async with self._manifest_lock:
            await asyncio.to_thread(self._write_line, line)

    def _write_line(self, line: str) -> None:
        with self._manifest_path.open("a", encoding="utf-8") as f:
            f.write(line)
