"""Read scoped invoice evidence and apply the fictional matching policy."""

import json
import re
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from hashlib import sha256
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    model_validator,
)

POLICY_VERSION = "invoice-match-v1"
MAX_TOOL_ROWS = 50
CENT = Decimal("0.01")

Status = Literal["MATCHED", "EXCEPTION", "INSUFFICIENT_EVIDENCE"]
Action = Literal[
    "REQUEST_RECEIPT",
    "ASK_BUYER_TO_CONFIRM_PO",
    "REQUEST_CORRECTED_INVOICE",
    "INVESTIGATE_DUPLICATE",
    "RESEARCH_MISSING_EVIDENCE",
    "REVIEW_MATCH",
]
ReasonCode = Literal[
    "MISSING_EVIDENCE",
    "DISPUTED_IDENTITY",
    "MISSING_LINKAGE",
    "AMBIGUOUS_LINKAGE",
    "UNSUPPORTED_CURRENCY",
    "UNSUPPORTED_UNIT",
    "DUPLICATE_REFERENCE",
    "ORDER_SHORTFALL",
    "RECEIPT_SHORTFALL",
    "UNIT_PRICE_VARIANCE",
    "LINE_NET_VARIANCE",
]
RecordKind = Literal[
    "invoice", "invoice_line", "purchase_order", "po_line", "receipt", "allocation"
]
Source = Literal["invoice", "purchase_order", "receipt", "allocation", "duplicate"]
Tool = Literal[
    "read_invoice",
    "read_purchase_order",
    "read_receipts",
    "read_prior_allocations",
    "find_duplicate_reference",
]
Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")]
Quantity = Annotated[int, Field(strict=True, ge=0, le=1_000_000_000)]


class FixtureNotFound(ValueError):
    """The named fixture or invoice is unavailable in the requested tenant."""


class ScopeError(ValueError):
    """The requested tool argument is outside this investigation's scope."""


class DraftError(ValueError):
    """The agent's draft disagrees with the facts its tools returned."""


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def parse_decimal_amount(value: object, maximum: Decimal) -> Decimal:
    if isinstance(value, str) and re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", value):
        value = Decimal(value)
    exponent = value.as_tuple().exponent if isinstance(value, Decimal) else None
    if (
        not isinstance(value, Decimal)
        or not value.is_finite()
        or value < 0
        or value > maximum
        or not isinstance(exponent, int)
        or exponent < -2
    ):
        raise ValueError("Expected a nonnegative decimal string with at most two decimal places.")
    return value.quantize(CENT)


def parse_money(value: object) -> Decimal:
    return parse_decimal_amount(value, Decimal("9999999999999999.99"))


def parse_delta(value: object) -> Decimal:
    return parse_decimal_amount(value, Decimal("99999999999999999999999999.99"))


def require_utc(value: datetime) -> datetime:
    if value.utcoffset() != timedelta(0):
        raise ValueError("Timestamp must be UTC.")
    return value


Money = Annotated[
    Decimal,
    BeforeValidator(parse_money, json_schema_input_type=str),
    PlainSerializer(str, return_type=str, when_used="json"),
]
CalculatedMoney = Annotated[
    Decimal,
    BeforeValidator(parse_delta, json_schema_input_type=str),
    PlainSerializer(str, return_type=str, when_used="json"),
]
UtcTime = Annotated[datetime, AfterValidator(require_utc)]


def normalize_reference(value: str) -> str:
    return value.strip().casefold()


class SourceRef(FrozenModel):
    kind: RecordKind
    source_id: Identifier
    revision: Annotated[int, Field(strict=True, gt=0)]


class Invoice(SourceRef):
    kind: Literal["invoice"] = "invoice"
    tenant_id: Identifier
    supplier_id: Identifier
    reference: Annotated[str, Field(min_length=1, max_length=128)]
    status: Literal["OPEN", "APPROVED", "VOID"]
    po_id: Identifier | None
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    event_at: UtcTime
    recorded_at: UtcTime
    memo: Annotated[str, Field(max_length=2000)] = ""


