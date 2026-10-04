"""Write xray/samples/layered-retries.json: a SYNTHETIC trace of retries stacked in two layers.

    python -m demo.make_layered_sample

One checkout request. `web` calls `orders` (POST /orders) and gives up after 2 s, three times. Every `orders` job calls
`payments` (POST /charge) and gives up after 0.7 s, three times. `payments` is slow (3 s), so every attempt times out and
every payments job keeps running. Result: three attempts one layer down, nine below that, for one checkout: and both
are writes. Real traces are the point of the product; this one only shows what the retry map looks like.
The file is OTLP/JSON, one export request per service per line, like the Collector's file exporter writes it.
"""
import json
import os
from datetime import datetime, timezone

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "xray", "samples", "layered-retries.json")
BASE_NS = int(datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc).timestamp()) * 1_000_000_000
TRACE_ID = "5b8aa5a2d2c872e8321cf37308d69df2"
INTERNAL, SERVER, CLIENT = 1, 2, 3
TIMEOUT = {"code": 2, "message": "ReadTimeout: "}

spans_by_service: dict[str, list[dict]] = {"web": [], "orders": [], "payments": []}
_next_id = [0x1000]


def new_id() -> str:
    _next_id[0] += 1
    return f"{_next_id[0]:016x}"


def attributes(**values):
    return [{"key": key.replace("_", "."), "value": {"intValue": str(v)} if isinstance(v, int) else {"stringValue": v}}
            for key, v in values.items()]


def add(service, name, kind, parent, start_s, end_s, status=None, **attrs):
    span_id = new_id()
    spans_by_service[service].append({
        "traceId": TRACE_ID, "spanId": span_id, "parentSpanId": parent or "", "name": name, "kind": kind,
        "startTimeUnixNano": str(BASE_NS + round(start_s * 1e9)), "endTimeUnixNano": str(BASE_NS + round(end_s * 1e9)),
        "status": status or {}, "attributes": attributes(**attrs),
    })
    return span_id


root = add("web", "GET /checkout", SERVER, None, 0.0, 6.35, {"code": 2}, http_method="GET", http_route="/checkout", http_status_code=504)

for k in range(3):  # web -> orders, three attempts, each abandoned after 2 s
    begin = 2.1 * k + 0.001
    attempt = add("web", "POST", CLIENT, root, begin, begin + 2.0, TIMEOUT, http_method="POST", http_url="http://orders:8001/orders")
    job_start = begin + 0.02
    job = add("orders", "POST /orders", SERVER, attempt, job_start, job_start + 2.15, http_method="POST", http_route="/orders",
              http_status_code=200, app_queue_wait_ms="0")
    for j in range(3):  # orders -> payments, three attempts, each abandoned after 0.7 s
        call_start = job_start + 0.01 + 0.71 * j
        call = add("orders", "POST", CLIENT, job, call_start, call_start + 0.7, TIMEOUT, http_method="POST",
                   http_url="http://payments:8002/charge")
        add("payments", "POST /charge", SERVER, call, call_start + 0.01, call_start + 3.0, http_method="POST", http_route="/charge",
            http_status_code=200, app_queue_wait_ms="0")

lines = []
for service, spans in spans_by_service.items():
    resource = {"attributes": [{"key": "service.name", "value": {"stringValue": service}}]}
    lines.append(json.dumps({"resourceSpans": [{"resource": resource, "scopeSpans": [{"spans": spans}]}]}))

with open(OUT, "w", encoding="utf-8") as handle:
    handle.write("\n".join(lines) + "\n")
print(f"wrote {sum(len(s) for s in spans_by_service.values())} spans to {OUT}")
