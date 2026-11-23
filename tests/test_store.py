from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest

from invoice_agent.fixtures import headline_snapshot
from invoice_agent.store import Conflict, NotFound, Store

TENANT = "cape-retail"
ACTOR = "demo-requester"
REVIEWER = "demo-reviewer"
REQUEST = {"snapshot_id": "retail-demo-v1", "invoice_id": "INV-060", "mode": "recorded"}


def key() -> str:
    return uuid4().hex


def test_snapshot_is_idempotent_and_scoped(seeded_store: Store) -> None:
    snapshot = headline_snapshot()
    seeded_store.seed_snapshot(snapshot)
    assert seeded_store.get_snapshot(TENANT, snapshot.snapshot_id) == snapshot
    changed = snapshot.model_copy(update={"fixture_version": "unexpected-version"})
    with pytest.raises(Conflict):
        seeded_store.seed_snapshot(changed)
    with pytest.raises(NotFound):
        seeded_store.get_snapshot("other-tenant", snapshot.snapshot_id)


def test_same_key_elects_only_one_worker(seeded_store: Store) -> None:
    snapshot = headline_snapshot()
    request_key = key()
    barrier = Barrier(4)

    def start() -> tuple[str, str]:
        barrier.wait(timeout=10)
        claim = seeded_store.reserve(TENANT, ACTOR, request_key, REQUEST, snapshot.policy_version)
        return claim.status, claim.run_id

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = [job.result(timeout=15) for job in [pool.submit(start) for _ in range(4)]]
    assert sorted(status for status, _ in outcomes) == ["active", "active", "active", "claimed"]
    assert len({run_id for _, run_id in outcomes}) == 1
    with pytest.raises(Conflict):
        seeded_store.reserve(
            TENANT, ACTOR, request_key, {**REQUEST, "mode": "live"}, snapshot.policy_version
        )


def test_expired_worker_cannot_complete_and_retry_reuses_case(seeded_store: Store) -> None:
    policy = headline_snapshot().policy_version
    request_key = key()
    first = seeded_store.reserve(TENANT, ACTOR, request_key, REQUEST, policy)
    assert first.lease_token is not None
    with seeded_store.connect() as conn:
        conn.execute(
            "UPDATE runs SET lease_expires_at = clock_timestamp() WHERE run_id = %s",
            (first.run_id,),
        )
    assert seeded_store.get_run(TENANT, first.run_id)["retryable"]
    second = seeded_store.reserve(TENANT, ACTOR, request_key, REQUEST, policy)
    assert second.run_id == first.run_id and second.lease_token is not None
    with pytest.raises(Conflict):
        seeded_store.complete(TENANT, first.run_id, first.lease_token, {"test": True})
    case_id = seeded_store.complete(TENANT, second.run_id, second.lease_token, {"test": True})
    retry = seeded_store.reserve(TENANT, ACTOR, request_key, REQUEST, policy)
    assert (retry.status, retry.case_id) == ("complete", case_id)
    with seeded_store.connect() as conn:
        count = conn.execute(
            "SELECT count(*) AS total FROM audit_events WHERE case_id = %s", (case_id,)
        ).fetchone()
    assert count == {"total": 1}
    assert Store(seeded_store.dsn).get_case(TENANT, case_id)["packet"] == {"test": True}
    with pytest.raises(NotFound):
        seeded_store.get_case("other-tenant", case_id)


def test_failed_case_transaction_leaves_a_claim_that_can_still_complete(
    seeded_store: Store,
) -> None:
    policy = headline_snapshot().policy_version
    first = seeded_store.reserve(TENANT, ACTOR, key(), REQUEST, policy)
    assert first.lease_token is not None
    with pytest.raises(TypeError):
        seeded_store.complete(TENANT, first.run_id, first.lease_token, {"bad": object()})
    assert seeded_store.get_run(TENANT, first.run_id)["state"] == "RUNNING"
    case_id = seeded_store.complete(TENANT, first.run_id, first.lease_token, {"test": True})
    assert seeded_store.get_case(TENANT, case_id)["version"] == 1


def test_provider_failure_and_reviewer_decisions_are_idempotent(seeded_store: Store) -> None:
    policy = headline_snapshot().policy_version
    request_key = key()
    first = seeded_store.reserve(TENANT, ACTOR, request_key, REQUEST, policy)
    assert first.lease_token is not None
    seeded_store.mark_retryable(TENANT, first.run_id, first.lease_token, "PROVIDER_UNAVAILABLE")
    assert seeded_store.get_run(TENANT, first.run_id)["retryable"]
    second = seeded_store.reserve(TENANT, ACTOR, request_key, REQUEST, policy)
    assert second.lease_token is not None
    case_id = seeded_store.complete(TENANT, second.run_id, second.lease_token, {"test": True})
    decision_key = key()
    args = (TENANT, REVIEWER, case_id, decision_key, 1, "RETURN_FOR_RESEARCH", "Confirm receipt")
    decision = seeded_store.decide(*args)
    assert seeded_store.decide(*args) == decision
    assert seeded_store.get_case(TENANT, case_id)["status"] == "RETURNED_FOR_RESEARCH"
    assert seeded_store.get_case(TENANT, case_id)["version"] == 2
    with pytest.raises(Conflict):
        seeded_store.decide(TENANT, REVIEWER, case_id, key(), 1, "APPROVE_DISPOSITION", "Stale")
    with pytest.raises(Conflict):
        seeded_store.decide(*args[:-1], "Changed reason")
    with seeded_store.connect() as conn:
        events = conn.execute(
            "SELECT event_type FROM audit_events WHERE case_id = %s ORDER BY version", (case_id,)
        ).fetchall()
    assert [event["event_type"] for event in events] == ["CASE_READY", "REVIEW_DECIDED"]
