"""Tests for detonator.analysis.har_body_map."""

from __future__ import annotations

import json

from detonator.analysis.har_body_map import HarBodyRef, load_capture_manifest, map_body_files


def _write_har(path, entries):
    """Write a minimal Playwright HAR fixture to *path*."""
    har = {"log": {"version": "1.2", "creator": {"name": "test", "version": "0"}, "entries": entries}}
    path.write_text(json.dumps(har), encoding="utf-8")


# ── map_body_files ──────────────────────────────────────────────────────────


def test_basic_response_mapping(tmp_path):
    har = tmp_path / "har_full.har"
    _write_har(
        har,
        [
            {
                "request": {"url": "https://example.com/static/main.js", "method": "GET"},
                "response": {
                    "content": {"_file": "bodies/abc123def456.bin", "mimeType": "application/javascript"}
                },
            },
            {
                "request": {"url": "https://example.com/style.css", "method": "GET"},
                "response": {"content": {"_file": "bodies/deadbeef0000.bin", "mimeType": "text/css"}},
            },
        ],
    )
    result = map_body_files(har)
    assert result["abc123def456.bin"] == HarBodyRef(
        url="https://example.com/static/main.js",
        direction="response",
        method="GET",
        mime_type="application/javascript",
        source="har_file",
    )
    assert result["deadbeef0000.bin"].direction == "response"


def test_request_body_mapping(tmp_path):
    """POST bodies attached via request.postData._file are indexed as direction='request'."""
    har = tmp_path / "har_full.har"
    _write_har(
        har,
        [
            {
                "request": {
                    "url": "https://telemetry.example.com/beacon",
                    "method": "POST",
                    "postData": {
                        "_file": "bodies/upload0001.dat",
                        "mimeType": "application/x-www-form-urlencoded",
                    },
                },
                "response": {"content": {}},  # no response body
            },
        ],
    )
    result = map_body_files(har)
    assert result == {
        "upload0001.dat": HarBodyRef(
            url="https://telemetry.example.com/beacon",
            direction="request",
            method="POST",
            mime_type="application/x-www-form-urlencoded",
            source="har_file",
        )
    }


def test_both_directions_indexed(tmp_path):
    """A POST with both a request body and a response body indexes both files."""
    har = tmp_path / "har_full.har"
    _write_har(
        har,
        [
            {
                "request": {
                    "url": "https://api.example.com/submit",
                    "method": "POST",
                    "postData": {"_file": "bodies/req.dat"},
                },
                "response": {"content": {"_file": "bodies/resp.json", "mimeType": "application/json"}},
            },
        ],
    )
    result = map_body_files(har)
    assert result["req.dat"].direction == "request"
    assert result["resp.json"].direction == "response"


def test_response_wins_on_collision(tmp_path):
    """If a basename is referenced as both request and response, response wins."""
    har = tmp_path / "har_full.har"
    _write_har(
        har,
        [
            {
                "request": {
                    "url": "https://a.example/post",
                    "method": "POST",
                    "postData": {"_file": "bodies/shared.bin"},
                },
                "response": {"content": {}},
            },
            {
                "request": {"url": "https://b.example/get", "method": "GET"},
                "response": {"content": {"_file": "bodies/shared.bin"}},
            },
        ],
    )
    result = map_body_files(har)
    assert result["shared.bin"].direction == "response"
    assert result["shared.bin"].url == "https://b.example/get"


def test_dedup_keeps_first_url(tmp_path):
    """When two entries share a response body file, the first URL wins."""
    har = tmp_path / "har_full.har"
    _write_har(
        har,
        [
            {
                "request": {"url": "https://example.com/first", "method": "GET"},
                "response": {"content": {"_file": "bodies/shared.bin"}},
            },
            {
                "request": {"url": "https://example.com/second", "method": "GET"},
                "response": {"content": {"_file": "bodies/shared.bin"}},
            },
        ],
    )
    result = map_body_files(har)
    assert result["shared.bin"].url == "https://example.com/first"