class InvoiceLine(SourceRef):
    kind: Literal["invoice_line"] = "invoice_line"
    invoice_id: Identifier
    po_line_id: Identifier | None
    candidate_po_line_ids: tuple[Identifier, ...] = ()
    quantity: Quantity
    unit: Identifier
    unit_price: Money
    net_amount: Money


class PurchaseOrder(SourceRef):
    kind: Literal["purchase_order"] = "purchase_order"
    tenant_id: Identifier
    supplier_id: Identifier
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]


class POLine(SourceRef):
    kind: Literal["po_line"] = "po_line"
    po_id: Identifier
    quantity: Quantity
    unit: Identifier
    unit_price: Money
    eligible: bool = True


class Receipt(SourceRef):
    kind: Literal["receipt"] = "receipt"
    po_line_id: Identifier
    quantity: Quantity
    event_type: Literal["RECEIPT", "REVERSAL"]
    posted: bool
    event_at: UtcTime
    recorded_at: UtcTime
    reverses_receipt_id: Identifier | None = None


class Allocation(SourceRef):
    kind: Literal["allocation"] = "allocation"
    invoice_id: Identifier
    po_line_id: Identifier
    quantity: Quantity
    status: Literal["APPROVED", "PENDING", "VOID"]
    event_at: UtcTime
    recorded_at: UtcTime


class Coverage(FrozenModel):
    source: Source
    scope_id: Identifier
    complete: bool
    through_utc: UtcTime | None

    def covers(self, cutoff: datetime) -> bool:
        return self.complete and self.through_utc is not None and self.through_utc >= cutoff


EvidenceRecord = Annotated[
    Invoice | InvoiceLine | PurchaseOrder | POLine | Receipt | Allocation,
    Field(discriminator="kind"),
]


class Snapshot(FrozenModel):
    snapshot_id: Identifier
    fixture_version: Identifier
    policy_version: Literal["invoice-match-v1"]
    as_of_utc: UtcTime
    invoices: tuple[Invoice, ...]
    invoice_lines: tuple[InvoiceLine, ...]
    purchase_orders: tuple[PurchaseOrder, ...]
    po_lines: tuple[POLine, ...]
    receipts: tuple[Receipt, ...]
    allocations: tuple[Allocation, ...]
    coverage: tuple[Coverage, ...]

    @model_validator(mode="after")
    def check_keys(self) -> "Snapshot":
        for records in (
            self.invoices,
            self.invoice_lines,
            self.purchase_orders,
            self.po_lines,
            self.receipts,
            self.allocations,
        ):
            if len({record.source_id for record in records}) != len(records):
                raise ValueError("Source IDs must be unique within each snapshot table.")
        if len({(entry.source, entry.scope_id) for entry in self.coverage}) != len(self.coverage):
            raise ValueError("Source coverage keys must be unique.")
        invoices = {item.source_id for item in self.invoices}
        pos = {item.source_id for item in self.purchase_orders}
        po_lines = {item.source_id for item in self.po_lines}
        for invoice in self.invoices:
            if invoice.po_id is not None and invoice.po_id not in pos:
                raise ValueError("Invoice must refer to a known purchase order.")
        for line in self.invoice_lines:
            linked = (line.po_line_id,) if line.po_line_id else ()
            if (
                line.invoice_id not in invoices
                or not set((*linked, *line.candidate_po_line_ids)) <= po_lines
            ):
                raise ValueError("Invoice line must refer to known invoice and PO records.")
        if any(line.po_id not in pos for line in self.po_lines):
            raise ValueError("PO line must refer to a known purchase order.")
        receipt_by_id = {item.source_id: item for item in self.receipts}
        for receipt in self.receipts:
            if receipt.po_line_id not in po_lines:
                raise ValueError("Receipt must refer to a known PO line.")
            if receipt.event_type == "REVERSAL":
                parent = receipt_by_id.get(receipt.reverses_receipt_id or "")
                if (
                    not parent
                    or parent.event_type != "RECEIPT"
                    or parent.po_line_id != receipt.po_line_id
                ):
                    raise ValueError("Reversal must refer to a receipt on the same PO line.")
            elif receipt.reverses_receipt_id is not None:
                raise ValueError("A receipt cannot reference a reversal target.")
        if any(
            row.invoice_id not in invoices or row.po_line_id not in po_lines
            for row in self.allocations
        ):
            raise ValueError("Allocation must refer to a known invoice and PO line.")
        return self


