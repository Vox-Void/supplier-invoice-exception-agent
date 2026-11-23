import pytest

from invoice_agent.domain import EvidenceAccess
from invoice_agent.fixtures import headline_snapshot
from invoice_agent.workflow import (
    MAX_TOOL_CALLS,
    DirectoryRecordings,
    InvestigationRequest,
    RetryableError,
    run_workflow,
)


@pytest.fixture
def headline():
    access = EvidenceAccess(headline_snapshot(), "cape-retail", "INV-060")
    request = InvestigationRequest(invoice_id="INV-060", snapshot_id="retail-demo-v1")
    return access, DirectoryRecordings().load(request)


def test_recorded_and_fixed_order_paths_agree_on_headline(headline):
    access, recording = headline
    recorded = run_workflow(access, mode="recorded", recording=recording)
    baseline = run_workflow(access, mode="baseline")
    assert recorded.status == baseline.status == "EXCEPTION"
    assert recorded.validator_decision == "VALIDATED"
    assert recorded.next_action == baseline.next_action == "REQUEST_RECEIPT"
    assert recorded.findings == baseline.findings
    assert recorded.comparisons == baseline.comparisons
    assert recorded.findings[0].quantity_delta == 10


def test_changed_tool_result_stops_replay_without_a_packet(headline):
    access, recording = headline
    recording.calls[0] = recording.calls[0].model_copy(update={"digest": "0" * 64})
    with pytest.raises(RetryableError, match="^RECORDING_DIGEST_MISMATCH$"):
        run_workflow(access, mode="recorded", recording=recording)


def test_a_draft_that_omits_a_known_finding_keeps_only_evidence(headline):
    access, recording = headline
    recording.draft["findings"] = []
    packet = run_workflow(access, mode="recorded", recording=recording)
    assert packet.status == "INSUFFICIENT_EVIDENCE"
    assert packet.validator_decision == "ESCALATED"
    assert packet.validator_reason == "INVALID_DRAFT"
    assert packet.next_action == "RESEARCH_MISSING_EVIDENCE"
    assert not packet.findings and not packet.comparisons
    assert packet.evidence


def test_ninth_accepted_tool_call_escalates_before_returning_more_evidence(headline):
    access, _ = headline
    accepted = []

    def repeat_invoice(tools):
        for _ in range(MAX_TOOL_CALLS + 1):
            accepted.append(tools.call("read_invoice", {}).digest)
        pytest.fail("The extra tool call must hit the limit")

    packet = run_workflow(access, mode="live", live=repeat_invoice)
    assert len(accepted) == MAX_TOOL_CALLS
    assert packet.status == "INSUFFICIENT_EVIDENCE"
    assert packet.validator_reason == "TOOL_CALL_LIMIT"
    assert not packet.findings and not packet.comparisons
