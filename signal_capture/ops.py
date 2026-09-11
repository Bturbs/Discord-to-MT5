"""Read audit records or explicitly clear a reconciled gate while all replicas are stopped."""
import argparse
import json


def main():
    from .__main__ import make_ledger
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["show", "release-gate"])
    parser.add_argument("--message-id")
    parser.add_argument("--expected-owner")
    parser.add_argument("--all-replicas-stopped-and-broker-reconciled", action="store_true")
    args = parser.parse_args()
    ledger = make_ledger()
    if args.action == "show":
        key = f"msg-{args.message_id}" if args.message_id else "execution-gate"
        print(json.dumps(ledger.get(key), indent=2))
        return
    if not args.all_replicas_stopped_and_broker_reconciled or not args.expected_owner:
        parser.error("Stop every replica, reconcile MT5 history, then supply the expected gate owner and acknowledgment flag")
    current = ledger.get("execution-gate")
    if not current or current["owner"] != args.expected_owner:
        parser.error("Gate owner does not match; no change made")
    ledger.delete("execution-gate")
    print("Gate released; message records retained to prevent replay")


if __name__ == "__main__":
    main()
