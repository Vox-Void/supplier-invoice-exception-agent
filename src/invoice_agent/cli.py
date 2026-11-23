"""Load fixture data, exercise the review API and grade investigation outcomes."""

import argparse
import json
import logging
import os
import platform
import sys
from importlib.metadata import version
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from fastapi.testclient import TestClient

from .api import DemoIdentity, create_app
from .fixtures import load_goldens, load_snapshots
from .store import DEFAULT_DSN, Record, Store
from .workflow import (
    Actor,
    DirectoryRecordings,
    InvestigationRequest,
    InvestigationService,
    LiveSettings,
    Mode,
    ServiceError,
)

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / "artifacts"
HEADLINE = {"snapshot_id": "retail-demo-v1", "invoice_id": "INV-060", "mode": "recorded"}


def save_report(name: str, report: Record) -> None:
    ARTIFACTS.mkdir(exist_ok=True)
    target = ARTIFACTS / name
    target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Report saved to {target.relative_to(ROOT)}")


def environment(store: Store) -> Record:
    with store.connect() as conn:
        row = conn.execute("SELECT current_setting('server_version') AS version").fetchone()
    return {
        "app_commit": os.environ.get("GITHUB_SHA"),
        "python": platform.python_version(),
        "postgresql": row["version"] if row else None,
        "agent_sdk": version("openai-agents"),
    }


def bootstrap(store: Store) -> None:
    store.migrate(ROOT / "db" / "schema.sql")
    snapshots = load_snapshots()
    for snapshot in snapshots:
        store.seed_snapshot(snapshot)
    print(f"PostgreSQL contains {len(snapshots)} fixture snapshots.")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def audit_history(store: Store, case_id: str) -> list[str]:
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT event_type FROM audit_events WHERE case_id = %s ORDER BY version", (case_id,)
        ).fetchall()
    return [str(row["event_type"]) for row in rows]


def demo(store: Store) -> None:
    identities = DemoIdentity.from_env()
    request_headers = {
        "Authorization": f"Bearer {identities.requester_token}",
        "Idempotency-Key": f"demo-{uuid4().hex}",
    }
    review_headers = {"Authorization": f"Bearer {identities.reviewer_token}"}
    with TestClient(create_app(identity=identities)) as client:
        created = client.post("/v1/investigations", json=HEADLINE, headers=request_headers)
        created.raise_for_status()
        require(created.status_code == 201, "The first request must create a case")
        case_id = created.json()["case_id"]
        retry = client.post("/v1/investigations", json=HEADLINE, headers=request_headers)
        retry.raise_for_status()
        require(
            retry.status_code == 200 and retry.json()["case_id"] == case_id,
            "Retry must return the same stored case",
        )

    # A new application instance reads the original case through the review API.
    with TestClient(create_app(identity=identities)) as restarted:
        response = restarted.get(f"/v1/cases/{case_id}", headers=review_headers)
        response.raise_for_status()
        case = response.json()
        require(case["status"] == "READY_FOR_REVIEW", "The queue must contain the saved draft")
        require(case["packet"]["status"] == "EXCEPTION", "The headline must be an exception")
        require(case["packet"]["next_action"] == "REQUEST_RECEIPT", "Expected a receipt request")
        comparison = case["packet"]["comparisons"][0]
        require(
            [
                comparison[field]
                for field in (
                    "requested_quantity",
                    "received_quantity",
                    "allocated_quantity",
                    "available_quantity",
                    "shortfall_quantity",
                )
            ]
            == [60, 80, 30, 50, 10],
            "Headline quantities differ from the expected result",
        )
        queue = restarted.get("/v1/cases?status=READY_FOR_REVIEW", headers=review_headers)
        queue.raise_for_status()
        require(any(row["case_id"] == case_id for row in queue.json()), "Case missing from queue")
        decision_headers = {**review_headers, "Idempotency-Key": f"review-{uuid4().hex}"}
        decision_body = {
            "expected_version": case["version"],
            "decision": "APPROVE_DISPOSITION",
            "reason": "Confirm the remaining receipt before processing the invoice.",
        }
        decision = restarted.post(
            f"/v1/cases/{case_id}/decisions",
            headers=decision_headers,
            json=decision_body,
        )
        decision.raise_for_status()
        repeated = restarted.post(
            f"/v1/cases/{case_id}/decisions",
            headers=decision_headers,
            json=decision_body,
        )
        require(
            repeated.status_code == 200 and repeated.json() == decision.json(),
            "Decision replay must return the original decision",
        )
    events = audit_history(store, case_id)
    require(events == ["CASE_READY", "REVIEW_DECIDED"], "Expected one event for each transition")
    print("Invoice requests 60 units. 50 are available, leaving a shortfall of 10.")
    print("The same case survived the API restart. Repeating the review saved one decision.")
    save_report(
        "demo.json",
        {
            "mode": "recorded",
            "runtime": environment(store),
            "first_status": created.status_code,
            "retry_status": retry.status_code,
            "case_before_review": case,
            "decision": decision.json(),
            "audit_events": events,
        },
    )