def source_ref(record: SourceRef) -> SourceRef:
    return SourceRef(kind=record.kind, source_id=record.source_id, revision=record.revision)


def visible(record: Invoice | Receipt | Allocation, cutoff: datetime) -> bool:
    return record.event_at <= cutoff and record.recorded_at <= cutoff


class EvidenceResult(FrozenModel):
    tool: Tool
    arguments: dict[str, str | tuple[str, ...]]
    snapshot_id: Identifier
    tenant_id: Identifier
    invoice_id: Identifier
    as_of_utc: UtcTime
    records: tuple[EvidenceRecord, ...]
    coverage: tuple[Coverage, ...]

    @property
    def digest(self) -> str:
        document = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return sha256(document.encode("utf-8")).hexdigest()


class EvidenceAccess:
    """Return evidence scoped to one visible invoice and its linked PO."""

    def __init__(self, snapshot: Snapshot, tenant_id: str, invoice_id: str) -> None:
        self.snapshot = snapshot
        self.tenant_id = tenant_id
        self.invoice_id = invoice_id
        invoices = {
            row.source_id: row
            for row in snapshot.invoices
            if row.tenant_id == tenant_id and visible(row, snapshot.as_of_utc)
        }
        if invoice_id not in invoices:
            raise FixtureNotFound("Invoice unavailable in this snapshot.")
        self._invoices = invoices
        self._invoice = invoices[invoice_id]
        self._po = next(
            (
                row
                for row in snapshot.purchase_orders
                if row.source_id == self._invoice.po_id and row.tenant_id == tenant_id
            ),
            None,
        )
        self._allowed_lines = {
            row.source_id
            for row in snapshot.po_lines
            if self._po
            and self._po.supplier_id == self._invoice.supplier_id
            and row.po_id == self._po.source_id
            and row.eligible
        }

    def _coverage(self, source: Source, scope_ids: Sequence[str]) -> tuple[Coverage, ...]:
        stored = {row.scope_id: row for row in self.snapshot.coverage if row.source == source}
        return tuple(
            stored.get(
                scope_id,
                Coverage(source=source, scope_id=scope_id, complete=False, through_utc=None),
            )
            for scope_id in scope_ids
        )

    def _result(
        self,
        tool: Tool,
        arguments: dict[str, str | tuple[str, ...]],
        records: Sequence[EvidenceRecord],
        coverage: tuple[Coverage, ...],
    ) -> EvidenceResult:
        if len(records) > MAX_TOOL_ROWS:
            coverage = tuple(row.model_copy(update={"complete": False}) for row in coverage)
        return EvidenceResult(
            tool=tool,
            arguments=arguments,
            records=tuple(records[:MAX_TOOL_ROWS]),
            coverage=coverage,
            snapshot_id=self.snapshot.snapshot_id,
            tenant_id=self.tenant_id,
            invoice_id=self.invoice_id,
            as_of_utc=self.snapshot.as_of_utc,
        )

    def _line_ids(self, po_line_ids: list[str]) -> tuple[str, ...]:
        ids = tuple(sorted(set(po_line_ids)))
        if not ids or len(po_line_ids) > MAX_TOOL_ROWS or not set(ids) <= self._allowed_lines:
            raise ScopeError("PO line request is outside this invoice's scope.")
        return ids

    def read_invoice(self) -> EvidenceResult:
        lines = sorted(
            (row for row in self.snapshot.invoice_lines if row.invoice_id == self.invoice_id),
            key=lambda row: row.source_id,
        )
        return self._result(
            "read_invoice",
            {},
            [self._invoice, *lines],
            self._coverage("invoice", [self.invoice_id]),
        )

    def read_purchase_order(self, po_id: str) -> EvidenceResult:
        if self._po is None or normalize_reference(po_id) != normalize_reference(
            self._po.source_id
        ):
            raise ScopeError("Purchase order unavailable in this invoice's scope.")
        lines = sorted(
            (row for row in self.snapshot.po_lines if row.po_id == self._po.source_id),
            key=lambda row: row.source_id,
        )
        return self._result(
            "read_purchase_order",
            {"po_id": self._po.source_id},
            [self._po, *lines],
            self._coverage("purchase_order", [self._po.source_id]),
        )

    def read_receipts(self, po_line_ids: list[str]) -> EvidenceResult:
        ids = self._line_ids(po_line_ids)
        records = sorted(
            (
                row
                for row in self.snapshot.receipts
                if row.po_line_id in ids and row.posted and visible(row, self.snapshot.as_of_utc)
            ),
            key=lambda row: row.source_id,
        )
        # A reversal without its visible original leaves that line's receipt count unresolved.
        posted = {row.source_id: row.quantity for row in records if row.event_type == "RECEIPT"}
        reversed_units: Counter[str] = Counter()
        incomplete = set()
        for row in records:
            if row.event_type == "REVERSAL":
                original = row.reverses_receipt_id or ""
                reversed_units[original] += row.quantity
                if original not in posted or reversed_units[original] > posted[original]:
                    incomplete.add(row.po_line_id)
        coverage = tuple(
            row.model_copy(update={"complete": False}) if row.scope_id in incomplete else row
            for row in self._coverage("receipt", ids)
        )
        return self._result("read_receipts", {"po_line_ids": ids}, records, coverage)

    def read_prior_allocations(self, po_line_ids: list[str]) -> EvidenceResult:
        ids = self._line_ids(po_line_ids)
        earlier_ids = {
            row.source_id
            for row in self._invoices.values()
            if row.supplier_id == self._invoice.supplier_id
            and row.status != "VOID"
            and row.source_id != self.invoice_id
        }
        records = sorted(
            (
                row
                for row in self.snapshot.allocations
                if row.po_line_id in ids
                and row.invoice_id in earlier_ids
                and row.status == "APPROVED"
                and visible(row, self.snapshot.as_of_utc)
            ),
            key=lambda row: row.source_id,
        )
        return self._result(
            "read_prior_allocations",
            {"po_line_ids": ids},
            records,
            self._coverage("allocation", ids),
        )

    def find_duplicate_reference(self) -> EvidenceResult:
        reference = normalize_reference(self._invoice.reference)
        records = sorted(
            (
                row
                for row in self._invoices.values()
                if reference
                and self._invoice.status != "VOID"
                and row.supplier_id == self._invoice.supplier_id
                and row.status != "VOID"
                and row.source_id != self.invoice_id
                and normalize_reference(row.reference) == reference
            ),
            key=lambda row: row.source_id,
        )
        return self._result(
            "find_duplicate_reference", {}, records, self._coverage("duplicate", [self.invoice_id])
        )