def test_entry_without_file_is_skipped(tmp_path):
    """Entries with no _file (redirects, empty bodies) are absent from the result."""
    har = tmp_path / "har_full.har"
    _write_har(
        har,
        [
            {
                "request": {"url": "https://example.com/redirect", "method": "GET"},
                "response": {"content": {}},
            },
            {
                "request": {"url": "https://example.com/page", "method": "GET"},
                "response": {"content": {"_file": "bodies/abc.bin"}},
            },
        ],
    )
    result = map_body_files(har)
    assert "redirect" not in str(result)
    assert list(result.keys()) == ["abc.bin"]


def test_missing_file_returns_empty(tmp_path):
    """A missing HAR file logs a warning and returns an empty dict."""
    assert map_body_files(tmp_path / "nonexistent.har") == {}


def test_malformed_har_returns_empty(tmp_path):
    """A HAR with unexpected structure returns an empty dict rather than raising."""
    har = tmp_path / "bad.har"
    har.write_text('{"not": "a har"}', encoding="utf-8")
    assert map_body_files(har) == {}


# ── load_capture_manifest (JSONL v2 format) ────────────────────────────────


def _write_manifest(run_dir, lines: list[dict]):
    """Write JSONL manifest entries to bodies/manifest.jsonl."""
    bodies = run_dir / "bodies"
    bodies.mkdir(parents=True, exist_ok=True)
    manifest = bodies / "manifest.jsonl"
    manifest.write_text(
        "\n".join(json.dumps(l) for l in lines) + "\n",
        encoding="utf-8",
    )


def _manifest_entry(
    basename,
    url,
    method="GET",
    direction="response",
    outcome="ok",
    mime_type=None,
    **kwargs,
) -> dict:
    return {
        "basename": basename,
        "url": url,
        "method": method,
        "direction": direction,
        "capture_outcome": outcome,
        "mime_type": mime_type,
        "size_actual": 100,
        "size_truncated": None,
        "captured_at": "2026-04-23T10:00:00+00:00",
        **kwargs,
    }


def test_load_capture_manifest_basic(tmp_path):
    _write_manifest(
        tmp_path,
        [_manifest_entry(
            "abc123.html",
            "https://storage.googleapis.com/fruiteex/fruiteex.html",
            mime_type="text/html",
        )],
    )
    result = load_capture_manifest(tmp_path)
    assert result["abc123.html"] == HarBodyRef(
        url="https://storage.googleapis.com/fruiteex/fruiteex.html",
        direction="response",
        method="GET",
        mime_type="text/html",
        source="capture_manifest",
    )


def test_load_capture_manifest_defaults_direction_and_method(tmp_path):
    bodies = tmp_path / "bodies"
    bodies.mkdir()
    (bodies / "manifest.jsonl").write_text(
        json.dumps({
            "basename": "x.bin",
            "url": "https://example.com/x",
            "capture_outcome": "ok",
            "size_actual": 10,
        }) + "\n",
        encoding="utf-8",
    )
    result = load_capture_manifest(tmp_path)
    assert result["x.bin"].direction == "response"
    assert result["x.bin"].method == "GET"
    assert result["x.bin"].source == "capture_manifest"


def test_load_capture_manifest_missing_is_empty(tmp_path):
    assert load_capture_manifest(tmp_path) == {}


def test_load_capture_manifest_malformed_jsonl_skips_bad_line(tmp_path):
    """A partial/corrupted last line is skipped; valid preceding lines are loaded."""
    bodies = tmp_path / "bodies"
    bodies.mkdir()
    good = json.dumps(_manifest_entry("good.html", "https://example.com/ok"))
    (bodies / "manifest.jsonl").write_text(
        good + "\n{this is not json",
        encoding="utf-8",
    )
    result = load_capture_manifest(tmp_path)
    assert list(result.keys()) == ["good.html"]


def test_load_capture_manifest_multi_source_first_wins(tmp_path):
    """Two entries sharing a basename: first URL wins (CDN dedup scenario)."""
    _write_manifest(
        tmp_path,
        [
            _manifest_entry("sha.js", "https://cdn-a.example/lib.js"),
            _manifest_entry("sha.js", "https://cdn-b.example/lib.js"),
        ],
    )
    result = load_capture_manifest(tmp_path)
    assert result["sha.js"].url == "https://cdn-a.example/lib.js"


