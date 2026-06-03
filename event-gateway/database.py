import sqlite3
import threading
from contextlib import contextmanager

DB_PATH = "gateway.db"
_conn: sqlite3.Connection | None = None
_lock = threading.Lock()


def get_connection() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        with _lock:
            if _conn is None:
                uri = DB_PATH.startswith("file:")
                _conn = sqlite3.connect(DB_PATH, check_same_thread=False, uri=uri)
                _conn.row_factory = sqlite3.Row
                _conn.execute("PRAGMA journal_mode=WAL")
    return _conn


@contextmanager
def get_db():
    conn = get_connection()
    with _lock:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT UNIQUE NOT NULL,
                account_id TEXT NOT NULL,
                type TEXT NOT NULL,
                amount REAL NOT NULL,
                currency TEXT NOT NULL,
                event_timestamp TEXT NOT NULL,
                metadata TEXT,
                received_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_evt_account ON events(account_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_evt_timestamp ON events(event_timestamp)")
