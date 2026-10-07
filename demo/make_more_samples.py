"""Write five more SYNTHETIC samples into xray/samples/, one file per traffic shape:

    python -m demo.make_more_samples

    deep-chain.json   zombie work propagates down a chain: web gives up at 1 s, billing keeps working to 2.02 s,
                      and its own call to risk already succeeded at 0.3 s. The waste is the middle layer's.
    fanout.json       cart reads its three shards in parallel and uses every answer: 3x fan-out, goodput 100%.
                      A deliberate shape, not a retry storm (the three calls hit different paths).
    queued.json       orders waited 3.0 s in a queue; the report counts the 0.5 s of real work, not the run time.
    hedged.json       gateway fires two overlapping GETs at the same origin. The retry map reads hedging
                      as 2 calls with 0 retries (an overlapping attempt is not a retry, DESIGN.md D14).
    clock-skew.json   billing's clock is 0.3 s ahead of web's: the callee starts before its caller, but the
                      call succeeds, so the tolerance must not invent zombie work. A clock-skew note fires.

Each file is OTLP/JSON, one export request per service per line, like the Collector's file exporter writes it.
"""
import json
import os
from datetime import datetime, timezone

SAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "xray", "samples")
BASE_NS = int(datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc).timestamp()) * 1_000_000_000
INTERNAL, SERVER, CLIENT = 1, 2, 3
TIMEOUT = {"code": 2, "message": "ReadTimeout: "}
OK = "OK"
UNSET = "UNSET"


