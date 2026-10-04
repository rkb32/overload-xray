import os

import httpx
from fastapi import FastAPI, HTTPException
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

from demo.deadline import HEADER, SAFETY_MARGIN_S, remaining_s
from demo.tracing import setup_tracing

setup_tracing("service-a")
# FastAPI 0.14x ships its own OpenTelemetry support and, when OTEL_EXPORTER_OTLP_ENDPOINT is set, adds a second OTLP
# exporter at startup: every span was exported twice. The explicit instrumentation below owns tracing here.
app = FastAPI(telemetry={"auto_configure": False, "tracing": False, "metrics": False, "logs": False, "operation_spans": False})
FastAPIInstrumentor.instrument_app(app)
HTTPXClientInstrumentor().instrument()

B_URL = os.environ.get("B_URL", "http://127.0.0.1:8001")
TIMEOUT_S = float(os.environ.get("A_TIMEOUT_SECONDS", "1"))
RETRIES = int(os.environ.get("A_RETRIES", "2"))
PROPAGATE = os.environ.get("DEADLINE_PROPAGATION", "0") == "1"  # the fix: tell B how long we will wait


async def add_deadline_header(request: httpx.Request) -> None:
    budget_s = remaining_s(TIMEOUT_S) - SAFETY_MARGIN_S
    request.headers[HEADER] = str(max(1, int(budget_s * 1000)))


client = httpx.AsyncClient(
    timeout=TIMEOUT_S,
    limits=httpx.Limits(max_connections=2000, max_keepalive_connections=200),
    event_hooks={"request": [add_deadline_header]} if PROPAGATE else {},
)


@app.get("/order")
async def order():
    for _ in range(1 + RETRIES):
        try:
            response = await client.get(f"{B_URL}/work")
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError:
            continue  # naive retry: the same request again, no backoff, no retry budget
    raise HTTPException(status_code=504, detail="dependency failed")
