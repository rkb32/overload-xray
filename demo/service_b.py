import asyncio
import os
import time

from fastapi import FastAPI, HTTPException
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from demo.deadline import DeadlineMiddleware, time_left_s
from demo.tracing import setup_tracing

setup_tracing("service-b")
# FastAPI 0.14x ships its own OpenTelemetry support and, when OTEL_EXPORTER_OTLP_ENDPOINT is set, adds a second OTLP
# exporter at startup: every span was exported twice. The explicit instrumentation below owns tracing here.
app = FastAPI(telemetry={"auto_configure": False, "tracing": False, "metrics": False, "logs": False, "operation_spans": False})
app.add_middleware(DeadlineMiddleware)  # only acts when a caller sends a deadline header
FastAPIInstrumentor.instrument_app(app)

WORK_SECONDS = float(os.environ.get("B_WORK_SECONDS", "3"))
CAPACITY = asyncio.Semaphore(int(os.environ.get("B_CONCURRENCY", "1000")))  # jobs that may run at once
SHED_DOOMED = os.environ.get("SHED_DOOMED", "0") == "1"  # refuse jobs that cannot finish before the deadline
SHED_MARGIN_S = 0.1
inflight = {"jobs": 0}  # running or queued, including leftover work nobody is waiting for


@app.get("/inflight")
async def inflight_jobs():
    return inflight


@app.get("/work")
async def work():
    span = trace.get_current_span()
    queued_at = time.perf_counter()
    started_at = None
    inflight["jobs"] += 1
    try:
        async with CAPACITY:
            started_at = time.perf_counter()
            left = time_left_s()
            if SHED_DOOMED and left is not None and left < WORK_SECONDS + SHED_MARGIN_S:
                raise HTTPException(status_code=503, detail="not enough time left to finish")
            # Stands in for a slow query. Without a deadline it never checks whether the caller is still there.
            await asyncio.sleep(WORK_SECONDS)
        return {"done": True}
    finally:
        inflight["jobs"] -= 1
        waited = (started_at or time.perf_counter()) - queued_at
        span.set_attribute("app.queue_wait_ms", round(waited * 1000))  # lets xray tell waiting from working
