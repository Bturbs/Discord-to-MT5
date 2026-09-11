from dataclasses import dataclass
import logging
import time

from .config import SOURCE_BOT_ID
from .execution import Rejected
from .ledger import account_gate
from .parser import parse_signal

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Event:
    message_id: str
    author_id: int
    channel_id: int
    created_at: float
    text: str
    webhook: bool = False


class Service:
    def __init__(self, config, executor, ledger):
        self.config, self.executor, self.ledger = config, executor, ledger

    def process(self, event):
        if event.author_id != SOURCE_BOT_ID or event.channel_id not in self.config.channels or event.webhook:
            return "ignored"
        key = f"msg-{event.message_id}"
        if self.ledger.get(key):
            return "duplicate"
        with account_gate(self.ledger, event.message_id):
            record = {"status": "CLAIMED", "message_id": event.message_id, "channel_id": str(event.channel_id),
                      "created_at": event.created_at, "recorded_at": time.time(), "dry_run": self.config.dry_run}
            if not self.ledger.create(key, record):
                return "duplicate"
            try:
                age = time.time() - event.created_at
                if not -2 <= age <= self.config.max_signal_age_seconds:
                    raise Rejected("Signal is stale or future-dated")
                try:
                    signal = parse_signal(event.text)
                except ValueError as exc:
                    raise Rejected(str(exc)) from exc
                channel = self.config.channels[event.channel_id]
                plan = self.executor.plan(signal, channel, event.message_id)
                record["plan"] = plan
                if time.time() - event.created_at > self.config.max_signal_age_seconds:
                    raise Rejected("Signal expired during validation")
                if self.config.dry_run:
                    record["status"] = "DRY_RUN"
                else:
                    record["status"] = "SUBMITTING"
                    self.ledger.put(key, record)
                    record["result"] = self.executor.send(plan, channel)
                    record["status"] = "EXECUTED"
            except Rejected as exc:
                record.update(status="REJECTED", reason=str(exc))
            except Exception:
                # Preserve the last durable state and gate. Logging is in caller;
                # avoid masking the original error with another storage call.
                raise
            self.ledger.put(key, record)
            log.info("message=%s status=%s detail=%s", event.message_id, record["status"], record.get("reason", ""))
            return record["status"]