def evaluate(store: Store, mode: str) -> bool:
    service = InvestigationService(store, DirectoryRecordings(), LiveSettings.from_env())
    arms = ("baseline", mode)
    results: list[Record] = []
    for golden in load_goldens():
        if mode == "live" and golden.case_id != "headline":
            continue
        for arm in arms:
            input_mode: Mode = "live" if arm == "live" else "recorded"
            request = InvestigationRequest(
                snapshot_id=golden.snapshot_id,
                invoice_id=golden.invoice_id,
                mode=input_mode,
            )
            actor = Actor(golden.tenant_id, "evaluation", "requester")
            start = perf_counter()
            result: Record = {"case": golden.case_id, "split": golden.split, "mode": arm}
            try:
                run = service.submit(
                    actor, request, f"evaluation-{uuid4().hex}", baseline=arm == "baseline"
                )
                if run.body.case_id is None:
                    raise RuntimeError("Evaluation did not return a completed case")
                case = store.get_case(actor.tenant_id, run.body.case_id)
                packet = case["packet"]
                expected = golden.model_dump(mode="json")
                if arm == "baseline" or arm == "live":
                    expected["validator_decision"] = "VALIDATED"
                elif expected["validator_decision"] == "ESCALATED":
                    expected.update(
                        status="INSUFFICIENT_EVIDENCE",
                        next_action="RESEARCH_MISSING_EVIDENCE",
                        findings=[],
                        comparisons={},
                    )
                actual_comparisons = {row["po_line_id"]: row for row in packet["comparisons"]}
                checks = {
                    "status": packet["status"] == expected["status"],
                    "action": packet["next_action"] == expected["next_action"],
                    "findings": packet["findings"] == expected["findings"],
                    "validator": packet["validator_decision"] == expected["validator_decision"],
                    "comparisons": all(
                        po_line in actual_comparisons
                        and all(
                            actual_comparisons[po_line][field] == value
                            for field, value in fields.items()
                        )
                        for po_line, fields in expected["comparisons"].items()
                    ),
                    "persistence": case["status"] == "READY_FOR_REVIEW"
                    and case["version"] == 1
                    and audit_history(store, case["case_id"]) == ["CASE_READY"],
                }
                result.update(
                    matched=all(checks.values()),
                    checks=checks,
                    status=packet["status"],
                    expected_status=expected["status"],
                    case_id=case["case_id"],
                    run_id=run.body.run_id,
                    trace_id=packet["trace_id"],
                    evidence_tools=len(packet["evidence"]),
                    fixture_version=packet["fixture_version"],
                    policy_version=packet["policy_version"],
                )
            except ServiceError as error:
                result.update(matched=False, error=error.code, run_id=error.run_id)
            result["elapsed_ms"] = round((perf_counter() - start) * 1000, 2)
            results.append(result)
    summary = {}
    for arm in arms:
        rows = [row for row in results if row["mode"] == arm]
        summary[arm] = {
            "cases": len(rows),
            "matched": sum(row["matched"] for row in rows),
            "false_clean_matches": sum(
                row.get("status") == "MATCHED" and row.get("expected_status") != "MATCHED"
                for row in rows
            ),
        }
        print(f"{arm}: {summary[arm]['matched']}/{summary[arm]['cases']} cases matched")
    save_report(
        f"evaluation-{mode}.json",
        {
            "version": 1,
            "runtime": environment(store),
            "scores": summary,
            "results": results,
        },
    )
    return bool(results) and all(row["matched"] for row in results)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("bootstrap")
    commands.add_parser("demo")
    evaluation = commands.add_parser("eval")
    evaluation.add_argument("--mode", choices=("recorded", "live"), default="recorded")
    commands.add_parser("fixtures")
    args = parser.parse_args()
    store = Store(os.environ.get("INVOICE_AGENT_DSN", DEFAULT_DSN))
    if args.command == "fixtures":
        from .fixtures import main as fixture_main

        fixture_main()
    elif args.command == "bootstrap":
        bootstrap(store)
    elif args.command == "demo":
        demo(store)
    else:
        return 0 if evaluate(store, args.mode) else 1
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    raise SystemExit(main())