class Finding(FrozenModel):
    code: ReasonCode
    invoice_line_id: Identifier | None = None
    po_line_id: Identifier | None = None
    quantity_delta: Annotated[int, Field(strict=True, gt=0)] | None = None
    amount_delta: CalculatedMoney | None = None
    citations: tuple[SourceRef, ...]


class AgentDraft(FrozenModel):
    status: Status
    findings: tuple[Finding, ...]
    next_action: Action


class PriceComparison(FrozenModel):
    invoice_line_id: Identifier
    invoice_unit_price: Money
    po_unit_price: Money
    unit_price_delta: CalculatedMoney
    invoice_net_amount: Money
    expected_net_amount: CalculatedMoney
    net_amount_delta: CalculatedMoney


class LineComparison(FrozenModel):
    po_line_id: Identifier
    invoice_line_ids: tuple[Identifier, ...]
    requested_quantity: int | None
    ordered_quantity: int
    received_quantity: int | None
    allocated_quantity: int | None
    remaining_ordered: int | None
    remaining_received: int | None
    available_quantity: int | None
    shortfall_quantity: int | None
    prices: tuple[PriceComparison, ...]


class MatchResult(AgentDraft):
    comparisons: tuple[LineComparison, ...]

    def as_draft(self) -> AgentDraft:
        return AgentDraft(status=self.status, findings=self.findings, next_action=self.next_action)


