import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def identifier():
    return str(uuid4())


class Store:
    def __init__(self, connection):
        self.connection = connection

    def get(self, collection, key):
        row = self.connection.execute(
            "SELECT data FROM records WHERE collection = ? AND id = ?", (collection, key)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def all(self, collection):
        rows = self.connection.execute(
            "SELECT data FROM records WHERE collection = ? ORDER BY id", (collection,)
        )
        return [json.loads(row[0]) for row in rows]

    def save(self, collection, key, data):
        self.connection.execute(
            """INSERT INTO records (collection, id, data) VALUES (?, ?, ?)
            ON CONFLICT (collection, id) DO UPDATE SET data = excluded.data""",
            (collection, key, json.dumps(data, ensure_ascii=False)),
        )
        return data

    def remove(self, collection, key):
        self.connection.execute(
            "DELETE FROM records WHERE collection = ? AND id = ?", (collection, key)
        )

    def event(self, event_type, payload):
        event = {
            "event_type": event_type,
            "idempotency_key": identifier(),
            "occurred_at": timestamp(),
            "payload": payload,
        }
        return self.save("events", event["idempotency_key"], event)


class Database:
    def __init__(self, path):
        self.path = path
        with self.transaction() as store:
            store.connection.execute(
                """CREATE TABLE IF NOT EXISTS records (
                    collection TEXT NOT NULL,
                    id TEXT NOT NULL,
                    data TEXT NOT NULL,
                    PRIMARY KEY (collection, id)
                )"""
            )

    @contextmanager
    def transaction(self, *, write=True):
        connection = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield Store(connection)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
