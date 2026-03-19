"""Shared Prometheus metrics and FastAPI middleware for adapters and comms."""

from __future__ import annotations

import os
import shutil
import time
from typing import Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse
from fastapi.routing import APIRoute

# ---------------------------------------------------------------------------
# Multiprocess setup — must happen BEFORE importing metric types.
# When PROMETHEUS_MULTIPROC_DIR is set (Cloud Run with multiple workers),
# metrics are backed by shared mmap'd files so every worker's writes are
# visible at scrape time regardless of which worker handles /metrics.
# ---------------------------------------------------------------------------
_MULTIPROC_DIR = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
if _MULTIPROC_DIR:
    if os.path.exists(_MULTIPROC_DIR):
        shutil.rmtree(_MULTIPROC_DIR)
    os.makedirs(_MULTIPROC_DIR, exist_ok=True)

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    CONTENT_TYPE_LATEST,
)

# ---------------------------------------------------------------------------
# Registry — one per process so metrics survive hot-reload in dev.
# ProcessCollector is only registered in single-process mode; in multiprocess
# mode it reports per-worker values that don't aggregate correctly.
# ---------------------------------------------------------------------------
REGISTRY = CollectorRegistry()
if not _MULTIPROC_DIR:
    from prometheus_client import ProcessCollector

    ProcessCollector(registry=REGISTRY)
else:
    # In multiprocess mode ProcessCollector is skipped (per-worker values
    # don't aggregate), but the GMP sidecar needs process_start_time_seconds
    # to anchor cumulative metric timestamps.  Expose it manually.
    _PROCESS_START = Gauge(
        "process_start_time_seconds",
        "Start time of the process since unix epoch in seconds.",
        registry=REGISTRY,
        multiprocess_mode="min",
    )
    _PROCESS_START.set(time.time())

# ---------------------------------------------------------------------------
# Shared HTTP metrics (used by the middleware on both services)
# ---------------------------------------------------------------------------
HTTP_REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "Latency of HTTP requests by method, endpoint, and status code.",
    labelnames=["service", "method", "endpoint", "status_code"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
    registry=REGISTRY,
)

HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "Total HTTP requests by method, endpoint, and status code.",
    labelnames=["service", "method", "endpoint", "status_code"],
    registry=REGISTRY,
)

# ---------------------------------------------------------------------------
# Adapters-specific metrics
# ---------------------------------------------------------------------------
ORCHESTRA_GET_ASSISTANT_DURATION = Histogram(
    "orchestra_get_assistant_duration_seconds",
    "Time spent calling the Orchestra /admin/assistant endpoint. "
    "Use status='success' to filter to healthy requests only.",
    labelnames=[
        "lookup_type",
        "status",
    ],  # lookup_type: email|phone|id, status: success|error
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
    registry=REGISTRY,
)

BUILD_WEBHOOK_CONTEXT_DURATION = Histogram(
    "build_webhook_context_duration_seconds",
    "Total time from inbound adapter request to webhook context built. "
    "job_started='true' includes start_unity_job + replenish_idle_pool; "
    "job_started='false' is just get_assistant + contact validation. "
    "Use status='success' to filter to healthy requests only.",
    labelnames=[
        "channel",
        "job_started",
        "status",
    ],  # job_started: true|false, status: success|error
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
    registry=REGISTRY,
)

UNITY_JOBS_RUNNING = Gauge(
    "unity_jobs_running",
    "Number of Unity K8s jobs with unity-status=running, "
    "sampled on every inventory call (webhook + scheduled).",
    registry=REGISTRY,
    multiprocess_mode="livemax",
)

UNITY_JOBS_IDLE = Gauge(
    "unity_jobs_idle",
    "Number of Unity K8s jobs with unity-status=idle, "
    "sampled on every inventory call (webhook + scheduled).",
    registry=REGISTRY,
    multiprocess_mode="livemax",
)

STALE_JOBS_LAST_SWEEP = Gauge(
    "stale_jobs_last_sweep_count",
    "Number of stale jobs found in the most recent daily sweep. "
    "Resets to 0 at the start of each sweep.",
    registry=REGISTRY,
    multiprocess_mode="max",
)


# ---------------------------------------------------------------------------
# FastAPI middleware
# ---------------------------------------------------------------------------
def _resolve_endpoint(request: Request) -> str:
    """Return the route pattern (e.g. /twilio/call) rather than the concrete
    path, to keep label cardinality bounded."""
    route = request.scope.get("route")
    if route and isinstance(route, APIRoute):
        return route.path
    # Fallback: use the raw path
    return request.url.path


def add_metrics_middleware(app: FastAPI, service_name: str) -> None:
    """Register middleware that records HTTP request duration and count.

    Args:
        app: The FastAPI application.
        service_name: Label value identifying the service ('adapters' or 'comms').
    """

    @app.middleware("http")
    async def metrics_middleware(request: Request, call_next: Callable) -> Response:
        # Skip the /metrics endpoint itself
        if request.url.path == "/metrics":
            return await call_next(request)

        start = time.perf_counter()
        response: Response = await call_next(request)
        elapsed = time.perf_counter() - start

        endpoint = _resolve_endpoint(request)
        method = request.method
        status = str(response.status_code)

        HTTP_REQUEST_DURATION.labels(
            service=service_name,
            method=method,
            endpoint=endpoint,
            status_code=status,
        ).observe(elapsed)

        HTTP_REQUESTS_TOTAL.labels(
            service=service_name,
            method=method,
            endpoint=endpoint,
            status_code=status,
        ).inc()

        return response


def metrics_endpoint(_request: Request) -> Response:
    """Handler for GET /metrics — returns Prometheus text exposition format."""
    if _MULTIPROC_DIR:
        from prometheus_client.multiprocess import MultiProcessCollector

        registry = CollectorRegistry()
        MultiProcessCollector(registry)
        body = generate_latest(registry)
    else:
        body = generate_latest(REGISTRY)
    return PlainTextResponse(content=body, media_type=CONTENT_TYPE_LATEST)


def setup_metrics(app: FastAPI, service_name: str) -> None:
    """One-call setup: adds middleware + /metrics endpoint.

    Args:
        app: The FastAPI application.
        service_name: 'adapters' or 'comms'.
    """
    add_metrics_middleware(app, service_name)
    app.add_api_route(
        "/metrics",
        metrics_endpoint,
        methods=["GET"],
        include_in_schema=False,
    )
