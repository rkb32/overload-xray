"""Open-loop load test: push user requests at A at fixed average rates and count how many succeed per second.

B can finish 4 jobs at once, 0.5s each = 8 jobs per second. A waits 1s per attempt and retries twice.
Three runs on the same arrivals:
    baseline  no deadline sent
    fixed     A sends its deadline; B cancels work when it passes
    shed      fixed, plus B refuses jobs that cannot finish in the time left

    python -m demo.loadtest
"""
import asyncio
import csv
import os
import random
import time

import httpx

from demo.stack import A_URL, ROOT, running_stack, wait_until_idle
from xray.analyze import analyze
from xray.spans import load_spans

MODES = ("baseline", "fixed", "shed")
RATES = [2, 5, 8, 12, 20]  # offered load, in user requests per second
STEP_SECONDS = 8
SERVICE_ENV = {"B_CONCURRENCY": "4", "B_WORK_SECONDS": "0.5"}


async def fire(client: httpx.AsyncClient, started: float, results: list[tuple[int, float]]) -> None:
    try:
        response = await client.get(f"{A_URL}/order")
        status = response.status_code
    except httpx.HTTPError:
        status = 0
    results.append((status, time.perf_counter() - started))  # (HTTP status, seconds since the step began)


async def run_step(rate: int, seconds: int) -> tuple[float, float]:
    """Open loop: requests arrive at random (Poisson) times whether or not earlier ones were answered.
    (A closed loop, where each user waits for their answer first, slows itself down and hides the collapse.)

    Returns (successes per second, share of requests that succeeded). The rate only counts successes that
    finished inside the window: answers that trickle in afterwards (from retries) would otherwise push the
    rate above what B can physically do."""
    rng = random.Random(rate)  # the same arrival times in every mode
    results: list[tuple[int, float]] = []
    async with httpx.AsyncClient(timeout=30, limits=httpx.Limits(max_connections=2000)) as client:
        started = time.perf_counter()
        tasks = []
        arrival = rng.expovariate(rate)
        while arrival < seconds:
            await asyncio.sleep(max(0.0, started + arrival - time.perf_counter()))
            tasks.append(asyncio.create_task(fire(client, started, results)))
            arrival += rng.expovariate(rate)
        await asyncio.gather(*tasks)
    ok = [done for status, done in results if status == 200]
    return sum(1 for done in ok if done <= seconds) / seconds, len(ok) / max(1, len(results))


def run_mode(mode: str) -> list[tuple[float, float]]:
    steps = []
    with running_stack(mode, f"load-{mode}", **SERVICE_ENV):
        asyncio.run(run_step(1, 3))  # warm-up: connection setup and lazy imports
        wait_until_idle()
        for rate in RATES:
            steps.append(asyncio.run(run_step(rate, STEP_SECONDS)))
            drain = wait_until_idle()  # every step starts from an idle system, so steps do not poison each other
            per_second, share = steps[-1]
            print(
                f"[{mode}] offered {rate:>2}/s -> {per_second:.1f} succeeded/s, {share:.0%} of requests"
                f"   (leftover work took {drain:.0f}s)",
                flush=True,
            )
        time.sleep(1)
    return steps


def main() -> None:
    results = {mode: run_mode(mode) for mode in MODES}

    print("\nsucceeded per second (B can finish about 8 jobs per second)")
    print("offered/s " + "".join(f"{mode:>12}" for mode in MODES))
    for i, rate in enumerate(RATES):
        print(f"{rate:>9} " + "".join(f"{results[mode][i][0]:>12.1f}" for mode in MODES))
    print("\nshare of requests that succeeded")
    print("offered/s " + "".join(f"{mode:>12}" for mode in MODES))
    for i, rate in enumerate(RATES):
        print(f"{rate:>9} " + "".join(f"{results[mode][i][1]:>12.0%}" for mode in MODES))

    os.makedirs(os.path.join(ROOT, "results"), exist_ok=True)
    with open(os.path.join(ROOT, "results", "loadtest.csv"), "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["offered_per_s", *(f"{mode}_{field}" for mode in MODES for field in ("ok_per_s", "success_share"))])
        writer.writerows(
            (rate, *(round(value, 3) for mode in MODES for value in results[mode][i])) for i, rate in enumerate(RATES)
        )

    print("\nwhat the traces say (all steps combined; work = time B spent running jobs, not waiting):")
    for mode in MODES:
        report = analyze(load_spans(os.path.join(ROOT, "spans", f"load-{mode}")))
        print(
            f"  {mode:<9} amplification {report.amplification:.1f}x   "
            f"B work {report.total_work_ns / 1e9:.0f}s   goodput {report.goodput:.0%}   "
            f"zombie tail {report.tail_ns / 1e9:.0f}s"
        )


if __name__ == "__main__":
    main()
