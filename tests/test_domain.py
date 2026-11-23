from collections import Counter
from decimal import Decimal

import pytest
from pydantic import ValidationError

from invoice_agent.domain import (
    MAX_TOOL_ROWS,
    AgentDraft,
    DraftError,
    EvidenceAccess,
    EvidenceResult,
    FixtureNotFound,
    InvoiceLine,
    ScopeError,
    Snapshot,
    evaluate_evidence,
    make_packet,
    validate_draft,
)
from invoice_agent.fixtures import (
    ALLOCATION_ID,
    FIXTURE_ROOT,
    INVOICE_ID,
    LATER,
    PO_ID,
    PO_LINE,
    TENANT,
    GoldenCase,
    fixture_files,
    headline_snapshot,
    load_goldens,
    load_snapshots,
)
from invoice_agent.workflow import DirectoryRecordings, InvestigationRequest, run_workflow

SNAPSHOTS = {snapshot.snapshot_id: snapshot for snapshot in load_snapshots()}
GOLDENS = load_goldens()


def access_for(snapshot: Snapshot | None = None) -> EvidenceAccess:
    return EvidenceAccess(snapshot or headline_snapshot(), TENANT, INVOICE_ID)


def all_reads(access: EvidenceAccess) -> list[EvidenceResult]:
    return [
        access.read_invoice(),
        access.read_purchase_order(PO_ID),
        access.read_receipts([PO_LINE]),
        access.read_prior_allocations([PO_LINE]),
        access.find_duplicate_reference(),
    ]


def test_fixture_files_match_the_generator() -> None:
    generated = fixture_files()
    committed = {
        str(path.relative_to(FIXTURE_ROOT).as_posix()) for path in FIXTURE_ROOT.rglob("*.json")
    }
    assert committed == generated.keys()
    assert Counter(case.split for case in GOLDENS) == {"development": 7, "evaluation": 12}
    for name, contents in generated.items():
        assert (FIXTURE_ROOT / name).read_bytes() == contents, name


@pytest.mark.parametrize("golden", GOLDENS, ids=[case.case_id for case in GOLDENS])
def test_recorded_and_baseline_packets_match_authored_outcomes(golden: GoldenCase) -> None:
    access = EvidenceAccess(SNAPSHOTS[golden.snapshot_id], golden.tenant_id, golden.invoice_id)
    baseline = run_workflow(access, mode="baseline", trace_id="baseline-trace")
    recording = DirectoryRecordings().load(
        InvestigationRequest(invoice_id=golden.invoice_id, snapshot_id=golden.snapshot_id)
    )
    replayed = run_workflow(access, mode="recorded", recording=recording, trace_id="replay-trace")
    assert replayed.trace_id == "replay-trace"
    assert baseline.validator_decision == "VALIDATED"
    assert replayed.validator_decision == golden.validator_decision
    assert tuple(row.digest for row in replayed.evidence) == tuple(
        call.digest for call in recording.calls
    )
    if golden.validator_decision == "ESCALATED":
        assert replayed.validator_reason == "INVALID_DRAFT"
        assert replayed.status == "INSUFFICIENT_EVIDENCE"
        assert replayed.next_action == "RESEARCH_MISSING_EVIDENCE"
        assert replayed.findings == replayed.comparisons == replayed.reason_codes == ()
        assert replayed.evidence
    else:
        assert replayed.status == baseline.status
        assert replayed.findings == baseline.findings
        assert replayed.next_action == baseline.next_action
        assert replayed.comparisons == baseline.comparisons
    assert baseline.status == golden.status
    assert baseline.findings == golden.findings
    assert baseline.next_action == golden.next_action
    comparisons = {row.po_line_id: row.model_dump(mode="json") for row in baseline.comparisons}
    for po_line, expected_fields in golden.comparisons.items():
        assert {field: comparisons[po_line][field] for field in expected_fields} == expected_fields


def test_cutoff_and_exclusion_remove_ineligible_allocations() -> None:
    snapshot = headline_snapshot()
    approved = snapshot.allocations[0]
    updated = snapshot.model_copy(
        update={
            "allocations": (
                approved,
                approved.model_copy(update={"source_id": "SELF", "invoice_id": INVOICE_ID}),
                approved.model_copy(update={"source_id": "LATE-EVENT", "event_at": LATER}),
                approved.model_copy(update={"source_id": "LATE-REC", "recorded_at": LATER}),
                approved.model_copy(update={"source_id": "PENDING", "status": "PENDING"}),
                approved.model_copy(update={"source_id": "VOID", "status": "VOID"}),
            )
        }
    )
    allocations = access_for(updated).read_prior_allocations([PO_LINE])
    assert tuple(row.source_id for row in allocations.records) == (ALLOCATION_ID,)


