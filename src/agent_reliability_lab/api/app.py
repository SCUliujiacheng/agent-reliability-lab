"""FastAPI application factory with lifespan-owned durable dependencies."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, cast
from uuid import UUID

from fastapi import FastAPI, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware

from agent_reliability_lab.api.errors import (
    RequestBodyLimitMiddleware,
    install_error_handlers,
)
from agent_reliability_lab.api.schemas import (
    ApprovalRequest,
    EvaluationCreateRequest,
    EvaluationListResponse,
    EvaluationResponse,
    HealthResponse,
    RunCreateRequest,
    RunListResponse,
    RunResponse,
    ScenarioListResponse,
    TracePageResponse,
)
from agent_reliability_lab.api.services import ApiContainer
from agent_reliability_lab.config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create an isolated HTTP application around one lifespan-owned store."""
    configured = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        container = ApiContainer(configured)
        app.state.container = container
        try:
            yield
        finally:
            container.store.close()

    app = FastAPI(
        title="Agent Reliability Lab",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_bytes=configured.max_request_body_bytes,
    )
    if configured.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(configured.cors_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Content-Type"],
        )
    install_error_handlers(app)

    @app.get("/health", response_model=HealthResponse)
    def health(request: Request) -> HealthResponse:
        return _container(request).health.ready()

    @app.get(
        "/v1/scenarios",
        response_model=ScenarioListResponse,
        response_model_exclude_none=True,
    )
    def list_scenarios(request: Request) -> ScenarioListResponse:
        return _container(request).catalog.list()

    @app.post(
        "/v1/runs",
        response_model=RunResponse,
        response_model_exclude_none=True,
        status_code=status.HTTP_201_CREATED,
    )
    def create_run(body: RunCreateRequest, request: Request) -> RunResponse:
        return _container(request).runs.start(body.scenario_id, body.mode)

    @app.get(
        "/v1/runs",
        response_model=RunListResponse,
        response_model_exclude_none=True,
    )
    def list_runs(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
    ) -> RunListResponse:
        return _container(request).queries.list(limit=limit)

    @app.get(
        "/v1/runs/{run_id}",
        response_model=RunResponse,
        response_model_exclude_none=True,
    )
    def get_run(run_id: UUID, request: Request) -> RunResponse:
        return _container(request).queries.get(run_id)

    @app.get(
        "/v1/runs/{run_id}/trace",
        response_model=TracePageResponse,
        response_model_exclude_none=True,
    )
    def get_trace(
        run_id: UUID,
        request: Request,
        after_sequence: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
    ) -> TracePageResponse:
        return _container(request).queries.trace(
            run_id, after_sequence=after_sequence, limit=limit
        )

    @app.post(
        "/v1/runs/{run_id}/approvals",
        response_model=RunResponse,
        response_model_exclude_none=True,
    )
    def approve_run(
        run_id: UUID, body: ApprovalRequest, request: Request
    ) -> RunResponse:
        return _container(request).runs.approve(
            run_id,
            actor=body.actor,
            allow=body.allow,
            reason=body.reason,
        )

    @app.post(
        "/v1/runs/{run_id}/resume",
        response_model=RunResponse,
        response_model_exclude_none=True,
    )
    def resume_run(run_id: UUID, request: Request) -> RunResponse:
        return _container(request).runs.resume(run_id)

    @app.get(
        "/v1/evaluations",
        response_model=EvaluationListResponse,
        response_model_exclude_none=True,
    )
    def list_evaluations(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=100)] = 10,
    ) -> EvaluationListResponse:
        return _container(request).evaluations.list(limit=limit)

    @app.post(
        "/v1/evaluations",
        response_model=EvaluationResponse,
        response_model_exclude_none=True,
        status_code=status.HTTP_201_CREATED,
    )
    def create_evaluation(
        body: EvaluationCreateRequest, request: Request
    ) -> EvaluationResponse:
        return _container(request).evaluations.create(body.suite)

    @app.get(
        "/v1/evaluations/{evaluation_id}",
        response_model=EvaluationResponse,
        response_model_exclude_none=True,
    )
    def get_evaluation(evaluation_id: UUID, request: Request) -> EvaluationResponse:
        return _container(request).evaluations.get(evaluation_id)

    return app


def _container(request: Request) -> ApiContainer:
    return cast(ApiContainer, request.app.state.container)


app = create_app()

__all__ = ["app", "create_app"]
