"""Durable message claims plus a conservative, non-expiring account gate.

A crashed/uncertain execution leaves the gate locked for operator reconciliation.
There is deliberately no timed takeover that could race an in-flight order.
"""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from uuid import uuid4


class Busy(Exception):
    pass


class SQLiteLedger:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS records (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.commit()

    def create(self, key, value):
        try:
            with self.db:
                self.db.execute("INSERT INTO records VALUES (?,?)", (key, json.dumps(value)))
            return True
        except sqlite3.IntegrityError:
            return False

    def put(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO records VALUES (?,?)", (key, json.dumps(value)))

    def get(self, key):
        row = self.db.execute("SELECT value FROM records WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def delete(self, key):
        with self.db:
            self.db.execute("DELETE FROM records WHERE key=?", (key,))


class AzureLedger:
    def __init__(self, connection_string, partition, table="SignalLedger"):
        from azure.data.tables import TableServiceClient
        service = TableServiceClient.from_connection_string(connection_string)
        self.client = service.create_table_if_not_exists(table)
        self.partition = partition

    def entity(self, key, value):
        return {"PartitionKey": self.partition, "RowKey": key, "payload": json.dumps(value)}

    def create(self, key, value):
        from azure.core.exceptions import ResourceExistsError
        try:
            self.client.create_entity(self.entity(key, value))
            return True
        except ResourceExistsError:
            return False

    def put(self, key, value):
        self.client.upsert_entity(self.entity(key, value), mode="replace")

    def get(self, key):
        from azure.core.exceptions import ResourceNotFoundError
        try:
            return json.loads(self.client.get_entity(self.partition, key)["payload"])
        except ResourceNotFoundError:
            return None

    def delete(self, key):
        self.client.delete_entity(self.partition, key)


@contextmanager
def account_gate(ledger, message_id):
    token = uuid4().hex
    if not ledger.create("execution-gate", {"owner": token, "message_id": message_id}):
        raise Busy("Account gate locked; inspect the ledger before releasing it")
    # Any unexpected exception keeps the gate, including audit-write failures.
    yield
    current = ledger.get("execution-gate")
    if current and current["owner"] == token:
        ledger.delete("execution-gate")
