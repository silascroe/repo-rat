"""Persistent SQLite state for release ingestion and processing."""

from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from .models import Judgment, Release


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class StoredRelease:
    repository: str
    release_id: str
    tag_name: str
    name: str
    body: str
    html_url: str
    published_at: str | None
    prerelease: bool
    first_seen_at: str
    status: str
    reason: str | None
    summary: str | None
    delivered_at: str | None
    delivered_via: str | None

    @property
    def identity(self) -> str:
        return f"{self.repository}:{self.release_id}"


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._connection: sqlite3.Connection | None = None

    def __enter__(self) -> "StateStore":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path)
        self._connection.row_factory = sqlite3.Row
        self._initialize_schema()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self._connection is not None:
            if exc_type is None:
                self._connection.commit()
            else:
                self._connection.rollback()
            self._connection.close()
            self._connection = None

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("StateStore must be used as a context manager")
        return self._connection

    @contextmanager
    def poll_lock(self):
        """Acquire nonblocking, crash-released ownership of this database's poll.

        A separate SQLite file keeps the lock alive across state commits. Never
        unlink this file: replacing it could split ownership between processes.
        Resolve symlinks so equivalent paths share the same lock.
        """
        lock_path = str(self.path.resolve()) + ".poll-lock"
        lock = sqlite3.connect(lock_path, timeout=0)
        try:
            try:
                lock.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                if exc.sqlite_errorcode not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    raise
                yield False
            else:
                yield True
        finally:
            lock.close()

    def _initialize_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS releases (
                repository TEXT NOT NULL,
                release_id TEXT NOT NULL,
                tag_name TEXT NOT NULL,
                name TEXT NOT NULL,
                body TEXT NOT NULL,
                html_url TEXT NOT NULL,
                published_at TEXT,
                prerelease INTEGER NOT NULL,
                first_seen_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('seeded','pending','interesting','uninteresting','failed')),
                reason TEXT,
                summary TEXT,
                delivered_at TEXT,
                delivered_via TEXT,
                PRIMARY KEY(repository, release_id)
            );
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                mode TEXT NOT NULL,
                fetched INTEGER NOT NULL DEFAULT 0,
                judged INTEGER NOT NULL DEFAULT 0,
                reported INTEGER NOT NULL DEFAULT 0,
                errors INTEGER NOT NULL DEFAULT 0,
                error_text TEXT
            );
            CREATE TABLE IF NOT EXISTS repository_baselines (
                repository TEXT PRIMARY KEY,
                initialized_at TEXT NOT NULL
            );
            -- Older versions labeled failed deliveries as failed judgments.
            -- A failed judgment has NULL summary; an approved delivery keeps it.
            UPDATE releases SET status = 'interesting'
            WHERE status = 'failed' AND summary IS NOT NULL;
            """
        )
        self.connection.commit()

    def insert_release(self, release: Release) -> bool:
        with self.connection:
            return self._insert_release(release)

    def _insert_release(self, release: Release) -> bool:
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO releases (
                repository, release_id, tag_name, name, body, html_url,
                published_at, prerelease, first_seen_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                release.repository,
                release.release_id,
                release.tag_name,
                release.name,
                release.body,
                release.html_url,
                release.published_at,
                int(release.prerelease),
                _utc_now(),
            ),
        )
        return cursor.rowcount == 1

    def insert_releases(self, releases: list[Release] | tuple[Release, ...]) -> int:
        with self.connection:
            return sum(int(self._insert_release(release)) for release in releases)

    def seed_repository(self, repository: str) -> int:
        cursor = self.connection.execute(
            "UPDATE releases SET status = 'seeded' WHERE repository = ? AND status = 'pending'",
            (repository,),
        )
        self.connection.commit()
        return cursor.rowcount

    def is_repository_initialized(self, repository: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM repository_baselines WHERE repository = ?", (repository,)
        ).fetchone()
        return row is not None

    def mark_repository_initialized(self, repository: str) -> bool:
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO repository_baselines (repository, initialized_at)
            VALUES (?, ?)
            """,
            (repository, _utc_now()),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def candidates(self, repository: str, include_seeded: bool = False) -> list[StoredRelease]:
        statuses = "'pending', 'failed', 'interesting'" + (", 'seeded'" if include_seeded else "")
        rows = self.connection.execute(
            f"""
            SELECT * FROM releases
            WHERE repository = ? AND status IN ({statuses}) AND delivered_at IS NULL
            ORDER BY published_at IS NOT NULL, published_at ASC
            """,
            (repository,),
        ).fetchall()
        return [self._stored_release(row) for row in rows]

    def release_ids(self, repository: str) -> frozenset[str]:
        """Return the IDs of every stored release, without loading bodies."""
        rows = self.connection.execute(
            "SELECT release_id FROM releases WHERE repository = ?", (repository,)
        ).fetchall()
        return frozenset(row["release_id"] for row in rows)

    def get_releases(self, repository: str) -> list[StoredRelease]:
        rows = self.connection.execute(
            "SELECT * FROM releases WHERE repository = ? ORDER BY published_at IS NOT NULL, published_at ASC",
            (repository,),
        ).fetchall()
        return [self._stored_release(row) for row in rows]

    def save_judgment(
        self,
        repository: str,
        release_id: str,
        judgment: Judgment | None,
        error: str | None = None,
    ) -> bool:
        if judgment is None:
            status = "failed"
            reason = error
            summary = None
        else:
            status = "interesting" if judgment.interesting else "uninteresting"
            reason = judgment.reason
            summary = judgment.summary
        cursor = self.connection.execute(
            """
            UPDATE releases
            SET status = ?, reason = ?, summary = ?
            WHERE repository = ? AND release_id = ?
              AND status IN ('pending', 'seeded', 'failed')
            """,
            (status, reason, summary, repository, release_id),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def mark_delivery(
        self,
        repository: str,
        release_id: str,
        delivered_via: str,
        delivered_at: str | None = None,
    ) -> bool:
        cursor = self.connection.execute(
            """
            UPDATE releases
            SET delivered_at = ?, delivered_via = ?
            WHERE repository = ? AND release_id = ? AND delivered_at IS NULL
            """,
            (delivered_at or _utc_now(), delivered_via, repository, release_id),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def mark_delivery_failed(self, repository: str, release_id: str) -> bool:
        """Keep an approved release eligible for delivery, never judgment, retry."""
        cursor = self.connection.execute(
            """
            UPDATE releases
            SET status = 'interesting'
            WHERE repository = ? AND release_id = ?
              AND status = 'interesting' AND delivered_at IS NULL
            """,
            (repository, release_id),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def start_run(self, mode: str, started_at: str | None = None) -> int:
        cursor = self.connection.execute(
            "INSERT INTO runs (started_at, mode) VALUES (?, ?)",
            (started_at or _utc_now(), mode),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def finish_run(
        self,
        run_id: int,
        *,
        finished_at: str | None = None,
        fetched: int = 0,
        judged: int = 0,
        reported: int = 0,
        errors: int = 0,
        error_text: str | None = None,
    ) -> bool:
        cursor = self.connection.execute(
            """
            UPDATE runs
            SET finished_at = ?, fetched = ?, judged = ?, reported = ?,
                errors = ?, error_text = ?
            WHERE id = ?
            """,
            (
                finished_at or _utc_now(),
                fetched,
                judged,
                reported,
                errors,
                error_text,
                run_id,
            ),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def get_run(self, run_id: int) -> dict[str, object] | None:
        row = self.connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def _stored_release(row: sqlite3.Row) -> StoredRelease:
        return StoredRelease(
            repository=row["repository"],
            release_id=row["release_id"],
            tag_name=row["tag_name"],
            name=row["name"],
            body=row["body"],
            html_url=row["html_url"],
            published_at=row["published_at"],
            prerelease=bool(row["prerelease"]),
            first_seen_at=row["first_seen_at"],
            status=row["status"],
            reason=row["reason"],
            summary=row["summary"],
            delivered_at=row["delivered_at"],
            delivered_via=row["delivered_via"],
        )
