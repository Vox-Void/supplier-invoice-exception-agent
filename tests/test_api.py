from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from fastapi.testclient import TestClient

from invoice_agent import workflow
from invoice_agent.api import DemoIdentity, create_app
from invoice_agent.fixtures import headline_snapshot
from invoice_agent.store import Conflict, Reservation, Store
from invoice_agent.workflow import DirectoryRecordings, InvestigationService, LiveSettings

REQUEST = {"invoice_id": "INV-060", "snapshot_id": "retail-demo-v1"}
IDENTITY = DemoIdentity("requester-token", "reviewer-token")
REQUESTER = {"Authorization": f"Bearer {IDENTITY.requester_token}"}
REVIEWER = {"Authorization": f"Bearer {IDENTITY.reviewer_token}"}


def setup_api(store, settings=LiveSettings()):
    if isinstance(store, Mock):
        store.find_active_or_complete.return_value = None
    return TestClient(
        create_app(InvestigationService(store, DirectoryRecordings(), settings), IDENTITY)
    )


def test_openapi_declares_typed_contract_and_errors():
    paths = setup_api(Mock(spec=Store)).get("/openapi.json").json()["paths"]
    post = paths["/v1/investigations"]["post"]
    assert {"200", "201", "202", "401", "403", "404", "409", "422", "503"} <= set(post["responses"])
    assert post["security"] == [{"HTTPBearer": []}]
    assert post["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/InvestigationRequest"
    )
    for operation in (post, paths["/v1/cases/{case_id}/decisions"]["post"]):
        assert any(
            parameter["in"] == "header"
            and parameter["name"] == "Idempotency-Key"
            and parameter["required"]
            for parameter in operation["parameters"]
        )
        assert operation["responses"]["409"]["content"]["application/json"]["schema"][
            "$ref"
        ].endswith("/ErrorResponse")


def test_auth_and_validation_run_before_reservation(monkeypatch):
    store = Mock(spec=Store)
    api = setup_api(store)
    header = {**REQUESTER, "Idempotency-Key": "first-request"}
    denied = api.post("/v1/investigations", json=REQUEST)
    assert denied.status_code == 401 and denied.headers["WWW-Authenticate"] == "Bearer"
    assert api.post("/v1/investigations", json=REQUEST, headers=REQUESTER).status_code == 422
    assert (
        api.post("/v1/investigations", json=REQUEST, headers={**header, **REVIEWER}).status_code
        == 403
    )
    assert (
        api.post(
            "/v1/investigations", json={**REQUEST, "tenant_id": "other"}, headers=header
        ).status_code
        == 422
    )
    assert api.get("/v1/cases", headers=REQUESTER).status_code == 403
    assert api.post("/v1/cases/a/decisions", json={}, headers=REQUESTER).status_code == 403

    def fail_if_read(name, _default=None):
        pytest_message = f"Disabled live mode attempted to read {name}"
        raise AssertionError(pytest_message)

    monkeypatch.setattr(workflow, "os", SimpleNamespace(environ=SimpleNamespace(get=fail_if_read)))
    live = api.post("/v1/investigations", json={**REQUEST, "mode": "live"}, headers=header)
    assert (live.status_code, live.json()["code"]) == (422, "LIVE_NOT_CONFIGURED")
    store.get_snapshot.assert_not_called()
    store.reserve.assert_not_called()


def test_active_reservation_and_conflicting_key_do_not_rerun_tools():
    store = Mock(spec=Store)
    store.get_snapshot.return_value = headline_snapshot()
    store.reserve.return_value = Reservation("active", "run-id")
    store.get_run.return_value = {"run_id": "run-id", "state": "RUNNING", "retryable": False}
    api = setup_api(store)
    headers = {**REQUESTER, "Idempotency-Key": "active"}
    active = api.post("/v1/investigations", headers=headers, json=REQUEST)
    assert (active.status_code, active.json()["run_id"]) == (202, "run-id")
    store.reserve.side_effect = Conflict("changed hash")
    changed = api.post("/v1/investigations", headers=headers, json=REQUEST)
    assert (changed.status_code, changed.json()["code"]) == (409, "IDEMPOTENCY_CONFLICT")
    store.complete.assert_not_called()


