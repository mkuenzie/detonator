"""Tests for detonator.analysis.har_body_map."""

from __future__ import annotations

import json

from detonator.analysis.har_body_map import HarBodyRef, map_body_files


def _write_har(path, entries):
    """Write a minimal Playwright HAR fixture to *path*."""
    har = {"log": {"version": "1.2", "creator": {"name": "test", "version": "0"}, "entries": entries}}
    path.write_text(json.dumps(har), encoding="utf-8")


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
