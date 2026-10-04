"""Send ONE user request through A -> B, once without the fix and once with it, and keep the spans of both.

    python -m demo.run_demo            # both runs
    python -m demo.run_demo baseline   # or: fixed
"""
import os
import sys
import time

import httpx

from demo.stack import A_URL, running_stack

B_WORK_SECONDS = float(os.environ.get("B_WORK_SECONDS", "3"))


def run(mode: str) -> None:
    with running_stack(mode, mode):
        started = time.time()
        response = httpx.get(f"{A_URL}/order", timeout=30)
        print(f"[{mode}] user request -> HTTP {response.status_code} after {time.time() - started:.1f}s")
        # The user already has their answer. Wait so B can finish (or drop) its leftover work and flush its spans.
        time.sleep(B_WORK_SECONDS + 1)


def main() -> None:
    choice = sys.argv[1] if len(sys.argv) > 1 else "both"
    for mode in ("baseline", "fixed") if choice == "both" else (choice,):
        run(mode)
    print("compare with:  python -m xray compare spans/baseline spans/fixed")


if __name__ == "__main__":
    main()
