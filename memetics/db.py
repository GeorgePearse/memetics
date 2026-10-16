"""SQLite is the authority for registrations, cached changes, and delivery receipts."""

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from .config import canonical, digest, validate

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
 id TEXT PRIMARY KEY, repo TEXT NOT NULL, ref TEXT NOT NULL, head TEXT, etag TEXT,
 next_poll REAL NOT NULL DEFAULT 0, error TEXT, polls INTEGER NOT NULL DEFAULT 0,
 UNIQUE(repo, ref));
CREATE TABLE IF NOT EXISTS listeners (
 id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id), config TEXT NOT NULL,
 config_hash TEXT NOT NULL, enabled INTEGER NOT NULL, observed TEXT, decided TEXT NOT NULL,
 adopted TEXT, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS changes (
 id TEXT PRIMARY KEY, repo TEXT NOT NULL, before_sha TEXT NOT NULL, after_sha TEXT NOT NULL,
 data TEXT NOT NULL, created REAL NOT NULL, UNIQUE(repo,before_sha,after_sha));
CREATE TABLE IF NOT EXISTS blobs (
 repo TEXT NOT NULL, sha TEXT NOT NULL, content TEXT NOT NULL, PRIMARY KEY(repo,sha));
CREATE VIRTUAL TABLE IF NOT EXISTS blob_search USING fts5(repo UNINDEXED, sha UNINDEXED, content);
CREATE TABLE IF NOT EXISTS jobs (
 id INTEGER PRIMARY KEY, listener_id TEXT NOT NULL REFERENCES listeners(id),
 config TEXT NOT NULL, config_hash TEXT NOT NULL, before_sha TEXT NOT NULL, after_sha TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
 not_before REAL NOT NULL DEFAULT 0, reason TEXT, evidence TEXT, delivery TEXT,
 created REAL NOT NULL, updated REAL NOT NULL,
 UNIQUE(listener_id, config_hash, after_sha));
CREATE TABLE IF NOT EXISTS proposals (
 listener_id TEXT PRIMARY KEY REFERENCES listeners(id), repo TEXT NOT NULL, number INTEGER NOT NULL,
 url TEXT NOT NULL, branch TEXT NOT NULL, head_sha TEXT NOT NULL, upstream_sha TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'open');
CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, received REAL NOT NULL);
"""


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "state.sqlite3"
        with self.connect() as db:
            db.executescript(SCHEMA)
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def rows(self, sql, args=()):
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql, args)]

    def one(self, sql, args=()):
        rows = self.rows(sql, args)
        return rows[0] if rows else None

    def execute(self, sql, args=()):
        with self.connect() as db:
            return db.execute(sql, args).lastrowid

    def register(self, configs):
        # Validate the whole request before mutating any standing authorization.
        configs = [validate(c) for c in configs]
        if len({c["id"] for c in configs}) != len(configs):
            raise ValueError("duplicate listener ids in manifest")
        with self.connect() as db:
            for c in configs:
                sid = digest(
                    [c["upstream"]["repository"].lower(), c["upstream"]["ref"]]
                )
                h = digest(c)
                old = db.execute(
                    "SELECT * FROM listeners WHERE id=?", (c["id"],)
                ).fetchone()
                if old and old["config_hash"] == h:
                    continue  # Re-import does not resume a manually paused listener.
                if (
                    old
                    and db.execute(
                        "SELECT 1 FROM proposals WHERE listener_id=? AND state='open'",
                        (c["id"],),
                    ).fetchone()
                ):
                    raise ValueError(
                        "close or merge the open proposal before changing its listener configuration"
                    )
                if (
                    old
                    and db.execute(
                        "SELECT 1 FROM jobs WHERE listener_id=? AND (status IN ('running','delivering') OR (status='blocked' AND delivery IS NOT NULL))",
                        (c["id"],),
                    ).fetchone()
                ):
                    raise ValueError(
                        "listener has in-flight work; reconcile it before changing configuration"
                    )
                db.execute(
                    "INSERT OR IGNORE INTO sources(id,repo,ref) VALUES(?,?,?)",
                    (sid, c["upstream"]["repository"], c["upstream"]["ref"]),
                )
                db.execute(
                    """INSERT INTO listeners(id,source_id,config,config_hash,enabled,decided,updated)
                    VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET source_id=excluded.source_id,
                    config=excluded.config,config_hash=excluded.config_hash,enabled=excluded.enabled,
                    decided=excluded.decided,observed=NULL,adopted=NULL,updated=excluded.updated""",
                    (
                        c["id"],
                        sid,
                        canonical(c),
                        h,
                        c["enabled"],
                        c["upstream"]["baseline_commit"],
                        time.time(),
                    ),
                )
                db.execute(
                    "UPDATE jobs SET status='superseded' WHERE listener_id=? AND status IN ('queued','retry','blocked')",
                    (c["id"],),
                )
                db.execute("UPDATE sources SET next_poll=0 WHERE id=?", (sid,))
        return [c["id"] for c in configs]

    def enable(self, listener_id, enabled):
        with self.connect() as db:
            if not db.execute(
                "SELECT 1 FROM listeners WHERE id=?", (listener_id,)
            ).fetchone():
                raise ValueError("unknown listener")
            db.execute(
                "UPDATE listeners SET enabled=?,updated=? WHERE id=?",
                (enabled, time.time(), listener_id),
            )
            if enabled:
                db.execute(
                    "UPDATE sources SET next_poll=0 WHERE id=(SELECT source_id FROM listeners WHERE id=?)",
                    (listener_id,),
                )

    def enqueue(self, source_id, head):
        now = time.time()
        with self.connect() as db:
            for listener in db.execute(
                "SELECT * FROM listeners WHERE source_id=? AND enabled=1", (source_id,)
            ).fetchall():
                db.execute(
                    "UPDATE listeners SET observed=? WHERE id=?", (head, listener["id"])
                )
                if head == listener["decided"]:
                    continue
                # Do not replace an uncertain publication or restart a blocked generation.
                if db.execute(
                    "SELECT 1 FROM jobs WHERE listener_id=? AND status IN ('running','delivering','blocked')",
                    (listener["id"],),
                ).fetchone():
                    continue
                db.execute(
                    "UPDATE jobs SET status='superseded',updated=? WHERE listener_id=? AND status IN ('queued','retry') AND after_sha<>?",
                    (now, listener["id"], head),
                )
                db.execute(
                    """INSERT OR IGNORE INTO jobs(listener_id,config,config_hash,before_sha,after_sha,created,updated)
                    VALUES(?,?,?,?,?,?,?)""",
                    (
                        listener["id"],
                        listener["config"],
                        listener["config_hash"],
                        listener["decided"],
                        head,
                        now,
                        now,
                    ),
                )

    def set_job(self, job_id, status, **fields):
        allowed = {
            "reason",
            "evidence",
            "delivery",
            "attempts",
            "not_before",
            "before_sha",
        }
        if not set(fields) <= allowed:
            raise ValueError("invalid job fields")
        fields.update(status=status, updated=time.time())
        self.execute(
            f"UPDATE jobs SET {','.join(k + '=?' for k in fields)} WHERE id=?",
            (*fields.values(), job_id),
        )

    def finish(self, job, status, reason, evidence=None):
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET status=?,reason=?,evidence=?,updated=? WHERE id=?",
                (status, reason, canonical(evidence), time.time(), job["id"]),
            )
            db.execute(
                "UPDATE listeners SET decided=? WHERE id=? AND config_hash=?",
                (job["after_sha"], job["listener_id"], job["config_hash"]),
            )

    def cache_blob(self, repo, sha, text):
        with self.connect() as db:
            if db.execute(
                "INSERT OR IGNORE INTO blobs VALUES(?,?,?)", (repo, sha, text)
            ).rowcount:
                db.execute(
                    "INSERT INTO blob_search(repo,sha,content) VALUES(?,?,?)",
                    (repo, sha, text),
                )

    def status(self):
        return {
            "sources": self.rows("SELECT * FROM sources"),
            "listeners": self.rows(
                "SELECT id,source_id,enabled,observed,decided,adopted,updated FROM listeners"
            ),
            "jobs": self.rows(
                "SELECT id,listener_id,status,after_sha,attempts,reason,updated FROM jobs ORDER BY id DESC LIMIT 100"
            ),
            "proposals": self.rows("SELECT * FROM proposals"),
            "cache": self.one(
                "SELECT (SELECT count(*) FROM changes) AS changes, (SELECT count(*) FROM blobs) AS blobs"
            ),
        }
