"""SQLite storage layer with the observable/technique/campaign schema."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from uuid import UUID

import aiosqlite

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SCHEMA_SQL = """
-- Core tables
CREATE TABLE IF NOT EXISTS runs (
    id              TEXT PRIMARY KEY,
    seed_url        TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    egress_type     TEXT NOT NULL DEFAULT 'direct',
    created_at      TEXT NOT NULL,
    completed_at    TEXT,
    config_json     TEXT NOT NULL,
    error           TEXT
);

CREATE TABLE IF NOT EXISTS artifacts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    type            TEXT NOT NULL,
    path            TEXT NOT NULL,
    size            INTEGER,
    content_hash    TEXT
);

-- Campaign tables
CREATE TABLE IF NOT EXISTS campaigns (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    description     TEXT DEFAULT '',
    confidence      REAL DEFAULT 0.0,
    status          TEXT DEFAULT 'active',
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaign_runs (
    campaign_id     TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    run_id          TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    PRIMARY KEY (campaign_id, run_id)
);

-- Observable tables
CREATE TABLE IF NOT EXISTS observables (
    id              TEXT PRIMARY KEY,
    type            TEXT NOT NULL,
    value           TEXT NOT NULL,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    UNIQUE(type, value)
);

CREATE TABLE IF NOT EXISTS observable_metadata (
    observable_id   TEXT NOT NULL REFERENCES observables(id) ON DELETE CASCADE,
    key             TEXT NOT NULL,
    value           TEXT NOT NULL,
    PRIMARY KEY (observable_id, key)
);

CREATE TABLE IF NOT EXISTS run_observables (
    run_id          TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    observable_id   TEXT NOT NULL REFERENCES observables(id) ON DELETE CASCADE,
    source          TEXT NOT NULL,
    context_json    TEXT,
    PRIMARY KEY (run_id, observable_id, source)
);

CREATE TABLE IF NOT EXISTS observable_links (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id       TEXT NOT NULL REFERENCES observables(id) ON DELETE CASCADE,
    target_id       TEXT NOT NULL REFERENCES observables(id) ON DELETE CASCADE,
    relationship    TEXT NOT NULL,
    confidence      REAL DEFAULT 1.0,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    evidence_json   TEXT
);

CREATE TABLE IF NOT EXISTS campaign_observables (
    campaign_id     TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    observable_id   TEXT NOT NULL REFERENCES observables(id) ON DELETE CASCADE,
    role            TEXT DEFAULT 'indicator',
    PRIMARY KEY (campaign_id, observable_id)
);

-- Technique tables
CREATE TABLE IF NOT EXISTS techniques (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    description     TEXT DEFAULT '',
    signature_type  TEXT NOT NULL,
    detection_module TEXT
);

CREATE TABLE IF NOT EXISTS technique_matches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    technique_id    TEXT NOT NULL REFERENCES techniques(id) ON DELETE CASCADE,
    run_id          TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    confidence      REAL DEFAULT 1.0,
    evidence_json   TEXT
);

CREATE TABLE IF NOT EXISTS campaign_techniques (
    campaign_id     TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    technique_id    TEXT NOT NULL REFERENCES techniques(id) ON DELETE CASCADE,
    PRIMARY KEY (campaign_id, technique_id)
);

-- Indexes for common query patterns
CREATE INDEX IF NOT EXISTS idx_runs_seed_url ON runs(seed_url);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
CREATE INDEX IF NOT EXISTS idx_artifacts_run ON artifacts(run_id);
CREATE INDEX IF NOT EXISTS idx_observables_type ON observables(type);
CREATE INDEX IF NOT EXISTS idx_observables_value ON observables(value);
CREATE INDEX IF NOT EXISTS idx_run_observables_run ON run_observables(run_id);
CREATE INDEX IF NOT EXISTS idx_run_observables_obs ON run_observables(observable_id);
CREATE INDEX IF NOT EXISTS idx_observable_links_source ON observable_links(source_id);
CREATE INDEX IF NOT EXISTS idx_observable_links_target ON observable_links(target_id);
CREATE INDEX IF NOT EXISTS idx_technique_matches_run ON technique_matches(run_id);
CREATE INDEX IF NOT EXISTS idx_technique_matches_tech ON technique_matches(technique_id);

