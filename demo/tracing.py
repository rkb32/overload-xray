import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor

DEFAULT_SPANS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "spans")


def setup_tracing(service_name: str) -> None:
    """Send this process's spans somewhere xray can read them.

    With OTEL_EXPORTER_OTLP_ENDPOINT set (the Docker setup) they go to an OpenTelemetry Collector, the way real
    systems do it. Without it, each finished span is appended to $SPANS_DIR/<service>.jsonl, one JSON object per line.
    """
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        # The exporter reads its endpoint from the environment. Batching keeps export off the request path.
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(), schedule_delay_millis=500))
    else:
        spans_dir = os.environ.get("SPANS_DIR", DEFAULT_SPANS_DIR)
        os.makedirs(spans_dir, exist_ok=True)
        out = open(os.path.join(spans_dir, f"{service_name}.jsonl"), "w", buffering=1, encoding="utf-8")
        exporter = ConsoleSpanExporter(
            service_name=service_name,
            out=out,
            formatter=lambda span: span.to_json(indent=None) + "\n",
        )
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
