"""Regression test for a bug that doubled every span in the Docker setup.

FastAPI 0.14x auto-configures OpenTelemetry from the environment: when OTEL_EXPORTER_OTLP_ENDPOINT is set it adds
its OWN OTLP exporter to the global provider at startup. The demo services already export through
demo.tracing.setup_tracing, so every span was sent twice. The services now pass telemetry={"auto_configure": False}.

It runs in a subprocess because the global tracer provider can only be set once per process.
"""
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PROBE = """
from fastapi.testclient import TestClient
from opentelemetry import trace
import {module} as service

with TestClient(service.app):  # entering the client runs the ASGI lifespan startup, where FastAPI configures telemetry
    pass
print(len(trace.get_tracer_provider()._active_span_processor._span_processors))
"""


@pytest.mark.parametrize("module", ["demo.service_a", "demo.service_b"])
def test_a_service_exports_each_span_through_exactly_one_processor(module, tmp_path):
    env = {
        **os.environ,
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:1",  # nothing listens here; we only count processors
        "SPANS_DIR": str(tmp_path),
    }

    result = subprocess.run(
        [sys.executable, "-c", PROBE.format(module=module)], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60
    )

    assert result.returncode == 0, result.stderr[-800:]
    assert result.stdout.strip().splitlines()[-1] == "1", "something added a second span processor at startup"
