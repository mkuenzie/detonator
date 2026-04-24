from __future__ import annotations

import json
import pytest
from pathlib import Path

from detonator.ui.routes import _index_har_started


def _write_har(tmp_path: Path, entries: list[dict]) -> Path:
    har = {"log": {"entries": entries}}
    p = tmp_path / "test.har"
    p.write_text(json.dumps(har))
    return p


def test_index_har_started_basic(tmp_path):
    entries = [
        {"request": {"url": "https://example.com/"}, "startedDateTime": "2024-01-01T00:00:01Z"},
        {"request": {"url": "https://example.com/js/app.js"}, "startedDateTime": "2024-01-01T00:00:02Z"},
    ]
    result = _index_har_started(_write_har(tmp_path, entries))
    assert result["https://example.com/"] == "2024-01-01T00:00:01Z"
    assert result["https://example.com/js/app.js"] == "2024-01-01T00:00:02Z"


def test_index_har_started_keeps_earliest_on_dupe(tmp_path):
    entries = [
        {"request": {"url": "https://example.com/"}, "startedDateTime": "2024-01-01T00:00:01Z"},
        {"request": {"url": "https://example.com/"}, "startedDateTime": "2024-01-01T00:00:05Z"},
    ]
    result = _index_har_started(_write_har(tmp_path, entries))
    assert result["https://example.com/"] == "2024-01-01T00:00:01Z"


def test_index_har_started_missing_file(tmp_path):
    result = _index_har_started(tmp_path / "nonexistent.har")
    assert result == {}


def test_index_har_started_malformed_json(tmp_path):
    p = tmp_path / "bad.har"
    p.write_text("not json {{{")
    result = _index_har_started(p)
    assert result == {}


def test_index_har_started_empty_entries(tmp_path):
    result = _index_har_started(_write_har(tmp_path, []))
    assert result == {}


def test_index_har_started_skips_entries_missing_fields(tmp_path):
    entries = [
        {"request": {}, "startedDateTime": "2024-01-01T00:00:01Z"},
        {"request": {"url": "https://example.com/"}, "startedDateTime": None},
        {"request": {"url": "https://good.com/"}, "startedDateTime": "2024-01-01T00:00:03Z"},
    ]
    result = _index_har_started(_write_har(tmp_path, entries))
    assert list(result) == ["https://good.com/"]
