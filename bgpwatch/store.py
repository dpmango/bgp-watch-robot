"""SQLite-хранилище: подписки (watches) и последний статус (state)."""
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS watches (
    chat_id  INTEGER NOT NULL,
    ip       TEXT    NOT NULL,
    port     INTEGER NOT NULL DEFAULT 22,
    prefix   TEXT,
    asn      INTEGER,
    added_at INTEGER NOT NULL,
    label    TEXT,
    PRIMARY KEY (chat_id, ip, port)
);
CREATE TABLE IF NOT EXISTS state (
    ip          TEXT    NOT NULL,
    port        INTEGER NOT NULL,
    prefix      TEXT,
    asn         INTEGER,
    tcp         TEXT,
    bgp_seeing  INTEGER,
    bgp_total   INTEGER,
    last_change INTEGER,
    last_check  INTEGER,
    PRIMARY KEY (ip, port)
);
"""


class Store:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        columns = {r["name"] for r in self.conn.execute("PRAGMA table_info(watches)")}
        if "label" not in columns:
            self.conn.execute("ALTER TABLE watches ADD COLUMN label TEXT")
        self.conn.commit()

    # --- watches ---
    def add_watch(self, chat_id, ip, port, prefix, asn):
        self.conn.execute(
            "INSERT INTO watches(chat_id,ip,port,prefix,asn,added_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(chat_id,ip,port) DO UPDATE SET "
            "prefix=excluded.prefix,asn=excluded.asn,added_at=excluded.added_at",
            (chat_id, ip, port, prefix, asn, int(time.time())),
        )
        self.conn.commit()

    def remove_watch(self, chat_id, ip, port=None) -> int:
        if port is None:
            cur = self.conn.execute(
                "DELETE FROM watches WHERE chat_id=? AND ip=?", (chat_id, ip)
            )
        else:
            cur = self.conn.execute(
                "DELETE FROM watches WHERE chat_id=? AND ip=? AND port=?",
                (chat_id, ip, port),
            )
        self.conn.commit()
        return cur.rowcount

    def list_watches(self, chat_id):
        return self.conn.execute(
            "SELECT * FROM watches WHERE chat_id=? ORDER BY added_at", (chat_id,)
        ).fetchall()

    def get_label(self, chat_id, ip, port):
        row = self.conn.execute(
            "SELECT label FROM watches WHERE chat_id=? AND ip=? AND port=?",
            (chat_id, ip, port),
        ).fetchone()
        return row["label"] if row else None

    def set_label(self, chat_id, ip, label) -> int:
        cur = self.conn.execute(
            "UPDATE watches SET label=? WHERE chat_id=? AND ip=?",
            (label, chat_id, ip),
        )
        self.conn.commit()
        return cur.rowcount

    def count_watches(self, chat_id) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM watches WHERE chat_id=?", (chat_id,)
        ).fetchone()[0]

    def chats_for(self, ip, port):
        return [
            r["chat_id"]
            for r in self.conn.execute(
                "SELECT chat_id FROM watches WHERE ip=? AND port=?", (ip, port)
            ).fetchall()
        ]

    def distinct_targets(self):
        """Уникальные (ip,port,prefix,asn) среди всех подписок."""
        return self.conn.execute(
            "SELECT DISTINCT ip,port,prefix,asn FROM watches"
        ).fetchall()

    # --- state ---
    def get_state(self, ip, port):
        return self.conn.execute(
            "SELECT * FROM state WHERE ip=? AND port=?", (ip, port)
        ).fetchone()

    def ensure_state(self, ip, port, prefix, asn, tcp, seeing, total, now):
        """Засеять state только если строки (ip,port) ещё нет.

        Канонический baseline ведёт исключительно monitor-цикл (upsert_state).
        Интерактивный /watch не должен перезаписывать существующую строку: иначе
        чужая подписка на ту же (ip,port) могла бы сдвинуть baseline мимо реального
        перехода up/down и заглушить алерт другим подписчикам (cross-tenant).
        INSERT OR IGNORE — no-op при конфликте PRIMARY KEY (ip,port).
        """
        self.conn.execute(
            "INSERT OR IGNORE INTO state"
            "(ip,port,prefix,asn,tcp,bgp_seeing,bgp_total,last_change,last_check) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (ip, port, prefix, asn, tcp, seeing, total, now, now),
        )
        self.conn.commit()

    def upsert_state(self, ip, port, prefix, asn, tcp, seeing, total, changed, now):
        existing = self.get_state(ip, port)
        if changed or existing is None:
            last_change = now
        else:
            last_change = existing["last_change"]
        self.conn.execute(
            "INSERT OR REPLACE INTO state"
            "(ip,port,prefix,asn,tcp,bgp_seeing,bgp_total,last_change,last_check) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (ip, port, prefix, asn, tcp, seeing, total, last_change, now),
        )
        self.conn.commit()

    def close(self):
        self.conn.close()
