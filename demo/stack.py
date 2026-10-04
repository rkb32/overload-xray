"""Start the two demo services as real processes, each writing its own spans file."""
import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
A_URL = "http://127.0.0.1:8000"
B_URL = "http://127.0.0.1:8001"


def _wait_up(url: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            httpx.get(url, timeout=1.0)  # any HTTP answer means the server is up
            return
        except httpx.HTTPError:
            time.sleep(0.2)
    raise RuntimeError(f"{url} did not come up")


def wait_until_idle(timeout: float = 90.0) -> float:
    """Block until B has nothing running or queued (leftover zombie work included). Returns seconds waited."""
    started = time.time()
    while time.time() - started < timeout:
        if httpx.get(f"{B_URL}/inflight", timeout=5).json()["jobs"] == 0:
            break
        time.sleep(0.5)
    return time.time() - started


def _start(module: str, port: int, env: dict) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", module, "--port", str(port), "--log-level", "warning"],
        cwd=ROOT,
        env=env,
    )


@contextmanager
def running_stack(mode: str, name: str, **extra_env: str):
    """Spans go to spans/<name>/. Modes:
    baseline  A sends no deadline
    fixed     A sends its deadline; B cancels work when it passes
    shed      fixed, plus B refuses jobs that cannot finish in the time left
    """
    spans_dir = os.path.join(ROOT, "spans", name)
    shutil.rmtree(spans_dir, ignore_errors=True)
    env = os.environ.copy()
    env.update(
        SPANS_DIR=spans_dir,
        DEADLINE_PROPAGATION="1" if mode in ("fixed", "shed") else "0",
        SHED_DOOMED="1" if mode == "shed" else "0",
        OTEL_PYTHON_FASTAPI_EXCLUDED_URLS="openapi.json,inflight",  # keep probes out of the traces
        **extra_env,
    )
    procs = [_start("demo.service_b:app", 8001, env), _start("demo.service_a:app", 8000, env)]
    try:
        _wait_up(f"{B_URL}/openapi.json")
        _wait_up(f"{A_URL}/openapi.json")
        yield spans_dir
    finally:
        for proc in procs:
            proc.terminate()
        for proc in procs:
            proc.wait(timeout=10)