class EvidenceUse(FrozenModel):
    tool: Tool
    arguments: dict[str, str | tuple[str, ...]]
    digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    records: tuple[SourceRef, ...]
    coverage: tuple[Coverage, ...]


class CasePacketV1(FrozenModel):
    schema_version: Literal["case-packet-v1"] = "case-packet-v1"
    invoice_id: Identifier
    snapshot_id: Identifier
    tenant_id: Identifier
    fixture_version: Identifier
    policy_version: Literal["invoice-match-v1"]
    as_of_utc: UtcTime
    status: Status
    comparisons: tuple[LineComparison, ...]
    evidence: tuple[EvidenceUse, ...]
    reason_codes: tuple[ReasonCode, ...]
    findings: tuple[Finding, ...]
    next_action: Action
    trace_id: Identifier
    validator_decision: Literal["VALIDATED", "ESCALATED"]
    validator_reason: Annotated[str, Field(min_length=1, max_length=512)] | None
    reviewer_summary: str


def _refs(records: Sequence[SourceRef]) -> tuple[SourceRef, ...]:
    by_key = {(row.kind, row.source_id, row.revision): source_ref(row) for row in records}
    return tuple(by_key[key] for key in sorted(by_key))


def _finding(
    code: ReasonCode,
    records: Sequence[SourceRef],
    *,
    invoice_line_id: str | None = None,
    po_line_id: str | None = None,
    quantity_delta: int | None = None,
    amount_delta: Decimal | None = None,
) -> Finding:
    return Finding(
        code=code,
        invoice_line_id=invoice_line_id,
        po_line_id=po_line_id,
        quantity_delta=quantity_delta,
        amount_delta=amount_delta,
        citations=_refs(records),
    )


class _ExecutedReads:
    """Index returned rows and coverage by tool."""

    def __init__(self, evidence: Sequence[EvidenceResult]) -> None:
        self.by_tool: dict[Tool, list[EvidenceResult]] = defaultdict(list)
        contexts = {
            (row.snapshot_id, row.tenant_id, row.invoice_id, row.as_of_utc) for row in evidence
        }
        if len(contexts) > 1:
            raise DraftError("The executed tools used different evidence scopes.")
        for row in evidence:
            self.by_tool[row.tool].append(row)

    def rows(self, tool: Tool) -> tuple[EvidenceRecord, ...]:
        by_key: dict[tuple[str, str, int], EvidenceRecord] = {}
        for result in self.by_tool[tool]:
            for record in result.records:
                key = (record.kind, record.source_id, record.revision)
                if key in by_key and by_key[key] != record:
                    raise DraftError("The executed tools returned conflicting source rows.")
                by_key[key] = record
        return tuple(by_key[key] for key in sorted(by_key))

    def covered(self, tool: Tool, source: Source, scope_id: str) -> bool:
        return any(
            row.source == source and row.scope_id == scope_id and row.covers(result.as_of_utc)
            for result in self.by_tool[tool]
            for row in result.coverage
        )


def _price_checks(line: InvoiceLine, po_line: POLine) -> tuple[PriceComparison, list[Finding]]:
    expected_net = (line.unit_price * line.quantity).quantize(CENT, rounding=ROUND_HALF_UP)
    price_delta = abs(line.unit_price - po_line.unit_price)
    net_delta = abs(line.net_amount - expected_net)
    comparison = PriceComparison(
        invoice_line_id=line.source_id,
        invoice_unit_price=line.unit_price,
        po_unit_price=po_line.unit_price,
        unit_price_delta=price_delta,
        invoice_net_amount=line.net_amount,
        expected_net_amount=expected_net,
        net_amount_delta=net_delta,
    )
    findings: list[Finding] = []
    if price_delta > CENT:
        findings.append(
            _finding(
                "UNIT_PRICE_VARIANCE",
                [line, po_line],
                invoice_line_id=line.source_id,
                po_line_id=po_line.source_id,
                amount_delta=price_delta,
            )
        )
    if net_delta > 0:
        findings.append(
            _finding(
                "LINE_NET_VARIANCE",
                [line],
                invoice_line_id=line.source_id,
                po_line_id=po_line.source_id,
                amount_delta=net_delta,
            )
        )
    return comparison, findings


