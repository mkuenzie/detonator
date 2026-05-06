"""Unit tests for the transaction view-model helper."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from detonator.ui.transactions import (
    BodyPreview,
    _dedup_transactions,
    _is_image_mime,
    _is_text_mime,
    _validate_basename,
    get_transaction,
    load_body_preview,
    load_transactions,
)
from detonator.ui.transactions import Transaction


# ── Fixtures ─────────────────────────────────────────────────────────


SAMPLE_HAR = {
    "log": {
        "version": "1.2",
        "entries": [
            {
                "startedDateTime": "2024-01-01T00:00:00.000Z",
                "time": 42.5,
                "request": {
                    "method": "GET",
                    "url": "https://example.com/",
                    "headers": [{"name": "Accept", "value": "*/*"}],
                    "bodySize": -1,
                },
                "response": {
                    "status": 200,
                    "headers": [{"name": "Content-Type", "value": "text/html; charset=utf-8"}],
                    "content": {
                        "size": 1234,
                        "mimeType": "text/html; charset=utf-8",
                        "_file": "bodies/aabbcc.html",
                    },
                },
                "_resourceType": "document",
                "serverIPAddress": "93.184.216.34",
                "_initiator": {"type": "other"},
            },
            {
                "startedDateTime": "2024-01-01T00:00:00.050Z",
                "time": 10.0,
                "request": {
                    "method": "GET",
                    "url": "https://cdn.example.com/lib.js",
                    "headers": [],
                    "bodySize": -1,
                },
                "response": {
                    "status": 200,
                    "headers": [],
                    "content": {
                        "size": 8192,
                        "mimeType": "application/javascript",
                        "_file": "bodies/deadbeef.js",
                    },
                },
                "_resourceType": "script",
                "serverIPAddress": "",
                "_initiator": {
                    "type": "parser",
                    "url": "https://example.com/",
                },
            },
            {
                "startedDateTime": "2024-01-01T00:00:00.100Z",
                "time": 5.0,
                "request": {
                    "method": "GET",
                    "url": "https://example.com/logo.png",
                    "headers": [],
                    "bodySize": -1,
                },
                "response": {
                    "status": 200,
                    "headers": [],
                    "content": {
                        "size": 2048,
                        "mimeType": "image/png",
                    },
                },
                "_resourceType": "image",
                "_initiator": {"type": "other"},
            },
        ],
    }
}

SAMPLE_MANIFEST = (
    '{"basename":"sidecar01.html","url":"https://example.com/extra","direction":"response",'
    '"method":"GET","mime_type":"text/html","capture_outcome":"ok","captured_at":"2024-01-01T00:00:00Z"}\n'
)


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    har_path = tmp_path / "har_full.har"
    har_path.write_text(json.dumps(SAMPLE_HAR), encoding="utf-8")

    bodies_dir = tmp_path / "bodies"
    bodies_dir.mkdir()
    (bodies_dir / "aabbcc.html").write_bytes(b"<html>hello</html>")
    (bodies_dir / "deadbeef.js").write_bytes(b"console.log('hi');")
    (bodies_dir / "manifest.jsonl").write_text(SAMPLE_MANIFEST, encoding="utf-8")

    return tmp_path


# ── load_transactions ─────────────────────────────────────────────────


def test_load_transactions_count(run_dir):
    txns = load_transactions(run_dir)
    assert len(txns) == 3


def test_load_transactions_first_entry(run_dir):
    txns = load_transactions(run_dir)
    t = txns[0]
    assert t.index == 0
    assert t.url == "https://example.com/"
    assert t.method == "GET"
    assert t.status == 200
    assert t.mime_type == "text/html"
    assert t.resource_type == "document"
    assert t.server_ip == "93.184.216.34"
    assert t.body_basename == "aabbcc.html"
    assert t.is_text is True
    assert t.is_image is False
    assert t.time_ms == 42.5


def test_load_transactions_script(run_dir):
    txns = load_transactions(run_dir)
    t = txns[1]
    assert t.resource_type == "script"
    assert t.initiator_url == "https://example.com/"
    assert t.initiator_type == "parser"
    assert t.body_basename == "deadbeef.js"
    assert t.is_text is True


def test_load_transactions_image(run_dir):
    txns = load_transactions(run_dir)
    t = txns[2]
    assert t.mime_type == "image/png"
    assert t.is_image is True
    assert t.is_text is False
    assert t.body_basename is None


def test_load_transactions_negative_bodysize(run_dir):
    txns = load_transactions(run_dir)
    assert txns[0].size_request is None


def test_load_transactions_negative_response_size(tmp_path):
    """HAR content.size=-1 is normalised to None (not shown as '-1 B')."""
    har = {
        "log": {
            "entries": [{
                "startedDateTime": "2024-01-01T00:00:00Z",
                "time": 10.0,
                "request": {"method": "GET", "url": "https://example.com/nav", "headers": [], "bodySize": -1},
                "response": {
                    "status": 200,
                    "headers": [],
                    "content": {"size": -1, "mimeType": "text/html"},
                },
                "_resourceType": "document",
                "_initiator": {"type": "other"},
            }],
        }
    }
    (tmp_path / "har_full.har").write_text(json.dumps(har))
    txns = load_transactions(tmp_path)
    assert txns[0].size_response is None


def test_load_transactions_empty_har(tmp_path):
    (tmp_path / "har_full.har").write_text(json.dumps({"log": {"entries": []}}))
    assert load_transactions(tmp_path) == []


def test_load_transactions_missing_har(tmp_path):
    assert load_transactions(tmp_path) == []


# ── Transaction properties ─────────────────────────────────────────────


def test_transaction_host(run_dir):
    txns = load_transactions(run_dir)
    assert txns[0].host == "example.com"
    assert txns[1].host == "cdn.example.com"


def test_transaction_path(run_dir):
    txns = load_transactions(run_dir)
    assert txns[0].path == "/"
    assert txns[1].path == "/lib.js"


def test_transaction_status_class(run_dir):
    txns = load_transactions(run_dir)
    assert txns[0].status_class == "status-2xx"


# ── get_transaction ────────────────────────────────────────────────────


def test_get_transaction_headers(run_dir):
    tx = get_transaction(run_dir, 0)
    assert tx is not None
    assert ("Accept", "*/*") in tx.request_headers
    assert ("Content-Type", "text/html; charset=utf-8") in tx.response_headers


def test_get_transaction_out_of_range(run_dir):
    assert get_transaction(run_dir, 99) is None


def test_get_transaction_initiator_chain(run_dir):
    tx = get_transaction(run_dir, 1)
    assert tx is not None
    assert "https://example.com/" in tx.initiator_chain


# ── load_body_preview ──────────────────────────────────────────────────


def test_body_preview_text(run_dir):
    preview = load_body_preview(run_dir, "aabbcc.html", "text/html")
    assert preview.is_text is True
    assert preview.text is not None
    assert "hello" in preview.text
    assert preview.truncated is False
    assert preview.size_bytes == len(b"<html>hello</html>")


def test_body_preview_truncation(run_dir):
    preview = load_body_preview(run_dir, "aabbcc.html", "text/html", max_bytes=5)
    assert preview.truncated is True
    assert preview.text == "<html"


def test_body_preview_missing_file(run_dir):
    preview = load_body_preview(run_dir, "nothere.html", "text/html")
    assert preview.size_bytes == 0
    assert preview.text is None


def test_body_preview_binary_mime(run_dir):
    bodies_dir = run_dir / "bodies"
    (bodies_dir / "img.png").write_bytes(b"\x89PNG\r\n")
    preview = load_body_preview(run_dir, "img.png", "image/png")
    assert preview.is_image is True
    assert preview.text is None
    assert preview.is_text is False


# ── Helpers ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("mime,expected", [
    ("text/html", True),
    ("text/html; charset=utf-8", True),
    ("application/json", True),
    ("application/javascript", True),
    ("application/xml", True),
    ("image/png", False),
    ("application/octet-stream", False),
    (None, False),
])
def test_is_text_mime(mime, expected):
    assert _is_text_mime(mime) == expected


@pytest.mark.parametrize("mime,expected", [
    ("image/png", True),
    ("image/jpeg", True),
    ("image/gif", True),
    ("text/html", False),
    (None, False),
])
def test_is_image_mime(mime, expected):
    assert _is_image_mime(mime) == expected


@pytest.mark.parametrize("basename,expected", [
    ("aabbcc.html", True),
    ("deadbeef.js", True),
    ("../secret", False),
    ("sub/file.js", False),
    ("file with spaces.js", True),
    ("", False),
])
def test_validate_basename(basename, expected):
    assert _validate_basename(basename) == expected


# ── _dedup_transactions ─────────────────────────────────────────────────


def _make_txn(**kwargs) -> Transaction:
    defaults = dict(
        index=0, started_at=None, time_ms=None, method="GET",
        url="https://example.com/", status=200, mime_type="text/html",
        resource_type="document", size_response=None, size_request=None,
        server_ip=None, initiator_type="other", initiator_url=None,
        body_basename=None, capture_outcome=None, is_text=True, is_image=False,
    )
    defaults.update(kwargs)
    return Transaction(**defaults)


def test_dedup_no_op_single_entries():
    """No dedup when all (url, method) pairs are unique."""
    txns = [
        _make_txn(index=0, url="https://a.com/", size_response=100),
        _make_txn(index=1, url="https://b.com/", size_response=200),
    ]
    result = _dedup_transactions(txns)
    assert len(result) == 2
    assert result[0].url == "https://a.com/"
    assert result[1].url == "https://b.com/"


def test_dedup_removes_no_data_duplicate():
    """The size=None (HAR-miss) entry is dropped when a with-data entry exists."""
    txns = [
        _make_txn(index=0, url="https://a.com/page", size_response=None, body_basename="sidecar.html"),
        _make_txn(index=1, url="https://a.com/page", size_response=245, body_basename="har.html"),
    ]
    result = _dedup_transactions(txns)
    assert len(result) == 1
    assert result[0].index == 1
    assert result[0].size_response == 245


def test_dedup_propagates_basename_to_winner():
    """body_basename from the no-data entry is promoted to the winner when it lacks one."""
    txns = [
        _make_txn(index=0, url="https://a.com/page", size_response=None, body_basename="sidecar.html"),
        _make_txn(index=1, url="https://a.com/page", size_response=245, body_basename=None),
    ]
    result = _dedup_transactions(txns)
    assert len(result) == 1
    assert result[0].body_basename == "sidecar.html"


def test_dedup_winner_basename_not_overridden():
    """When the winner already has a basename, the sidecar one is ignored."""
    txns = [
        _make_txn(index=0, url="https://a.com/page", size_response=None, body_basename="sidecar.html"),
        _make_txn(index=1, url="https://a.com/page", size_response=245, body_basename="har.html"),
    ]
    result = _dedup_transactions(txns)
    assert result[0].body_basename == "har.html"


def test_dedup_keeps_all_genuine_duplicates():
    """Two entries with data for the same URL are genuine parallel requests — keep both."""
    txns = [
        _make_txn(index=0, url="https://a.com/poll", size_response=50),
        _make_txn(index=1, url="https://a.com/poll", size_response=50),
    ]
    result = _dedup_transactions(txns)
    assert len(result) == 2


def test_dedup_keeps_all_no_data_when_no_winner():
    """Two size=None entries for same URL — neither has data, keep both (don't silently drop)."""
    txns = [
        _make_txn(index=0, url="https://a.com/page", size_response=None),
        _make_txn(index=1, url="https://a.com/page", size_response=None),
    ]
    result = _dedup_transactions(txns)
    assert len(result) == 2


def test_dedup_via_load_transactions(tmp_path):
    """Integration: duplicate HAR entries collapse to one row in load_transactions."""
    har = {
        "log": {
            "entries": [
                {
                    "startedDateTime": "2024-01-01T00:00:00Z",
                    "time": 5.0,
                    "request": {"method": "GET", "url": "https://example.com/nav", "headers": [], "bodySize": -1},
                    "response": {"status": 200, "headers": [], "content": {"size": -1, "mimeType": "text/html"}},
                    "_resourceType": "document",
                    "_initiator": {"type": "other"},
                },
                {
                    "startedDateTime": "2024-01-01T00:00:00.010Z",
                    "time": 10.0,
                    "request": {"method": "GET", "url": "https://example.com/nav", "headers": [], "bodySize": -1},
                    "response": {
                        "status": 200, "headers": [],
                        "content": {"size": 512, "mimeType": "text/html", "_file": "bodies/abc123.html"},
                    },
                    "_resourceType": "document",
                    "_initiator": {"type": "other"},
                },
                {
                    "startedDateTime": "2024-01-01T00:00:01Z",
                    "time": 3.0,
                    "request": {"method": "GET", "url": "https://cdn.example.com/lib.js", "headers": [], "bodySize": -1},
                    "response": {"status": 200, "headers": [], "content": {"size": 1024, "mimeType": "application/javascript"}},
                    "_resourceType": "script",
                    "_initiator": {"type": "other"},
                },
            ]
        }
    }
    (tmp_path / "har_full.har").write_text(json.dumps(har))
    bodies = tmp_path / "bodies"
    bodies.mkdir()
    (bodies / "abc123.html").write_bytes(b"<html/>")

    txns = load_transactions(tmp_path)
    assert len(txns) == 2
    nav = txns[0]
    assert nav.url == "https://example.com/nav"
    assert nav.size_response == 512
    assert nav.body_basename == "abc123.html"
