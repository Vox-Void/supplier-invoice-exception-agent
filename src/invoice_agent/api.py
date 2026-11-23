"""Expose investigation and review operations through the local FastAPI app."""

import os
from dataclasses import dataclass
from secrets import compare_digest
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import Field, StringConstraints

from invoice_agent.domain import CasePacketV1, Identifier
from invoice_agent.store import DEFAULT_DSN, Conflict, NotFound, Store
from invoice_agent.workflow import (
    Actor,
    Contract,
    DirectoryRecordings,
    IdempotencyKey,
    InvestigationRequest,
    InvestigationService,
    LiveSettings,
    RunView,
    ServiceError,
    configure_trace_logging,
)

CaseStatus = Literal["READY_FOR_REVIEW", "APPROVED", "RETURNED_FOR_RESEARCH"]
Decision = Literal["APPROVE_DISPOSITION", "RETURN_FOR_RESEARCH"]
DecisionReason = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)
]
CaseVersion = Annotated[int, Field(strict=True, ge=1, le=2_147_483_647)]
KeyHeader = Annotated[IdempotencyKey, Header(alias="Idempotency-Key")]
BEARER = HTTPBearer(auto_error=False)


class DecisionRequest(Contract):
    expected_version: CaseVersion
    decision: Decision
    reason: DecisionReason


class DecisionView(Contract):
    decision_id: str
    case_id: str
    decision: Decision
    reason: str
    version: int


class CaseView(Contract):
    case_id: str
    run_id: str
    status: CaseStatus
    version: int
    packet: CasePacketV1


class ErrorResponse(Contract):
    code: str
    message: str
    run_id: str | None = None


@dataclass(frozen=True)
class DemoIdentity:
    """Map two local bearer tokens to fixed demo actors."""

    requester_token: str
    reviewer_token: str
    tenant_id: str = "cape-retail"

    def __post_init__(self) -> None:
        if not all(
            token and token.isascii() and not any(char.isspace() for char in token)
            for token in (self.requester_token, self.reviewer_token)
        ):
            raise ValueError("Demo bearer tokens must be nonempty ASCII strings without whitespace")
        if self.requester_token == self.reviewer_token:
            raise ValueError("Requester and reviewer must have different tokens")

    @classmethod
    def from_env(cls) -> "DemoIdentity":
        return cls(
            os.environ.get("DEMO_REQUESTER_TOKEN", "demo-requester-local"),
            os.environ.get("DEMO_REVIEWER_TOKEN", "demo-reviewer-local"),
        )

    def resolve(self, token: str) -> Actor:
        if compare_digest(token.encode(), self.requester_token.encode()):
            return Actor(self.tenant_id, "demo-requester", "requester")
        if compare_digest(token.encode(), self.reviewer_token.encode()):
            return Actor(self.tenant_id, "demo-reviewer", "reviewer")
        raise ServiceError(401, "AUTHENTICATION_REQUIRED", "A valid demo bearer token is required.")


def unavailable() -> ServiceError:
    return ServiceError(404, "NOT_FOUND", "The requested record was not found.")


def error_response(status: int, code: str, message: str, run_id: str | None = None) -> JSONResponse:
    error = ErrorResponse(code=code, message=message, run_id=run_id)
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
    return JSONResponse(error.model_dump(exclude_none=True), status_code=status, headers=headers)


def create_app(
    service: InvestigationService | None = None,
    identity: DemoIdentity | None = None,
) -> FastAPI:
    if service is None:
        configure_trace_logging()
        dsn = os.environ.get("INVOICE_AGENT_DSN", DEFAULT_DSN)
        service = InvestigationService(Store(dsn), DirectoryRecordings(), LiveSettings.from_env())
    identities = identity or DemoIdentity.from_env()
    workflow = service
    store = workflow.store
    app = FastAPI(title="Supplier Invoice Exception Agent", version="1.0.0")

    def authenticate(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(BEARER)],
    ) -> Actor:
        if credentials is None:
            raise ServiceError(
                401, "AUTHENTICATION_REQUIRED", "A valid demo bearer token is required."
            )
        return identities.resolve(credentials.credentials)

    def reviewer(actor: Annotated[Actor, Depends(authenticate)]) -> Actor:
        if actor.role != "reviewer":
            raise ServiceError(403, "ROLE_FORBIDDEN", "A reviewer must perform this operation.")
        return actor

    @app.exception_handler(ServiceError)
    async def service_error(_: Request, error: ServiceError) -> JSONResponse:
        return error_response(error.status_code, error.code, error.message, error.run_id)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_: Request, _error: RequestValidationError) -> JSONResponse:
        return error_response(422, "INVALID_REQUEST", "The request did not match the API contract.")

    @app.post(
        "/v1/investigations",
        status_code=201,
        response_model=RunView,
        responses={
            200: {"model": RunView, "description": "Completed investigation replay"},
            202: {"model": RunView, "description": "An investigation already holds the lease"},
            401: {"model": ErrorResponse},
            403: {"model": ErrorResponse},
            404: {"model": ErrorResponse},
            409: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
    )
    def start_investigation(
        body: InvestigationRequest,
        response: Response,
        actor: Annotated[Actor, Depends(authenticate)],
        key: KeyHeader,
    ) -> RunView:
        result = workflow.submit(actor, body, key)
        response.status_code = result.status_code
        return result.body

    @app.get(
        "/v1/investigations/{run_id}",
        response_model=RunView,
        responses={
            401: {"model": ErrorResponse},
            404: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
        },
    )
    def investigation(
        run_id: Identifier, actor: Annotated[Actor, Depends(authenticate)]
    ) -> RunView:
        try:
            return RunView.model_validate(store.get_run(actor.tenant_id, run_id))
        except NotFound as error:
            raise unavailable() from error

    @app.get(
        "/v1/cases",
        response_model=list[CaseView],
        responses={
            401: {"model": ErrorResponse},
            403: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
        },
    )
    def cases(
        actor: Annotated[Actor, Depends(reviewer)],
        status: Annotated[CaseStatus, Query()] = "READY_FOR_REVIEW",
    ) -> list[CaseView]:
        return [CaseView.model_validate(row) for row in store.list_cases(actor.tenant_id, status)]

    @app.get(
        "/v1/cases/{case_id}",
        response_model=CaseView,
        responses={
            401: {"model": ErrorResponse},
            404: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
        },
    )
    def case(case_id: Identifier, actor: Annotated[Actor, Depends(authenticate)]) -> CaseView:
        try:
            return CaseView.model_validate(store.get_case(actor.tenant_id, case_id))
        except NotFound as error:
            raise unavailable() from error

    @app.post(
        "/v1/cases/{case_id}/decisions",
        response_model=DecisionView,
        responses={
            401: {"model": ErrorResponse},
            403: {"model": ErrorResponse},
            404: {"model": ErrorResponse},
            409: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
        },
    )
    def decide(
        case_id: Identifier,
        body: DecisionRequest,
        actor: Annotated[Actor, Depends(reviewer)],
        key: KeyHeader,
    ) -> DecisionView:
        try:
            decision = store.decide(
                actor.tenant_id,
                actor.actor_id,
                case_id,
                key,
                body.expected_version,
                body.decision,
                body.reason,
            )
        except NotFound as error:
            raise unavailable() from error
        except Conflict as error:
            raise ServiceError(
                409, "DECISION_CONFLICT", "The decision conflicts with the saved case or key."
            ) from error
        return DecisionView.model_validate(decision)

    return app