def _compare_line(
    reads: _ExecutedReads,
    lines: list[InvoiceLine],
    po_line: POLine,
    *,
    requested_complete: bool,
) -> tuple[LineComparison, list[Finding]]:
    po_line_id = po_line.source_id
    findings: list[Finding] = []
    prices: list[PriceComparison] = []
    for line in lines:
        price, price_findings = _price_checks(line, po_line)
        prices.append(price)
        findings.extend(price_findings)

    # Only covered lookups supply a quantity. An empty, covered read gives zero.
    receipts = tuple(
        row
        for row in reads.rows("read_receipts")
        if isinstance(row, Receipt) and row.po_line_id == po_line_id
    )
    allocations = tuple(
        row
        for row in reads.rows("read_prior_allocations")
        if isinstance(row, Allocation) and row.po_line_id == po_line_id
    )
    received = None
    allocated = None
    if reads.covered("read_receipts", "receipt", po_line_id):
        received = sum(
            row.quantity if row.event_type == "RECEIPT" else -row.quantity for row in receipts
        )
    if reads.covered("read_prior_allocations", "allocation", po_line_id):
        allocated = sum(row.quantity for row in allocations)
    if received is None or allocated is None:
        findings.append(_finding("MISSING_EVIDENCE", [*lines, po_line], po_line_id=po_line_id))

    requested = sum(line.quantity for line in lines) if requested_complete else None
    ordered = po_line.quantity
    remaining_ordered = max(0, ordered - allocated) if allocated is not None else None
    remaining_received = (
        max(0, received - allocated) if received is not None and allocated is not None else None
    )
    available = (
        min(remaining_ordered, remaining_received)
        if remaining_ordered is not None and remaining_received is not None
        else None
    )
    shortfall = (
        max(0, requested - available) if requested is not None and available is not None else None
    )
    if shortfall is not None and shortfall > 0:
        code: ReasonCode = (
            "ORDER_SHORTFALL" if remaining_ordered == available else "RECEIPT_SHORTFALL"
        )
        findings.append(
            _finding(
                code,
                [*lines, po_line, *receipts, *allocations],
                po_line_id=po_line_id,
                quantity_delta=shortfall,
            )
        )
    return LineComparison(
        po_line_id=po_line_id,
        invoice_line_ids=tuple(line.source_id for line in lines),
        requested_quantity=requested,
        ordered_quantity=ordered,
        received_quantity=received,
        allocated_quantity=allocated,
        remaining_ordered=remaining_ordered,
        remaining_received=remaining_received,
        available_quantity=available,
        shortfall_quantity=shortfall,
        prices=tuple(prices),
    ), findings


def _route(findings: list[Finding], comparisons: list[LineComparison]) -> MatchResult:
    codes = {finding.code for finding in findings}
    status: Status = "INSUFFICIENT_EVIDENCE"
    if codes & {"MISSING_EVIDENCE", "UNSUPPORTED_CURRENCY", "UNSUPPORTED_UNIT"}:
        action: Action = "RESEARCH_MISSING_EVIDENCE"
    elif codes & {"DISPUTED_IDENTITY", "MISSING_LINKAGE", "AMBIGUOUS_LINKAGE"}:
        action = "ASK_BUYER_TO_CONFIRM_PO"
    else:
        status = "EXCEPTION"
        if "DUPLICATE_REFERENCE" in codes:
            action = "INVESTIGATE_DUPLICATE"
        elif "ORDER_SHORTFALL" in codes:
            action = "ASK_BUYER_TO_CONFIRM_PO"
        elif "RECEIPT_SHORTFALL" in codes:
            action = "REQUEST_RECEIPT"
        elif codes & {"UNIT_PRICE_VARIANCE", "LINE_NET_VARIANCE"}:
            action = "REQUEST_CORRECTED_INVOICE"
        else:
            status, action = "MATCHED", "REVIEW_MATCH"
    unique = {_finding_key(finding): finding for finding in findings}
    return MatchResult(
        status=status,
        findings=tuple(unique.values()),
        next_action=action,
        comparisons=tuple(comparisons),
    )


