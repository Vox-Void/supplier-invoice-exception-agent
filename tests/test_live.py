import json

import httpx2
import pytest
from openai import DefaultAsyncHttpxClient

from invoice_agent import live
from invoice_agent.domain import EvidenceAccess
from invoice_agent.fixtures import headline_snapshot
from invoice_agent.workflow import (
    DirectoryRecordings,
    InvestigationRequest,
    LiveSettings,
    RetryableError,
    run_workflow,
)


def install_transport(monkeypatch, handler):
    def make_client(**kwargs):
        return DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler), **kwargs)

    monkeypatch.setattr(live, "DefaultAsyncHttpxClient", make_client)


def finished_response(number, output):
    return {
        "id": f"resp_{number}",
        "object": "response",
        "created_at": 1_700_000_000,
        "model": "test-model",
        "status": "completed",
        "output": [output],
        "error": None,
        "incomplete_details": None,
        "tools": [],
        "parallel_tool_calls": False,
        "usage": {
            "input_tokens": 500,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 100,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 600,
        },
    }


def test_agents_sdk_reads_scoped_tools_and_passes_the_draft_to_the_rule_engine(monkeypatch):
    access = EvidenceAccess(headline_snapshot(), "cape-retail", "INV-060")
    request = InvestigationRequest(invoice_id="INV-060", snapshot_id="retail-demo-v1")
    recording = DirectoryRecordings().load(request)
    observed = []

    def respond(request):
        number = len(observed)
        assert str(request.url) == "https://api.openai.com/v1/responses"
        assert request.headers["authorization"] == "Bearer test-only-key"
        payload = json.loads(request.content)
        observed.append(payload)
        assert payload["store"] is False
        assert payload["max_output_tokens"] == 1600
        assert payload["parallel_tool_calls"] is False
        assert payload["text"]["format"]["strict"] is True
        assert {tool["name"] for tool in payload["tools"]} == {
            "read_invoice",
            "read_purchase_order",
            "read_receipts",
            "read_prior_allocations",
            "find_duplicate_reference",
        }
        if number < len(recording.calls):
            action = recording.calls[number]
            output = {
                "type": "function_call",
                "id": f"fc_{number}",
                "call_id": f"call_{number}",
                "name": action.tool,
                "arguments": json.dumps(action.arguments),
                "status": "completed",
            }
        else:
            output = {
                "type": "message",
                "id": f"msg_{number}",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {"type": "output_text", "annotations": [], "text": json.dumps(recording.draft)}
                ],
            }
        return httpx2.Response(200, json=finished_response(number, output))

    install_transport(monkeypatch, respond)
    adapter = live.OpenAIAdapter(LiveSettings(True, "test-model"), "test-only-key")
    packet = run_workflow(access, mode="live", live=adapter)
    assert len(observed) == len(recording.calls) + 1
    assert packet.status == "EXCEPTION" and packet.validator_decision == "VALIDATED"
    assert packet.findings[0].quantity_delta == 10


def test_provider_http_failure_is_retryable_and_sdk_does_not_resend(monkeypatch):
    observed = []

    def respond(request):
        observed.append(request.url)
        return httpx2.Response(
            503, json={"error": {"message": "Simulated failure", "type": "server_error"}}
        )

    install_transport(monkeypatch, respond)
    access = EvidenceAccess(headline_snapshot(), "cape-retail", "INV-060")
    adapter = live.OpenAIAdapter(LiveSettings(True, "test-model"), "test-only-key")
    with pytest.raises(RetryableError, match="^PROVIDER_UNAVAILABLE$"):
        run_workflow(access, mode="live", live=adapter)
    assert len(observed) == 1


def test_budget_stops_a_provider_request_before_transport(monkeypatch):
    def respond(_request):
        pytest.fail("A request over budget must never reach the transport")

    install_transport(monkeypatch, respond)
    access = EvidenceAccess(headline_snapshot(), "cape-retail", "INV-060")
    adapter = live.OpenAIAdapter(LiveSettings(True, "test-model", token_budget=50), "test-only-key")
    packet = run_workflow(access, mode="live", live=adapter)
    assert packet.status == "INSUFFICIENT_EVIDENCE" and packet.validator_reason == "TOKEN_LIMIT"
