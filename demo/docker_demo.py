"""The same one-request demo, but through Docker: A -> B in containers, spans shipped through an OpenTelemetry
Collector as OTLP/JSON, then read back by xray.

    python -m demo.docker_demo            # without the fix, then with it
    python -m demo.docker_demo fixed      # or just one of: baseline, fixed

Spans land in spans/docker-baseline and spans/docker-fixed.
"""
import os
import shutil
import subprocess
import sys
import time

import httpx

from demo.stack import ROOT

A_URL = "http://127.0.0.1:18000"
SERVICES = ["collector", "service-b", "service-a"]  # not the database: it should survive between demo runs


def compose(*args: str, **env: str) -> None:
    subprocess.run(["docker", "compose", *args], cwd=ROOT, env={**os.environ, **env}, check=True)


def run(mode: str) -> None:
    subdir = f"docker-{mode}"
    spans_dir = os.path.join(ROOT, "spans", subdir)
    shutil.rmtree(spans_dir, ignore_errors=True)
    os.makedirs(spans_dir)
    env = {"DEADLINE_PROPAGATION": "1" if mode == "fixed" else "0", "SPANS_SUBDIR": subdir}

    compose("up", "-d", "--build", *SERVICES, **env)
    try:
        for _ in range(60):  # wait for service A to answer
            try:
                httpx.get(f"{A_URL}/openapi.json", timeout=1.0)
                break
            except httpx.HTTPError:
                time.sleep(1)
        started = time.time()
        response = httpx.get(f"{A_URL}/order", timeout=30)
        print(f"[docker {mode}] user request -> HTTP {response.status_code} after {time.time() - started:.1f}s")
        time.sleep(5)  # let B finish or drop its leftover work, and let the spans reach the collector
    finally:
        compose("stop", *SERVICES, **env)  # a graceful stop flushes any spans still waiting to be exported
        compose("rm", "-f", *SERVICES, **env)


def main() -> None:
    choice = sys.argv[1] if len(sys.argv) > 1 else "both"
    for mode in ("baseline", "fixed") if choice == "both" else (choice,):
        run(mode)
    print("compare with:  python -m xray compare spans/docker-baseline spans/docker-fixed")


if __name__ == "__main__":
    main()