def evaluate_evidence(evidence: Sequence[EvidenceResult]) -> MatchResult:
    """Apply the matching policy to the rows actually returned by the tools."""
    reads = _ExecutedReads(evidence)
    invoice_rows = reads.rows("read_invoice")
    invoice = next((row for row in invoice_rows if isinstance(row, Invoice)), None)
    if invoice is None:
        return _route([_finding("MISSING_EVIDENCE", [])], [])
    lines = [row for row in invoice_rows if isinstance(row, InvoiceLine)]
    findings: list[Finding] = []
    invoice_complete = bool(lines) and reads.covered("read_invoice", "invoice", invoice.source_id)
    if not invoice_complete:
        findings.append(_finding("MISSING_EVIDENCE", [invoice]))
    duplicate_rows = reads.rows("find_duplicate_reference")
    if duplicate_rows:
        findings.append(_finding("DUPLICATE_REFERENCE", [invoice, *duplicate_rows]))
    if invoice.status == "VOID" or not normalize_reference(invoice.reference):
        return _route([*findings, _finding("DISPUTED_IDENTITY", [invoice])], [])
    if invoice.po_id is None:
        return _route([*findings, _finding("MISSING_LINKAGE", [invoice])], [])
    po_rows = reads.rows("read_purchase_order")
    po = next(
        (
            row
            for row in po_rows
            if isinstance(row, PurchaseOrder) and row.source_id == invoice.po_id
        ),
        None,
    )
    if po is None:
        return _route([*findings, _finding("MISSING_EVIDENCE", [invoice])], [])
    if po.tenant_id != invoice.tenant_id or po.supplier_id != invoice.supplier_id:
        return _route([*findings, _finding("DISPUTED_IDENTITY", [invoice, po])], [])
    if not reads.covered("read_purchase_order", "purchase_order", po.source_id):
        return _route([*findings, _finding("MISSING_EVIDENCE", [invoice, po])], [])
    if invoice.currency != "ZAR" or po.currency != "ZAR":
        return _route([*findings, _finding("UNSUPPORTED_CURRENCY", [invoice, po])], [])

    eligible = {
        row.source_id: row
        for row in po_rows
        if isinstance(row, POLine) and row.po_id == po.source_id and row.eligible
    }
    by_po_line: dict[str, list[InvoiceLine]] = defaultdict(list)
    unresolved_links = False
    unsupported_lines = set()
    for line in lines:
        po_line = eligible.get(line.po_line_id or "")
        if po_line is None:
            unresolved_links = True
            candidates = sorted(set(line.candidate_po_line_ids) & eligible.keys())
            code: ReasonCode = "AMBIGUOUS_LINKAGE" if len(candidates) > 1 else "MISSING_LINKAGE"
            findings.append(
                _finding(
                    code,
                    [line, po, *(eligible[key] for key in candidates)],
                    invoice_line_id=line.source_id,
                )
            )
        elif line.unit != "EA" or po_line.unit != "EA":
            unsupported_lines.add(po_line.source_id)
            findings.append(
                _finding(
                    "UNSUPPORTED_UNIT",
                    [line, po_line],
                    invoice_line_id=line.source_id,
                    po_line_id=po_line.source_id,
                )
            )
        else:
            by_po_line[po_line.source_id].append(line)

    comparisons = []
    for po_line_id in sorted(by_po_line):
        # Unseen or unresolved lines leave the invoice total open for that PO line.
        requested_complete = (
            invoice_complete and not unresolved_links and po_line_id not in unsupported_lines
        )
        comparison, line_findings = _compare_line(
            reads,
            by_po_line[po_line_id],
            eligible[po_line_id],
            requested_complete=requested_complete,
        )
        comparisons.append(comparison)
        findings.extend(line_findings)
    if (
        not unresolved_links
        and not unsupported_lines
        and not reads.covered("find_duplicate_reference", "duplicate", invoice.source_id)
    ):
        findings.append(_finding("MISSING_EVIDENCE", [invoice]))
    return _route(findings, comparisons)


def _finding_key(finding: Finding) -> str:
    document = finding.model_dump(mode="json")
    document["citations"] = sorted(
        document["citations"], key=lambda ref: (ref["kind"], ref["source_id"], ref["revision"])
    )
    return json.dumps(document, sort_keys=True)