def test_load_capture_manifest_request_direction(tmp_path):
    """Entries with direction='request' are indexed correctly."""
    _write_manifest(
        tmp_path,
        [_manifest_entry(
            "beacon.dat",
            "https://telemetry.example.com/ping",
            method="POST",
            direction="request",
            mime_type="application/x-www-form-urlencoded",
        )],
    )
    result = load_capture_manifest(tmp_path)
    assert result["beacon.dat"].direction == "request"
    assert result["beacon.dat"].method == "POST"


def test_load_capture_manifest_skips_non_body_outcomes(tmp_path):
    """Entries with outcome=redirect/empty/error have no body file; skip them."""
    _write_manifest(
        tmp_path,
        [
            _manifest_entry("good.html", "https://example.com/page", outcome="ok"),
            _manifest_entry(None, "https://example.com/redir", outcome="redirect"),
            _manifest_entry(None, "https://example.com/empty", outcome="empty"),
            _manifest_entry(None, "https://example.com/err", outcome="error"),
        ],
    )
    result = load_capture_manifest(tmp_path)
    assert list(result.keys()) == ["good.html"]


def test_load_capture_manifest_skips_entries_missing_basename(tmp_path):
    _write_manifest(
        tmp_path,
        [
            _manifest_entry("good.html", "https://example.com/ok"),
            # No basename field
            {"url": "https://example.com/x", "capture_outcome": "ok", "size_actual": 10},
        ],
    )
    result = load_capture_manifest(tmp_path)
    assert list(result.keys()) == ["good.html"]


def test_load_capture_manifest_truncated_outcome_indexed(tmp_path):
    """Truncated bodies still have a file on disk and should be indexed."""
    _write_manifest(
        tmp_path,
        [_manifest_entry(
            "big.bin",
            "https://example.com/large",
            outcome="truncated",
            mime_type="application/octet-stream",
        )],
    )
    result = load_capture_manifest(tmp_path)
    assert "big.bin" in result
    assert result["big.bin"].source == "capture_manifest"


# ── Legacy extra.json fallback ─────────────────────────────────────────────


def _write_extra(run_dir, payload):
    bodies = run_dir / "bodies"
    bodies.mkdir(parents=True, exist_ok=True)
    (bodies / "extra.json").write_text(json.dumps(payload), encoding="utf-8")


def test_load_capture_manifest_falls_back_to_extra_json(tmp_path):
    """Older runs that have extra.json but not manifest.jsonl are still readable."""
    _write_extra(
        tmp_path,
        {
            "abc123.html": {
                "url": "https://storage.googleapis.com/fruiteex/fruiteex.html",
                "method": "GET",
                "mime_type": "text/html",
                "direction": "response",
            }
        },
    )
    result = load_capture_manifest(tmp_path)
    assert result["abc123.html"] == HarBodyRef(
        url="https://storage.googleapis.com/fruiteex/fruiteex.html",
        direction="response",
        method="GET",
        mime_type="text/html",
        source="capture_manifest",
    )


def test_load_capture_manifest_prefers_jsonl_over_extra_json(tmp_path):
    """manifest.jsonl takes precedence when both files exist."""
    _write_manifest(tmp_path, [_manifest_entry("new.html", "https://example.com/new")])
    _write_extra(tmp_path, {"old.html": {"url": "https://example.com/old"}})
    result = load_capture_manifest(tmp_path)
    assert "new.html" in result
    assert "old.html" not in result


def test_load_capture_manifest_extra_json_malformed_is_empty(tmp_path):
    bodies = tmp_path / "bodies"
    bodies.mkdir()
    (bodies / "extra.json").write_text("not json", encoding="utf-8")
    assert load_capture_manifest(tmp_path) == {}


def test_load_capture_manifest_extra_json_skips_entries_missing_url(tmp_path):
    _write_extra(
        tmp_path,
        {
            "good.html": {"url": "https://example.com/ok"},
            "bad.html": {"method": "GET"},  # no url
            "worse.html": "not a dict",
        },
    )
    result = load_capture_manifest(tmp_path)
    assert list(result.keys()) == ["good.html"]
