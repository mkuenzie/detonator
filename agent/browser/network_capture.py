"""Network body capture layer for detonation runs.

Captures every fetchable response and outgoing request body to
``bodies/<sha1>.<ext>`` and writes a JSONL manifest (one JSON object
per capture event) to ``bodies/manifest.jsonl``. The manifest carries
URL / method / direction / outcome metadata that the host's ingestion
pipeline attaches to each body file.

Two sinks feed this module:
- ``_do_capture_request`` pulls request ``post_data_buffer`` synchronously
  off Playwright's ``context.on("request")`` event.
- ``CDPResponseTap`` (see ``cdp_response_tap.py``) pulls response bodies
  via ``Network.getResponseBody`` inside the ``loadingFinished`` handler,
  which is the only window where Chromium guarantees the body is still
  resident. The tap calls ``record_response`` / ``record_failure`` on
  this module.

The manifest is append-only — one JSON object per line, flushed per
event — so a mid-run crash leaves everything up to the last complete
line intact.

# Why SHA-1 (not SHA-256) for the body filenames

Playwright's HAR writer (``record_har_content="attach"``) runs in parallel
and writes its own copy of bodies, naming them by SHA-1. By using SHA-1
here too, both capture paths produce the *same* basename for the same
body bytes — so they share the file on disk, the host's basename-keyed
merge in ``Runner._collect_artifacts`` strict-unions them without
duplication, and we get exactly one artifact row per unique body.

SHA-1's cryptographic weakness is irrelevant: we're content-addressing
network captures inside a sandbox we control, not signing them. Git uses
SHA-1 for the same job. The host's CAS still uses SHA-256 internally
(computed at adoption time) — that's a separate concern.

# Why this layer exists at all (it isn't redundant with HAR)

Empirically, both capture paths are load-bearing:
- HAR attach has a documented race where Chromium disposes main-frame
  document bodies before Playwright's writer reads them, dropping the
  body silently. The CDP tap closes this race by reading inside
  ``loadingFinished``.
- Conversely, ``CDPResponseTap``'s per-page session does not see Network
  events from cross-origin iframe targets (Chromium site isolation puts
  OOPIFs in a separate renderer with its own CDP target). HAR catches
  those because Playwright owns the underlying CDP and is wired into
  every target.

Each path covers the other's gap. There is no single-source-of-truth
simplification — observed empirically across multiple runs (see
``scripts/capture_diff.py``). The "duplication" you see between
paths is the price of complete coverage; SHA-1 alignment is what makes
that duplication free at the storage and ingest layers.
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

CaptureOutcome = Literal["ok", "truncated", "empty", "redirect", "aborted", "disposed", "failed", "error"]

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
    failed: int = 0
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
            case "failed":
                self.failed += 1
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
            "failed": self.failed,
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

    # ── Sink interface (used by CDPResponseTap) ───────────────────

    async def record_response(
        self,
        *,
        request_id: str,
        url: str,
        method: str,
        status: int,
        mime_type: str | None,
        resource_type: str | None,
        frame_url: str | None,
        remote_address: str | None,
        response_headers: dict[str, Any] | None,
        body: bytes | None,
        outcome: CaptureOutcome,
    ) -> None:
        """Write a response capture entry to manifest (and body file if applicable)."""
        if not body:
            entry = self._base_entry(
                "response", url, method, status, mime_type, 0, None,
                "empty" if outcome == "ok" else outcome,
            )
            entry["request_id"] = request_id
            if resource_type is not None:
                entry["resource_type"] = resource_type
            if frame_url is not None:
                entry["frame_url"] = frame_url
            if remote_address is not None:
                entry["remote_address"] = remote_address
            if response_headers is not None:
                entry["response_headers"] = response_headers
            actual_outcome: CaptureOutcome = "empty" if outcome == "ok" else outcome
            await self._append_manifest(entry)
            self._stats._bump(actual_outcome)
            return

        size_actual = len(body)
        size_truncated: int | None = None
        actual_outcome = outcome
        if size_actual > self._max_body_bytes:
            body = body[: self._max_body_bytes]
            size_truncated = self._max_body_bytes
            actual_outcome = "truncated"

        sha = hashlib.sha1(body).hexdigest()
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
            "response", url, method, status, mime_type, size_actual, size_truncated,
            actual_outcome, basename=basename,
        )
        entry["request_id"] = request_id
        if resource_type is not None:
            entry["resource_type"] = resource_type
        if frame_url is not None:
            entry["frame_url"] = frame_url
        if remote_address is not None:
            entry["remote_address"] = remote_address
        if response_headers is not None:
            entry["response_headers"] = response_headers
        await self._append_manifest(entry)
        self._stats._bump(actual_outcome)

    async def record_failure(
        self,
        *,
        request_id: str,
        url: str,
        method: str,
        outcome: CaptureOutcome,
        reason: str | None = None,
    ) -> None:
        """Write a failure manifest entry (no body file)."""
        entry = self._base_entry("response", url, method, 0, None, 0, None, outcome)
        entry["request_id"] = request_id
        if reason:
            entry["failure_reason"] = reason
        await self._append_manifest(entry)
        self._stats._bump(outcome)

    # ── Scheduling ────────────────────────────────────────────────

    def _schedule_request(self, request: Any) -> None:
        task = asyncio.create_task(self._capture_request(request))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

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

        sha = hashlib.sha1(body).hexdigest()
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
            entry["request_headers"] = {k.lower(): v for k, v in headers.items()} or None
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