def validate_draft(evidence: Sequence[EvidenceResult], draft: AgentDraft) -> MatchResult:
    """Check each claimed finding against the matching calculation and its sources."""
    expected = evaluate_evidence(evidence)
    if draft.status != expected.status or draft.next_action != expected.next_action:
        raise DraftError("Draft status or reviewer action does not match the returned evidence.")
    if Counter(map(_finding_key, draft.findings)) != Counter(map(_finding_key, expected.findings)):
        raise DraftError("Draft findings, deltas, or citations do not match the returned evidence.")
    return expected


def _reviewer_summary(result: MatchResult, escalated: bool) -> str:
    if escalated:
        return "The draft did not pass validation. Review the collected evidence and investigate the gaps."
    if result.status == "MATCHED":
        return "The invoice passed the matching checks. Review the evidence before recording a disposition."
    messages = []
    for finding in result.findings:
        if finding.code in {"ORDER_SHORTFALL", "RECEIPT_SHORTFALL"}:
            messages.append(
                f"PO line {finding.po_line_id} is {finding.quantity_delta} units short."
            )
        elif finding.amount_delta is not None:
            field = "unit price" if finding.code == "UNIT_PRICE_VARIANCE" else "net amount"
            messages.append(
                f"Invoice line {finding.invoice_line_id} has a {field} difference of R{finding.amount_delta}."
            )
        else:
            labels = {
                "MISSING_EVIDENCE": "Source coverage is incomplete.",
                "DISPUTED_IDENTITY": "The invoice and purchase order identity need verification.",
                "MISSING_LINKAGE": "The purchase order linkage could not be confirmed.",
                "AMBIGUOUS_LINKAGE": "More than one purchase order line could match.",
                "UNSUPPORTED_CURRENCY": "The currency requires review.",
                "UNSUPPORTED_UNIT": "The unit of measure requires review.",
                "DUPLICATE_REFERENCE": "A second invoice uses the same supplier reference.",
            }
            messages.append(labels[finding.code])
    return " ".join(dict.fromkeys(messages))


def make_packet(
    access: EvidenceAccess,
    evidence: Sequence[EvidenceResult],
    trace_id: str,
    *,
    draft: AgentDraft | None = None,
    validator_reason: str | None = None,
) -> CasePacketV1:
    """Build the persisted packet from verified fields and source references."""
    context = (
        access.snapshot.snapshot_id,
        access.tenant_id,
        access.invoice_id,
        access.snapshot.as_of_utc,
    )
    if any(
        (row.snapshot_id, row.tenant_id, row.invoice_id, row.as_of_utc) != context
        for row in evidence
    ):
        raise DraftError("Evidence does not belong to this investigation.")
    if validator_reason is not None:
        if draft is not None:
            raise DraftError("Cannot escalate and validate a draft together.")
        result = MatchResult(
            status="INSUFFICIENT_EVIDENCE",
            findings=(),
            next_action="RESEARCH_MISSING_EVIDENCE",
            comparisons=(),
        )
    elif draft is not None:
        result = validate_draft(evidence, draft)
    else:
        result = evaluate_evidence(evidence)
    return CasePacketV1(
        invoice_id=access.invoice_id,
        snapshot_id=access.snapshot.snapshot_id,
        tenant_id=access.tenant_id,
        fixture_version=access.snapshot.fixture_version,
        policy_version=access.snapshot.policy_version,
        as_of_utc=access.snapshot.as_of_utc,
        status=result.status,
        comparisons=result.comparisons,
        evidence=tuple(
            EvidenceUse(
                tool=row.tool,
                arguments=row.arguments,
                digest=row.digest,
                records=_refs(row.records),
                coverage=row.coverage,
            )
            for row in evidence
        ),
        reason_codes=tuple(dict.fromkeys(finding.code for finding in result.findings)),
        findings=result.findings,
        next_action=result.next_action,
        trace_id=trace_id,
        validator_decision="ESCALATED" if validator_reason is not None else "VALIDATED",
        validator_reason=validator_reason,
        reviewer_summary=_reviewer_summary(result, validator_reason is not None),
    )
