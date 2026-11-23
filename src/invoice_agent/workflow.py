"""Run an invoice investigation and hand its verified packet to the review queue."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from invoice_agent.domain import (
    MAX_TOOL_ROWS,
    AgentDraft,
    CasePacketV1,
    DraftError,
    EvidenceAccess,
    EvidenceResult,
    FixtureNotFound,
    Identifier,
    Invoice,
    InvoiceLine,
    POLine,
    PurchaseOrder,
    ScopeError,
    Tool,
    make_packet,
)
from invoice_agent.fixtures import FIXTURE_ROOT
from invoice_agent.store import Conflict, NotFound, Store

IdempotencyKey = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
Mode = Literal["recorded", "live"]
WorkflowMode = Literal["baseline", "recorded", "live"]
Role = Literal["requester", "reviewer"]

MAX_TOOL_CALLS = 8
MAX_SECONDS = 30
LEASE_SECONDS = 45
LOG = logging.getLogger(__name__)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InvestigationRequest(Contract):
    invoice_id: Identifier
    snapshot_id: Identifier
    mode: Mode = "recorded"


class RunView(Contract):
    run_id: str
    state: Literal["RUNNING", "RETRYABLE", "COMPLETE"]
    retryable: bool
    case_id: str | None = None
    related_run_ids: list[str] = Field(default_factory=list)
    reason: str | None = None


@dataclass(frozen=True)
class Actor:
    tenant_id: str
    actor_id: str
    role: Role


@dataclass(frozen=True)
class Submission:
    status_code: int
    body: RunView


class ServiceError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        run_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.run_id = run_id


class RetryableError(Exception):
    """Carry a stable reason from a failed replay or provider attempt."""


class BudgetExceeded(Exception):
    """Signal that an investigation reached a tool or time limit."""


class RecordedCall(Contract):
    tool: Tool
    arguments: dict[str, Any]
    digest: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class Recording(Contract):
    calls: Annotated[list[RecordedCall], Field(max_length=MAX_TOOL_CALLS)]
    draft: dict[str, Any]


@dataclass(frozen=True)
class DirectoryRecordings:
    root: Path = FIXTURE_ROOT / "recordings"

    def load(self, request: InvestigationRequest) -> Recording:
        path = self.root / f"{request.snapshot_id}__{request.invoice_id}.json"
        try:
            return Recording.model_validate_json(path.read_bytes())
        except FileNotFoundError as error:
            raise RetryableError("RECORDING_NOT_FOUND") from error
        except (OSError, ValidationError) as error:
            raise RetryableError("RECORDING_INVALID") from error


@dataclass(frozen=True)
class LiveSettings:
    enabled: bool = False
    model: str = ""
    token_budget: int = 48_000

    @classmethod
    def from_env(cls) -> LiveSettings:
        enabled = os.environ.get("INVOICE_AGENT_ENABLE_LIVE") == "true"
        if not enabled:
            return cls()
        try:
            token_budget = int(os.environ.get("INVOICE_AGENT_TOKEN_BUDGET", "48000"))
        except ValueError as error:
            raise ValueError("INVOICE_AGENT_TOKEN_BUDGET must be a whole number") from error
        if not 6_000 <= token_budget <= 250_000:
            raise ValueError("INVOICE_AGENT_TOKEN_BUDGET must be between 6000 and 250000")
        return cls(True, os.environ.get("INVOICE_AGENT_MODEL", "").strip(), token_budget)


class OrderArguments(Contract):
    po_id: Identifier


class LineArguments(Contract):
    po_line_ids: Annotated[list[Identifier], Field(min_length=1, max_length=MAX_TOOL_ROWS)]


class ToolSession:
    """Validate arguments and retain the results returned by accepted tools."""

    def __init__(self, access: EvidenceAccess, trace_id: str):
        self.access = access
        self.trace_id = trace_id
        self.evidence: list[EvidenceResult] = []
        self.deadline = monotonic() + MAX_SECONDS

    def check_time(self) -> None:
        if monotonic() >= self.deadline:
            raise BudgetExceeded("WALL_TIME_LIMIT")

    def call(self, tool: Tool, arguments: dict[str, Any]) -> EvidenceResult:
        self.check_time()
        if len(self.evidence) >= MAX_TOOL_CALLS:
            raise BudgetExceeded("TOOL_CALL_LIMIT")

        # Match the tool name to its argument schema and scoped reader.
        if tool == "read_invoice" and not arguments:
            result = self.access.read_invoice()
        elif tool == "find_duplicate_reference" and not arguments:
            result = self.access.find_duplicate_reference()
        elif tool == "read_purchase_order":
            result = self.access.read_purchase_order(OrderArguments.model_validate(arguments).po_id)
        elif tool in {"read_receipts", "read_prior_allocations"}:
            line_ids = LineArguments.model_validate(arguments).po_line_ids
            if tool == "read_receipts":
                result = self.access.read_receipts(line_ids)
            else:
                result = self.access.read_prior_allocations(line_ids)
        else:
            raise ScopeError("Invalid tool arguments")

        self.evidence.append(result)
        trace("tool_returned", trace_id=self.trace_id, tool=tool, digest=result.digest)
        self.check_time()
        return result


def trace(event: str, **fields: str | int) -> None:
    """Log a structured event containing IDs, counters and outcome codes."""
    LOG.info(json.dumps({"event": event, **fields}, sort_keys=True))


def configure_trace_logging() -> None:
    """Emit invoice trace events even when the server configures only its own logger."""
    if not LOG.hasHandlers():
        LOG.addHandler(logging.StreamHandler())
    LOG.setLevel(logging.INFO)


def collect_in_order(tools: ToolSession) -> None:
    invoice_records = tools.call("read_invoice", {}).records
    tools.call("find_duplicate_reference", {})
    invoice = next((row for row in invoice_records if isinstance(row, Invoice)), None)
    if invoice is None or invoice.po_id is None:
        return
    try:
        order_records = tools.call("read_purchase_order", {"po_id": invoice.po_id}).records
    except ScopeError:
        return
    order = next((row for row in order_records if isinstance(row, PurchaseOrder)), None)
    if order is None or order.supplier_id != invoice.supplier_id:
        return
    linked = {row.po_line_id for row in invoice_records if isinstance(row, InvoiceLine)}
    line_ids = sorted(
        row.source_id
        for row in order_records
        if isinstance(row, POLine) and row.eligible and row.source_id in linked
    )
    if line_ids:
        tools.call("read_receipts", {"po_line_ids": line_ids})
        tools.call("read_prior_allocations", {"po_line_ids": line_ids})


def replay(tools: ToolSession, recording: Recording) -> AgentDraft:
    for call in recording.calls:
        try:
            actual = tools.call(call.tool, call.arguments)
        except (ScopeError, ValidationError) as error:
            raise RetryableError("RECORDING_TOOL_REJECTED") from error
        if actual.digest != call.digest:
            trace(
                "recorded_digest_mismatch",
                trace_id=tools.trace_id,
                tool=call.tool,
                expected=call.digest,
                actual=actual.digest,
            )
            raise RetryableError("RECORDING_DIGEST_MISMATCH")
    return AgentDraft.model_validate(recording.draft)


def run_workflow(
    access: EvidenceAccess,
    *,
    mode: WorkflowMode,
    recording: Recording | None = None,
    live: Callable[[ToolSession], AgentDraft] | None = None,
    trace_id: str | None = None,
) -> CasePacketV1:
    tools = ToolSession(access, trace_id or uuid4().hex)
    trace(
        "workflow_started",
        trace_id=tools.trace_id,
        mode=mode,
        invoice_id=access.invoice_id,
        snapshot_id=access.snapshot.snapshot_id,
    )

    # Build the packet only from evidence already returned through this session.
    try:
        if mode == "baseline":
            collect_in_order(tools)
            packet = make_packet(access, tools.evidence, tools.trace_id)
        else:
            if mode == "recorded" and recording is not None:
                draft = replay(tools, recording)
            elif mode == "live" and live is not None:
                draft = live(tools)
            else:
                raise RetryableError("ADAPTER_NOT_CONFIGURED")
            tools.check_time()
            packet = make_packet(access, tools.evidence, tools.trace_id, draft=draft)
    except BudgetExceeded as error:
        packet = make_packet(access, tools.evidence, tools.trace_id, validator_reason=str(error))
    except (DraftError, ValidationError, ScopeError):
        packet = make_packet(
            access, tools.evidence, tools.trace_id, validator_reason="INVALID_DRAFT"
        )

    trace(
        "workflow_finished",
        trace_id=tools.trace_id,
        outcome=packet.status,
        validator=packet.validator_decision,
        tool_calls=len(tools.evidence),
    )
    return packet


class InvestigationService:
    """Reserve a run, investigate outside a transaction and persist the result."""

    def __init__(self, store: Store, recordings: DirectoryRecordings, live_settings: LiveSettings):
        self.store = store
        self.recordings = recordings
        self.live_settings = live_settings

    def submit(
        self,
        actor: Actor,
        request: InvestigationRequest,
        key: str,
        *,
        baseline: bool = False,
    ) -> Submission:
        if actor.role != "requester":
            raise ServiceError(403, "ROLE_FORBIDDEN", "A requester must start an investigation.")

        mode: WorkflowMode = "baseline" if baseline else request.mode
        tenant = actor.tenant_id
        canonical = request.model_dump(mode="json")
        canonical["mode"] = mode
        try:
            previous = self.store.find_active_or_complete(tenant, key, canonical)
        except Conflict as error:
            raise ServiceError(
                409, "IDEMPOTENCY_CONFLICT", "The key belongs to a different request."
            ) from error
        if previous is not None:
            return self._existing(tenant, previous)

        live = self._live_adapter() if mode == "live" else None
        try:
            snapshot = self.store.get_snapshot(tenant, request.snapshot_id)
            access = EvidenceAccess(snapshot, tenant, request.invoice_id)
        except (FixtureNotFound, NotFound) as error:
            raise ServiceError(
                404, "NOT_FOUND", "The snapshot or invoice was not found."
            ) from error
        except Conflict as error:
            raise ServiceError(
                503, "SNAPSHOT_UNAVAILABLE", "The snapshot failed its integrity check."
            ) from error

        try:
            reservation = self.store.reserve(
                tenant,
                actor.actor_id,
                key,
                canonical,
                snapshot.policy_version,
                LEASE_SECONDS,
            )
        except Conflict as error:
            raise ServiceError(
                409, "IDEMPOTENCY_CONFLICT", "The key belongs to a different request."
            ) from error
        except NotFound as error:
            raise ServiceError(
                404, "NOT_FOUND", "The snapshot or invoice was not found."
            ) from error
        if reservation.status != "claimed":
            return self._existing(tenant, reservation.run_id)
        token = reservation.lease_token
        if token is None:
            raise RuntimeError("Claimed investigation has no lease token")

        run_id = reservation.run_id
        trace_id = uuid4().hex
        trace(
            "investigation_attempt",
            run_id=run_id,
            trace_id=trace_id,
            mode=mode,
            invoice_id=request.invoice_id,
            snapshot_id=request.snapshot_id,
        )
        try:
            recording = self.recordings.load(request) if mode == "recorded" else None
            packet = run_workflow(
                access, mode=mode, recording=recording, live=live, trace_id=trace_id
            )
            case_id = self.store.complete(tenant, run_id, token, packet.model_dump(mode="json"))
        except RetryableError as error:
            reason = str(error)
            trace("attempt_retryable", run_id=run_id, trace_id=trace_id, mode=mode, reason=reason)
            try:
                self.store.mark_retryable(tenant, run_id, token, reason)
            except Conflict:
                return self._existing(tenant, run_id)
            raise ServiceError(
                503, reason, "The investigation can be retried with the same key.", run_id=run_id
            ) from error
        except Conflict:
            trace("attempt_superseded", run_id=run_id, trace_id=trace_id)
            return self._existing(tenant, run_id)
        trace("case_committed", run_id=run_id, trace_id=trace_id, case_id=case_id)
        return Submission(201, RunView.model_validate(self.store.get_run(tenant, run_id)))

    def _existing(self, tenant: str, run_id: str) -> Submission:
        run = RunView.model_validate(self.store.get_run(tenant, run_id))
        if run.state == "RETRYABLE":
            raise ServiceError(
                503,
                run.reason or "INVESTIGATION_RETRYABLE",
                "The investigation can be retried with the same key.",
                run_id=run_id,
            )
        return Submission(200 if run.state == "COMPLETE" else 202, run)

    def _live_adapter(self) -> Callable[[ToolSession], AgentDraft]:
        settings = self.live_settings
        if not settings.enabled or not settings.model:
            raise ServiceError(
                422, "LIVE_NOT_CONFIGURED", "Live mode is unavailable on this server."
            )
        key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not key:
            raise ServiceError(
                422, "LIVE_NOT_CONFIGURED", "Live mode is unavailable on this server."
            )

        # Import the provider SDK adapter only for an explicitly enabled live run.
        from invoice_agent.live import OpenAIAdapter

        return OpenAIAdapter(settings, key)
