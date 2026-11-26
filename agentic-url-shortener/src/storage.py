import sqlite3
import json
import threading
import time
from typing import Optional, List, Dict, Any
from src.models import ShortLink, ClickEvent


class StorageRepository:
    def __init__(self, db_path: str = ":memory:"):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS short_links (
                    id INTEGER,
                    code TEXT PRIMARY KEY,
                    long_url TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL,
                    clicks INTEGER NOT NULL DEFAULT 0,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                )
            """)
            columns = {
                row["name"]
                for row in self._conn.execute("PRAGMA table_info(short_links)").fetchall()
            }
            if "id" not in columns:
                self._conn.execute(
                    "ALTER TABLE short_links ADD COLUMN id INTEGER"
                )
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS click_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    user_agent TEXT,
                    referer TEXT,
                    client_ip_hash TEXT,
                    redirect_status INTEGER,
                    latency_ms REAL
                )
            """)
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_click_events_code ON click_events(code)"
            )

    def create_link(self, link: ShortLink) -> bool:
        with self._lock:
            try:
                with self._conn:
                    self._conn.execute(
                        """
                        INSERT INTO short_links (id, code, long_url, created_at, expires_at, clicks, is_active, metadata_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            link.numeric_id,
                            link.code,
                            link.long_url,
                            link.created_at,
                            link.expires_at,
                            link.clicks,
                            1 if link.is_active else 0,
                            json.dumps(link.metadata),
                        ),
                    )
                return True
            except sqlite3.IntegrityError:
                return False

    def allocate_numeric_id(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(id), 0) + 1 AS next_id FROM short_links"
            ).fetchone()
            return int(row["next_id"])

    def check_health(self) -> None:
        with self._lock:
            self._conn.execute("SELECT 1").fetchone()

    def get_link(self, code: str) -> Optional[ShortLink]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM short_links WHERE code = ?", (code,)
            ).fetchone()
        if not row:
            return None
        return ShortLink(
            code=row["code"],
            long_url=row["long_url"],
            created_at=row["created_at"],
            numeric_id=row["id"] if "id" in row.keys() else None,
            expires_at=row["expires_at"],
            clicks=row["clicks"],
            is_active=bool(row["is_active"]),
            metadata=json.loads(row["metadata_json"] or "{}"),
        )

    def find_link_by_url(self, long_url: str) -> Optional[ShortLink]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM short_links WHERE long_url = ? ORDER BY id ASC LIMIT 1",
                (long_url,),
            ).fetchone()
        if not row:
            return None
        return ShortLink(
            code=row["code"],
            long_url=row["long_url"],
            created_at=row["created_at"],
            numeric_id=row["id"] if "id" in row.keys() else None,
            expires_at=row["expires_at"],
            clicks=row["clicks"],
            is_active=bool(row["is_active"]),
            metadata=json.loads(row["metadata_json"] or "{}"),
        )

    def record_click_and_increment(self, event: ClickEvent) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE short_links SET clicks = clicks + 1 WHERE code = ?",
                (event.code,),
            )
            self._conn.execute(
                """
                INSERT INTO click_events (code, timestamp, user_agent, referer, client_ip_hash, redirect_status, latency_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.code,
                    event.timestamp,
                    event.user_agent,
                    event.referer,
                    event.client_ip_hash,
                    event.redirect_status,
                    event.latency_ms,
                ),
            )

    def get_analytics(self, code: str) -> Optional[Dict[str, Any]]:
        link = self.get_link(code)
        if not link:
            return None
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT timestamp, user_agent, referer, client_ip_hash, redirect_status, latency_ms
                FROM click_events WHERE code = ? ORDER BY id DESC LIMIT 100
                """,
                (code,),
            ).fetchall()
            avg_row = self._conn.execute(
                "SELECT AVG(latency_ms) AS avg_lat FROM click_events WHERE code = ?",
                (code,),
            ).fetchone()

        recent_events = [
            {
                "timestamp": row["timestamp"],
                "redirect_status": row["redirect_status"],
                "latency_ms": row["latency_ms"],
            }
            for row in rows
        ]
        unique_visitors = len({r["client_ip_hash"] for r in rows if r["client_ip_hash"]})
        return {
            "code": link.code,
            "long_url": link.long_url,
            "total_clicks": link.clicks,
            "unique_recent_visitors": unique_visitors,
            "avg_redirect_latency_ms": round(avg_row["avg_lat"], 3) if avg_row and avg_row["avg_lat"] else 0.0,
            "is_active": link.is_active,
            "expires_at": link.expires_at,
            "recent_events": recent_events[:10],
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()