-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_meta (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);
"""


class Database:
    """Async SQLite database wrapper for the detonator schema."""

    def __init__(self, db_path: str | Path) -> None:
        self._path = str(db_path)
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.executescript(_SCHEMA_SQL)
        await self._db.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
            ("version", str(SCHEMA_VERSION)),
        )
        await self._db.commit()
        logger.info("Database initialized at %s (schema v%d)", self._path, SCHEMA_VERSION)

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "call connect() first"
        return self._db

    # ── Runs ──────────────────────────────────────────────────────

    async def insert_run(
        self, run_id: str, seed_url: str, egress_type: str, config: dict, created_at: str
    ) -> None:
        await self.db.execute(
            "INSERT INTO runs (id, seed_url, egress_type, config_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (run_id, seed_url, egress_type, json.dumps(config), created_at),
        )
        await self.db.commit()

    async def update_run_status(
        self, run_id: str, status: str, *, completed_at: str | None = None, error: str | None = None
    ) -> None:
        if completed_at:
            await self.db.execute(
                "UPDATE runs SET status=?, completed_at=?, error=? WHERE id=?",
                (status, completed_at, error, run_id),
            )
        else:
            await self.db.execute(
                "UPDATE runs SET status=?, error=? WHERE id=?",
                (status, error, run_id),
            )
        await self.db.commit()

    async def get_run(self, run_id: str) -> dict | None:
        cursor = await self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_runs(
        self, *, status: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[dict]:
        if status:
            cursor = await self.db.execute(
                "SELECT * FROM runs WHERE status=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (status, limit, offset),
            )
        else:
            cursor = await self.db.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
        return [dict(row) for row in await cursor.fetchall()]

    async def delete_run(self, run_id: str) -> bool:
        cursor = await self.db.execute("DELETE FROM runs WHERE id=?", (run_id,))
        await self.db.commit()
        return cursor.rowcount > 0

    # ── Artifacts ─────────────────────────────────────────────────

    async def insert_artifact(
        self, run_id: str, artifact_type: str, path: str, size: int | None = None, content_hash: str | None = None
    ) -> None:
        await self.db.execute(
            "INSERT INTO artifacts (run_id, type, path, size, content_hash) VALUES (?, ?, ?, ?, ?)",
            (run_id, artifact_type, path, size, content_hash),
        )
        await self.db.commit()

    async def get_artifacts(self, run_id: str) -> list[dict]:
        cursor = await self.db.execute(
            "SELECT * FROM artifacts WHERE run_id=?", (run_id,)
        )
        return [dict(row) for row in await cursor.fetchall()]

    # ── Observables ───────────────────────────────────────────────

    async def upsert_observable(
        self, obs_id: str, obs_type: str, value: str, seen_at: str
    ) -> None:
        await self.db.execute(
            """INSERT INTO observables (id, type, value, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(type, value) DO UPDATE SET last_seen=excluded.last_seen""",
            (obs_id, obs_type, value, seen_at, seen_at),
        )
        await self.db.commit()

    async def link_run_observable(
        self, run_id: str, observable_id: str, source: str, context: dict | None = None
    ) -> None:
        await self.db.execute(
            """INSERT OR IGNORE INTO run_observables (run_id, observable_id, source, context_json)
               VALUES (?, ?, ?, ?)""",
            (run_id, observable_id, source, json.dumps(context) if context else None),
        )
        await self.db.commit()

    async def link_observables(
        self,
        source_id: str,
        target_id: str,
        relationship: str,
        seen_at: str,
        *,
        confidence: float = 1.0,
        evidence: dict | None = None,
    ) -> None:
        await self.db.execute(
            """INSERT INTO observable_links (source_id, target_id, relationship, confidence, first_seen, last_seen, evidence_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (source_id, target_id, relationship, confidence, seen_at, seen_at, json.dumps(evidence) if evidence else None),
        )
        await self.db.commit()

    async def find_observables(
        self, *, obs_type: str | None = None, value_pattern: str | None = None, limit: int = 50
    ) -> list[dict]:
        conditions = []
        params: list[Any] = []
        if obs_type:
            conditions.append("type=?")
            params.append(obs_type)
        if value_pattern:
            conditions.append("value LIKE ?")
            params.append(value_pattern)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cursor = await self.db.execute(
            f"SELECT * FROM observables {where} ORDER BY last_seen DESC LIMIT ?",
            (*params, limit),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def get_observable_graph(self, observable_id: str) -> dict:
        """Return the neighborhood of an observable: linked observables and campaigns."""
        outgoing = await self.db.execute(
            "SELECT * FROM observable_links WHERE source_id=?", (observable_id,)
        )
        incoming = await self.db.execute(
            "SELECT * FROM observable_links WHERE target_id=?", (observable_id,)
        )
        campaigns = await self.db.execute(
            """SELECT c.* FROM campaigns c
               JOIN campaign_observables co ON c.id=co.campaign_id
               WHERE co.observable_id=?""",
            (observable_id,),
        )
        return {
            "observable_id": observable_id,
            "outgoing_links": [dict(r) for r in await outgoing.fetchall()],
            "incoming_links": [dict(r) for r in await incoming.fetchall()],
            "campaigns": [dict(r) for r in await campaigns.fetchall()],
        }

    # ── Campaigns ─────────────────────────────────────────────────

    async def insert_campaign(
        self, campaign_id: str, name: str, description: str, seen_at: str
    ) -> None:
        await self.db.execute(
            "INSERT INTO campaigns (id, name, description, first_seen, last_seen) VALUES (?, ?, ?, ?, ?)",
            (campaign_id, name, description, seen_at, seen_at),
        )
        await self.db.commit()

    async def get_campaign(self, campaign_id: str) -> dict | None:
        cursor = await self.db.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def link_campaign_run(self, campaign_id: str, run_id: str) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO campaign_runs (campaign_id, run_id) VALUES (?, ?)",
            (campaign_id, run_id),
        )
        await self.db.commit()

    # ── Techniques ────────────────────────────────────────────────

    async def upsert_technique(
        self, tech_id: str, name: str, description: str, signature_type: str, detection_module: str | None = None
    ) -> None:
        await self.db.execute(
            """INSERT INTO techniques (id, name, description, signature_type, detection_module)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET description=excluded.description, signature_type=excluded.signature_type, detection_module=excluded.detection_module""",
            (tech_id, name, description, signature_type, detection_module),
        )
        await self.db.commit()

    async def insert_technique_match(
        self, technique_id: str, run_id: str, confidence: float = 1.0, evidence: dict | None = None
    ) -> None:
        await self.db.execute(
            "INSERT INTO technique_matches (technique_id, run_id, confidence, evidence_json) VALUES (?, ?, ?, ?)",
            (technique_id, run_id, confidence, json.dumps(evidence) if evidence else None),
        )
        await self.db.commit()