def test_provider_failure_leaves_retryable_run_and_no_case(monkeypatch):
    store = Mock(spec=Store)
    store.get_snapshot.return_value = headline_snapshot()
    store.reserve.return_value = Reservation("claimed", "run-id", "lease-token")
    monkeypatch.setenv("OPENAI_API_KEY", "test-only-key")

    def unavailable(_settings, _key):
        def adapter(_tools):
            raise workflow.RetryableError("PROVIDER_UNAVAILABLE")

        return adapter

    monkeypatch.setattr("invoice_agent.live.OpenAIAdapter", unavailable)
    api = setup_api(store, LiveSettings(True, "test-model"))
    failed = api.post(
        "/v1/investigations",
        headers={**REQUESTER, "Idempotency-Key": "live"},
        json={**REQUEST, "mode": "live"},
    )
    assert (failed.status_code, failed.json()["code"], failed.json()["run_id"]) == (
        503,
        "PROVIDER_UNAVAILABLE",
        "run-id",
    )
    store.mark_retryable.assert_called_once_with(
        "cape-retail", "run-id", "lease-token", "PROVIDER_UNAVAILABLE"
    )
    store.complete.assert_not_called()


def test_real_api_case_survives_new_app_and_decision_retry(seeded_store: Store):
    api = setup_api(seeded_store)
    headers = {**REQUESTER, "Idempotency-Key": uuid4().hex}
    submitted = api.post("/v1/investigations", headers=headers, json=REQUEST)
    assert submitted.status_code == 201, submitted.text
    run_id, case_id = (submitted.json()[field] for field in ("run_id", "case_id"))

    # Use a new app and store instance to fetch the committed run and review packet.
    api = setup_api(Store(seeded_store.dsn))
    retry = api.post("/v1/investigations", headers=headers, json={**REQUEST, "mode": "recorded"})
    assert retry.status_code == 200 and retry.json()["case_id"] == case_id
    for change in ({"mode": "live"}, {"invoice_id": "unknown-invoice"}):
        conflict = api.post("/v1/investigations", headers=headers, json={**REQUEST, **change})
        assert (conflict.status_code, conflict.json()["code"]) == (409, "IDEMPOTENCY_CONFLICT")
    assert api.get(f"/v1/investigations/{run_id}", headers=REQUESTER).json()["state"] == "COMPLETE"
    assert any(row["case_id"] == case_id for row in api.get("/v1/cases", headers=REVIEWER).json())
    case = api.get(f"/v1/cases/{case_id}", headers=REVIEWER).json()
    assert (case["status"], case["version"], case["packet"]["findings"][0]["quantity_delta"]) == (
        "READY_FOR_REVIEW",
        1,
        10,
    )

    endpoint = f"/v1/cases/{case_id}/decisions"
    decision = {
        "expected_version": 1,
        "decision": "APPROVE_DISPOSITION",
        "reason": "Review the short receipt.",
    }
    review_headers = {**REVIEWER, "Idempotency-Key": uuid4().hex}
    approved = api.post(endpoint, headers=review_headers, json=decision)
    assert approved.status_code == 200, approved.text
    assert api.post(endpoint, headers=review_headers, json=decision).json() == approved.json()
    stale = api.post(endpoint, headers={**REVIEWER, "Idempotency-Key": uuid4().hex}, json=decision)
    assert stale.status_code == 409 and stale.json()["code"] == "DECISION_CONFLICT"

    with seeded_store.connect() as conn:
        rows = conn.execute(
            "SELECT event_type FROM audit_events WHERE case_id = %s ORDER BY version",
            (case_id,),
        ).fetchall()
    assert [row["event_type"] for row in rows] == ["CASE_READY", "REVIEW_DECIDED"]


def test_completed_live_run_can_be_retrieved_after_disabling_provider(seeded_store: Store):
    request = {**REQUEST, "mode": "live"}
    key = uuid4().hex
    claim = seeded_store.reserve("cape-retail", "demo-requester", key, request, "invoice-match-v1")
    assert claim.lease_token is not None
    case_id = seeded_store.complete("cape-retail", claim.run_id, claim.lease_token, {"test": True})
    response = setup_api(seeded_store).post(
        "/v1/investigations", headers={**REQUESTER, "Idempotency-Key": key}, json=request
    )
    assert response.status_code == 200 and response.json()["case_id"] == case_id