class Trace:
    """Build one synthetic trace; ids are a per-trace block so they cannot collide with another sample's."""

    def __init__(self, trace_id: str, services: list[str], id_base: int):
        self.trace_id = trace_id
        self.spans_by_service: dict[str, list[dict]] = {s: [] for s in services}
        self._next = id_base

    def add(self, service, name, kind, parent, start_s, end_s, status=None, **attrs) -> str:
        span_id = f"{self._next:016x}"
        self._next += 1
        self.spans_by_service[service].append({
            "traceId": self.trace_id, "spanId": span_id, "parentSpanId": parent or "", "name": name, "kind": kind,
            "startTimeUnixNano": str(BASE_NS + round(start_s * 1e9)), "endTimeUnixNano": str(BASE_NS + round(end_s * 1e9)),
            "status": status or {},
            "attributes": [
                # Underscores in a keyword become dots (http_status_code -> http.status.code), EXCEPT the queue
                # wait key, whose underscore is part of its actual name: app.queue_wait_ms.
                {"key": "app.queue_wait_ms" if key == "app_queue_wait_ms" else key.replace("_", "."),
                 "value": {"intValue": str(v)} if isinstance(v, int) else {"stringValue": v}}
                for key, v in attrs.items()
            ],
        })
        return span_id

    def write(self, filename: str) -> None:
        out = os.path.join(SAMPLES, filename)
        lines = []
        for service, spans in self.spans_by_service.items():
            resource = {"attributes": [{"key": "service.name", "value": {"stringValue": service}}]}
            lines.append(json.dumps({"resourceSpans": [{"resource": resource, "scopeSpans": [{"spans": spans}]}]}))
        with open(out, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        print(f"wrote {sum(len(s) for s in self.spans_by_service.values())} spans to {out}")


def deep_chain() -> None:
    """web gives up on billing at 1 s; billing works on to 2.02 s, but its call to risk already succeeded."""
    trace = Trace("3c94ac49b8dc42ab8b6a51d0b7ad0f31", ["web", "billing", "risk"], 0x2000)
    root = trace.add("web", "GET /checkout", SERVER, None, 0.0, 1.0, {"code": 2},
                     http_method="GET", http_route="/checkout", http_status_code=504, app_queue_wait_ms="0")
    price = trace.add("web", "GET /price", CLIENT, root, 0.001, 1.0, TIMEOUT,
                      http_method="GET", http_url="http://billing:8020/price")
    billing_job = trace.add("billing", "GET /price", SERVER, price, 0.02, 2.02, http_method="GET", http_route="/price",
                            http_status_code=200, app_queue_wait_ms="0")
    risk = trace.add("billing", "GET /risk", CLIENT, billing_job, 0.10, 0.30, http_method="GET",
                     http_url="http://risk:8011/risk", http_status_code=200)
    trace.add("risk", "GET /risk", SERVER, risk, 0.12, 0.30, http_method="GET", http_route="/risk",
              http_status_code=200, app_queue_wait_ms="0")
    trace.write("deep-chain.json")


def fanout() -> None:
    """cart reads its three shards in parallel and uses every answer."""
    trace = Trace("4bf2c9d71e8a4f3ab5d9e2c7f6a1b8c3", ["cart", "shard-a", "shard-b", "shard-c"], 0x3000)
    root = trace.add("cart", "GET /cart", SERVER, None, 0.0, 1.8, http_method="GET", http_route="/cart",
                     http_status_code=200, app_queue_wait_ms="0")
    for i, (service, start) in enumerate([("shard-a", 0.1), ("shard-b", 0.15), ("shard-c", 0.2)]):
        call = trace.add("cart", "GET /shards/{id}", CLIENT, root, start, start + 0.8, http_method="GET",
                         http_url=f"http://{service}:801{i}/shards/{i + 1}", http_status_code=200)
        trace.add(service, "GET /shards/{id}", SERVER, call, start + 0.01, start + 0.8, http_method="GET",
                  http_route=f"/shards/{i + 1}", http_status_code=200, app_queue_wait_ms="0")
    trace.write("fanout.json")


def queued() -> None:
    """orders sat 3.0 s in a queue; the 3.5 s run time is only 0.5 s of work."""
    trace = Trace("7ef1d4a2c8b3465e9f0a5b7d3e1c8f4a", ["web", "orders"], 0x4000)
    root = trace.add("web", "POST /orders", SERVER, None, 0.0, 4.0, http_method="POST", http_route="/orders",
                     http_status_code=200, app_queue_wait_ms="0")
    call = trace.add("web", "POST", CLIENT, root, 0.01, 3.51, http_method="POST",
                     http_url="http://orders:8001/orders", http_status_code=200)
    trace.add("orders", "POST /orders", SERVER, call, 0.02, 3.52, http_method="POST", http_route="/orders",
              http_status_code=200, app_queue_wait_ms="3000")  # queued behind a spike: only 0.5 s of work
    trace.write("queued.json")


def hedged() -> None:
    """gateway fires two OVERLAPPING GETs at the same origin: hedging, not a retry (D14)."""
    trace = Trace("9a5c7e2b4d1f46a8b0c3d5e7f9a1b2c4", ["gateway", "origin"], 0x5000)
    root = trace.add("gateway", "GET /asset", SERVER, None, 0.0, 0.6, http_method="GET", http_route="/asset",
                     http_status_code=200, app_queue_wait_ms="0")
    first = trace.add("gateway", "GET /origin/asset", CLIENT, root, 0.1, 0.4, http_method="GET",
                      http_url="http://origin:8030/origin/asset", http_status_code=200)
    second = trace.add("gateway", "GET /origin/asset", CLIENT, root, 0.3, 0.6, http_method="GET",
                       http_url="http://origin:8030/origin/asset", http_status_code=200)
    trace.add("origin", "GET /origin/asset", SERVER, first, 0.12, 0.38, http_method="GET", http_route="/origin/asset",
              http_status_code=200, app_queue_wait_ms="0")
    trace.add("origin", "GET /origin/asset", SERVER, second, 0.32, 0.58, http_method="GET", http_route="/origin/asset",
              http_status_code=200, app_queue_wait_ms="0")
    trace.write("hedged.json")


def clock_skew() -> None:
    """billing's clock runs 0.3 s ahead: its span starts before web's, but the call succeeds."""
    trace = Trace("d2f8b6a4c1e9475d8a0b3c5e7f9a2d4b6", ["web", "billing"], 0x6000)
    root = trace.add("web", "GET /charge", SERVER, None, 0.0, 0.6, http_method="GET", http_route="/charge",
                     http_status_code=200, app_queue_wait_ms="0")
    call = trace.add("web", "GET", CLIENT, root, 0.001, 0.6, http_method="GET",
                     http_url="http://billing:8020/charge", http_status_code=200)
    trace.add("billing", "GET /charge", SERVER, call, -0.3, 0.25, http_method="GET", http_route="/charge",
              http_status_code=200, app_queue_wait_ms="0")  # started before the caller: the clocks disagree
    trace.write("clock-skew.json")


if __name__ == "__main__":
    deep_chain()
    fanout()
    queued()
    hedged()
    clock_skew()