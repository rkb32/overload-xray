from xray.limits import RateLimiter


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_it_allows_up_to_the_limit_then_refuses():
    limiter = RateLimiter(max_events=3, per_seconds=60, clock=Clock())

    assert [limiter.allow("a") for _ in range(4)] == [True, True, True, False]


def test_each_key_has_its_own_budget():
    limiter = RateLimiter(max_events=1, per_seconds=60, clock=Clock())

    assert limiter.allow("a") and limiter.allow("b") and not limiter.allow("a")


def test_the_budget_comes_back_as_time_passes():
    clock = Clock()
    limiter = RateLimiter(max_events=1, per_seconds=60, clock=clock)
    assert limiter.allow("a") and not limiter.allow("a")

    clock.now = 61

    assert limiter.allow("a")


def test_retry_after_says_how_long_to_wait():
    clock = Clock()
    limiter = RateLimiter(max_events=1, per_seconds=60, clock=clock)
    limiter.allow("a")
    clock.now = 20

    assert 40 <= limiter.retry_after("a") <= 41
    assert limiter.retry_after("never-seen") == 0


def test_memory_stays_bounded_when_an_attacker_uses_endless_keys():
    limiter = RateLimiter(max_events=1, per_seconds=60, max_keys=100, clock=Clock())

    for i in range(10_000):
        limiter.allow(f"attacker-{i}")

    assert len(limiter._events) <= 100
