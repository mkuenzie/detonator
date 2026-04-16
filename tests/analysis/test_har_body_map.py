"""Tests for detonator.analysis.har_body_map."""

from __future__ import annotations

import json

import pytest

from detonator.analysis.har_body_map import map_body_files_to_urls


def _write_har(path, entries):
    """Write a minimal Playwright HAR fixture to *path*."""
    har = {"log": {"version": "1.2", "creator": {"name": "test", "version": "0"}, "entries": entries}}
    path.write_text(json.dumps(har), encoding="utf-8")


def test_basic_mapping(tmp_path):
    har = tmp_path / "har_full.har"
    _write_har(
        har,
        [
            {
                "request": {"url": "https://example.com/static/main.js"},
                "response": {"content": {"_file": "bodies/abc123def456.bin"}},
            },
            {
                "request": {"url": "https://example.com/style.css"},
                "response": {"content": {"_file": "bodies/deadbeef0000.bin"}},
            },
        ],
    )
    result = map_body_files_to_urls(har)
    assert result == {
        "abc123def456.bin": "https://example.com/static/main.js",
        "deadbeef0000.bin": "https://example.com/style.css",
    }


def test_dedup_keeps_first_url(tmp_path):
    """When two entries share a body file, the first URL wins."""
    har = tmp_path / "har_full.har"
    _write_har(
        har,
        [
            {
                "request": {"url": "https://example.com/first"},
                "response": {"content": {"_file": "bodies/shared.bin"}},
            },
            {
                "request": {"url": "https://example.com/second"},
                "response": {"content": {"_file": "bodies/shared.bin"}},
            },
        ],
    )
    result = map_body_files_to_urls(har)
    assert result == {"shared.bin": "https://example.com/first"}


def test_entry_without_file_is_skipped(tmp_path):
    """Entries with no _file (redirects, empty bodies) are absent from the result."""
    har = tmp_path / "har_full.har"
    _write_har(
        har,
        [
            {
                "request": {"url": "https://example.com/redirect"},
                "response": {"content": {}},  # no _file
            },
            {
                "request": {"url": "https://example.com/page"},
                "response": {"content": {"_file": "bodies/abc.bin"}},
            },
        ],
    )
    result = map_body_files_to_urls(har)
    assert "redirect" not in str(result)
    assert result == {"abc.bin": "https://example.com/page"}


def test_missing_file_returns_empty(tmp_path):
    """A missing HAR file logs a warning and returns an empty dict."""
    result = map_body_files_to_urls(tmp_path / "nonexistent.har")
    assert result == {}


def test_malformed_har_returns_empty(tmp_path):
    """A HAR with unexpected structure returns an empty dict rather than raising."""
    har = tmp_path / "bad.har"
    har.write_text('{"not": "a har"}', encoding="utf-8")
    result = map_body_files_to_urls(har)
    assert result == {}
