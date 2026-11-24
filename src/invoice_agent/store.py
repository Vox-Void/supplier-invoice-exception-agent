"""PostgreSQL storage for fixture evidence, investigation leases and review decisions."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from .domain import Snapshot

Record = dict[str, Any]
DEFAULT_DSN = "postgresql://invoice_demo:invoice_demo_local@127.0.0.1:5414/invoice_agent"

# Keep parents ahead of child rows for the fixture loader.
SOURCE_KEYS = {
    "purchase_orders": ("source_id",),
    "invoices": ("source_id", "tenant_id", "po_id"),
    "po_lines": ("source_id", "po_id"),
    "invoice_lines": ("source_id", "invoice_id", "po_line_id"),
    "receipts": ("source_id", "po_line_id", "reverses_receipt_id"),
    "allocations": ("source_id", "invoice_id", "po_line_id"),
    "coverage": (),
}


class NotFound(Exception):
    """The requested resource is missing or outside the tenant."""


class Conflict(Exception):
    """The request does not match the stored idempotency key, lease or version."""


@dataclass(frozen=True)
class Reservation:
    status: Literal["claimed", "active", "complete"]
    run_id: str
    lease_token: str | None = None
    case_id: str | None = None


def canonical_digest(value: Record) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


class Store:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def connect(self) -> psycopg.Connection[Record]:
        return psycopg.connect(
            self.dsn, row_factory=dict_row, connect_timeout=5, options="-c timezone=UTC"
        )

    def migrate(self, schema: Path) -> None:
        with self.connect() as conn:
            conn.execute(schema.read_text(encoding="utf-8"))

    def seed_snapshot(self, snapshot: Snapshot) -> None:
        data = snapshot.model_dump(mode="json")
        digest = canonical_digest(data)
        with self.connect() as conn:
            inserted = conn.execute(
                """
                INSERT INTO snapshots VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (snapshot_id) DO NOTHING RETURNING snapshot_id
                """,
                (
                    snapshot.snapshot_id,
                    snapshot.fixture_version,
                    snapshot.policy_version,
                    snapshot.as_of_utc,
                    digest,
                ),
            ).fetchone()
            if inserted is None:
                stored = conn.execute(
                    "SELECT sha256 FROM snapshots WHERE snapshot_id = %s", (snapshot.snapshot_id,)
                ).fetchone()
                if stored is None or stored["sha256"] != digest:
                    raise Conflict("Snapshot content differs from the existing revision")
                return
            for table, keys in SOURCE_KEYS.items():
                columns = ("snapshot_id", "position", "payload", *keys)
                statement = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                    sql.Identifier(table),
                    sql.SQL(", ").join(map(sql.Identifier, columns)),
                    sql.SQL(", ").join(sql.Placeholder() for _ in columns),
                )
                with conn.cursor() as cursor:
                    cursor.executemany(
                        statement,
                        [
                            (
                                snapshot.snapshot_id,
                                position,
                                Jsonb(row),
                                *(row[key] for key in keys),
                            )
                            for position, row in enumerate(data[table])
                        ],
                    )

    def get_snapshot(self, tenant: str, snapshot_id: str) -> Snapshot:
        with self.connect() as conn:
            record = conn.execute(
                """
                SELECT snapshot_id, fixture_version, policy_version, as_of_utc, sha256
                FROM snapshots s
                WHERE s.snapshot_id = %s AND EXISTS (
                    SELECT 1 FROM invoices i WHERE i.snapshot_id = s.snapshot_id AND i.tenant_id = %s
                )
                """,
                (snapshot_id, tenant),
            ).fetchone()
            if record is None:
                raise NotFound("Snapshot not found")
            stored_digest = record.pop("sha256")
            for table in SOURCE_KEYS:
                rows = conn.execute(
                    sql.SQL(
                        "SELECT payload FROM {} WHERE snapshot_id = %s ORDER BY position"
                    ).format(sql.Identifier(table)),
                    (snapshot_id,),
                ).fetchall()
                record[table] = [row["payload"] for row in rows]
        try:
            snapshot = Snapshot.model_validate(record)
        except ValidationError as error:
            raise Conflict("Stored snapshot records do not match the fixture schema") from error
        if canonical_digest(snapshot.model_dump(mode="json")) != stored_digest:
            raise Conflict("Stored snapshot digest differs from its records")
        return snapshot

    def reserve(
        self,
        tenant: str,
        actor: str,
        key: str,
        request: Record,
        policy_version: str,
        lease_seconds: int = 45,
    ) -> Reservation:
        run_id = uuid4().hex
        token = uuid4().hex
        digest = canonical_digest(request)
        with self.connect() as conn:
            inserted = conn.execute(
                """
                INSERT INTO runs (
                    run_id, tenant_id, actor_id, idempotency_key, request_hash, request,
                    snapshot_id, invoice_id, policy_version, state, lease_token, lease_expires_at,
                    attempts
                )
                SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s, 'RUNNING', %s,
                       clock_timestamp() + %s * interval '1 second', 1
                FROM invoices WHERE snapshot_id = %s AND source_id = %s AND tenant_id = %s
                ON CONFLICT (tenant_id, idempotency_key) DO NOTHING RETURNING run_id
                """,
                (
                    run_id,
                    tenant,
                    actor,
                    key,
                    digest,
                    Jsonb(request),
                    request["snapshot_id"],
                    request["invoice_id"],
                    policy_version,
                    token,
                    lease_seconds,
                    request["snapshot_id"],
                    request["invoice_id"],
                    tenant,
                ),
            ).fetchone()
            if inserted is not None:
                return Reservation("claimed", run_id, token)

            # Retrying requests take a row lock before examining or replacing the lease.
            row = conn.execute(
                """
                SELECT run_id, request_hash, policy_version, state,
                       lease_expires_at > clock_timestamp() AS active
                FROM runs WHERE tenant_id = %s AND idempotency_key = %s FOR UPDATE
                """,
                (tenant, key),
            ).fetchone()
            if row is None:
                raise NotFound("Invoice not found")
            if row["request_hash"] != digest or row["policy_version"] != policy_version:
                raise Conflict("Idempotency key already belongs to a different investigation")
            existing_run = str(row["run_id"])
            if row["state"] == "COMPLETE":
                stored_case = conn.execute(
                    "SELECT case_id FROM cases WHERE tenant_id = %s AND run_id = %s",
                    (tenant, existing_run),
                ).fetchone()
                if stored_case is None:
                    raise RuntimeError("Completed investigation has no case")
                return Reservation("complete", existing_run, case_id=str(stored_case["case_id"]))
            if row["active"]:
                return Reservation("active", existing_run)
            conn.execute(
                """
                UPDATE runs SET state = 'RUNNING', lease_token = %s,
                    lease_expires_at = clock_timestamp() + %s * interval '1 second',
                    attempts = attempts + 1, reason = NULL
                WHERE tenant_id = %s AND run_id = %s
                """,
                (token, lease_seconds, tenant, existing_run),
            )
            return Reservation("claimed", existing_run, token)

    def find_active_or_complete(self, tenant: str, key: str, request: Record) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT run_id, request_hash, state = 'COMPLETE'
                    OR coalesce(lease_expires_at > clock_timestamp(), false) AS current
                FROM runs WHERE tenant_id = %s AND idempotency_key = %s
                """,
                (tenant, key),
            ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != canonical_digest(request):
            raise Conflict("Idempotency key already belongs to a different investigation")
        return str(row["run_id"]) if row["current"] else None

    def complete(self, tenant: str, run_id: str, lease_token: str, packet: Record) -> str:
        case_id = uuid4().hex
        with self.connect() as conn:
            # The guarded state update locks the lease until the case and audit event commit.
            run = conn.execute(
                """
                UPDATE runs SET state = 'COMPLETE', lease_token = NULL, lease_expires_at = NULL
                WHERE tenant_id = %s AND run_id = %s AND state = 'RUNNING'
                  AND lease_token = %s AND lease_expires_at > clock_timestamp()
                RETURNING actor_id
                """,
                (tenant, run_id, lease_token),
            ).fetchone()
            if run is None:
                raise Conflict("Investigation lease is no longer held")
            conn.execute(
                """
                INSERT INTO cases (case_id, tenant_id, run_id, status, version, packet)
                VALUES (%s, %s, %s, 'READY_FOR_REVIEW', 1, %s)
                """,
                (case_id, tenant, run_id, Jsonb(packet)),
            )
            conn.execute(
                """
                INSERT INTO audit_events (case_id, actor_id, event_type, version)
                VALUES (%s, %s, 'CASE_READY', 1)
                """,
                (case_id, run["actor_id"]),
            )
        return case_id

    def mark_retryable(self, tenant: str, run_id: str, lease_token: str, reason: str) -> None:
        with self.connect() as conn:
            result = conn.execute(
                """
                UPDATE runs SET state = 'RETRYABLE', lease_token = NULL,
                    lease_expires_at = NULL, reason = %s
                WHERE tenant_id = %s AND run_id = %s AND state = 'RUNNING'
                  AND lease_token = %s AND lease_expires_at > clock_timestamp()
                """,
                (reason, tenant, run_id, lease_token),
            )
            if result.rowcount == 0:
                raise Conflict("Investigation lease is no longer held")

    def get_run(self, tenant: str, run_id: str) -> Record:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT r.run_id,
                       CASE WHEN r.lease_expires_at <= statement_timestamp()
                            THEN 'RETRYABLE' ELSE r.state END AS state,
                       (r.state = 'RETRYABLE'
                            OR coalesce(r.lease_expires_at <= statement_timestamp(), false)) AS retryable,
                       CASE WHEN r.lease_expires_at <= statement_timestamp()
                            THEN 'LEASE_EXPIRED' ELSE r.reason END AS reason,
                       c.case_id, ARRAY(
                           SELECT other.run_id FROM runs other
                           WHERE other.tenant_id = r.tenant_id AND other.invoice_id = r.invoice_id
                             AND other.run_id <> r.run_id
                           ORDER BY other.created_at, other.run_id
                       ) AS related_run_ids
                FROM runs r LEFT JOIN cases c ON c.run_id = r.run_id
                WHERE r.tenant_id = %s AND r.run_id = %s
                """,
                (tenant, run_id),
            ).fetchone()
        if row is None:
            raise NotFound("Investigation not found")
        return row

    def list_cases(self, tenant: str, status: str) -> list[Record]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT case_id, run_id, status, version, packet FROM cases
                WHERE tenant_id = %s AND status = %s ORDER BY created_at, case_id
                """,
                (tenant, status),
            ).fetchall()

    def get_case(self, tenant: str, case_id: str) -> Record:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT case_id, run_id, status, version, packet FROM cases
                WHERE tenant_id = %s AND case_id = %s
                """,
                (tenant, case_id),
            ).fetchone()
        if row is None:
            raise NotFound("Case not found")
        return row

    def decide(
        self,
        tenant: str,
        actor: str,
        case_id: str,
        key: str,
        expected_version: int,
        decision: str,
        reason: str,
    ) -> Record:
        digest = canonical_digest(
            {
                "case_id": case_id,
                "actor": actor,
                "version": expected_version,
                "decision": decision,
                "reason": reason,
            }
        )
        # An identical decision retry returns the original response.
        with self.connect() as conn:
            row = conn.execute(
                "SELECT status, version FROM cases WHERE tenant_id = %s AND case_id = %s FOR UPDATE",
                (tenant, case_id),
            ).fetchone()
            if row is None:
                raise NotFound("Case not found")
            existing = conn.execute(
                """
                SELECT decision_id, case_id, decision, reason, version, request_hash
                FROM decisions WHERE tenant_id = %s AND idempotency_key = %s
                """,
                (tenant, key),
            ).fetchone()
            if existing is not None:
                if existing.pop("request_hash") != digest:
                    raise Conflict("Idempotency key already belongs to a different decision")
                return existing
            if row["status"] != "READY_FOR_REVIEW" or row["version"] != expected_version:
                raise Conflict("Case version or review state changed")

            version = expected_version + 1
            inserted = conn.execute(
                """
                INSERT INTO decisions (
                    decision_id, tenant_id, actor_id, case_id, idempotency_key, request_hash,
                    decision, reason, version
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
                RETURNING decision_id, case_id, decision, reason, version
                """,
                (uuid4().hex, tenant, actor, case_id, key, digest, decision, reason, version),
            ).fetchone()
            if inserted is None:
                raise Conflict("Idempotency key already belongs to a different decision")
            status = "APPROVED" if decision == "APPROVE_DISPOSITION" else "RETURNED_FOR_RESEARCH"
            conn.execute(
                "UPDATE cases SET status = %s, version = %s WHERE case_id = %s",
                (status, version, case_id),
            )
            conn.execute(
                """
                INSERT INTO audit_events (case_id, actor_id, version, event_type, decision_id)
                VALUES (%s, %s, %s, 'REVIEW_DECIDED', %s)
                """,
                (case_id, actor, version, inserted["decision_id"]),
            )
        return inserted