def test_incomplete_invoice_rows_leave_total_and_shortfall_unresolved() -> None:
    snapshot = headline_snapshot()
    line = snapshot.invoice_lines[0]
    many_lines = tuple(
        line.model_copy(update={"source_id": f"IL-LOTS-{index:02}"})
        for index in range(MAX_TOOL_ROWS + 1)
    )
    updated = snapshot.model_copy(update={"invoice_lines": many_lines})
    evidence = all_reads(access_for(updated))
    assert len(evidence[0].records) == MAX_TOOL_ROWS
    assert not evidence[0].coverage[0].complete
    result = evaluate_evidence(evidence)
    assert result.status == "INSUFFICIENT_EVIDENCE"
    assert {finding.code for finding in result.findings} == {"MISSING_EVIDENCE"}
    assert result.comparisons[0].requested_quantity is None
    assert result.comparisons[0].shortfall_quantity is None
    assert result.comparisons[0].available_quantity == 50


def test_visible_reversal_without_visible_original_makes_coverage_incomplete() -> None:
    snapshot = SNAPSHOTS["split-with-reversal"]
    receipts = tuple(
        row.model_copy(update={"recorded_at": LATER}) if row.source_id == "RCV-050" else row
        for row in snapshot.receipts
    )
    updated = snapshot.model_copy(update={"receipts": receipts})
    evidence = all_reads(access_for(updated))
    assert not evidence[2].coverage[0].complete
    result = evaluate_evidence(evidence)
    assert result.status == "INSUFFICIENT_EVIDENCE"
    assert result.comparisons[0].received_quantity is None


def test_tenant_and_supplier_scope() -> None:
    snapshot = headline_snapshot()
    with pytest.raises(FixtureNotFound):
        EvidenceAccess(snapshot, "other-tenant", INVOICE_ID)
    access = access_for(snapshot)
    with pytest.raises(ScopeError):
        access.read_purchase_order("PO-OUTSIDE")
    with pytest.raises(ScopeError):
        access.read_receipts([PO_LINE, "POL-OUTSIDE"])
    with pytest.raises(ScopeError):
        access.read_prior_allocations(["POL-OUTSIDE"])
    different_supplier = snapshot.purchase_orders[0].model_copy(
        update={"supplier_id": "other-supplier"}
    )
    scoped = access_for(snapshot.model_copy(update={"purchase_orders": (different_supplier,)}))
    with pytest.raises(ScopeError):
        scoped.read_receipts([PO_LINE])
    packet = make_packet(
        scoped, [scoped.read_invoice(), scoped.read_purchase_order(PO_ID)], "trace"
    )
    assert packet.reason_codes == ("DISPUTED_IDENTITY",)
    assert packet.next_action == "ASK_BUYER_TO_CONFIRM_PO"


@pytest.mark.parametrize(
    "field,value",
    [
        ("quantity", -1),
        ("quantity", 1.0),
        ("unit_price", "1.001"),
        ("unit_price", 1.1),
        ("net_amount", "1e2"),
        ("net_amount", "-1.00"),
    ],
)
def test_quantity_and_money_reject_unsupported_values(field: str, value: object) -> None:
    data = headline_snapshot().invoice_lines[0].model_dump(mode="json")
    data[field] = value
    with pytest.raises(ValidationError):
        InvoiceLine.model_validate(data)


def test_draft_rejects_an_exact_citation_bound_to_wrong_finding() -> None:
    access = access_for(SNAPSHOTS["wrong-finding-citation"])
    evidence = all_reads(access)
    claims = (
        DirectoryRecordings()
        .load(InvestigationRequest(invoice_id=INVOICE_ID, snapshot_id=access.snapshot.snapshot_id))
        .draft
    )
    invalid = AgentDraft.model_validate(claims)
    assert invalid.findings[0].amount_delta == Decimal("0.02")
    assert invalid.findings[0].citations[1].source_id in {
        row.source_id for row in evidence[2].records
    }
    with pytest.raises(DraftError, match="findings, deltas, or citations"):
        validate_draft(evidence, invalid)
