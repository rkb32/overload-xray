"""Did the caller stop waiting, or did the call fail some other way?

A caller's span ending in ERROR does not by itself mean the caller gave up: the callee may have replied with an
error. Zombie work only exists when the caller LEFT. Rules decide the clear cases (cheap, deterministic, right for
standard instrumentation). A model (Jev) is only asked about error text the rules cannot place, only when the user
opts in, and anything it is not confident about stays UNKNOWN.
"""
import re
from collections.abc import Callable
from functools import lru_cache

ABANDONED = "abandoned"  # the caller stopped waiting: timeout, deadline, cancellation
FAILED = "failed"  # the call failed without the caller leaving: an error reply, a dropped connection
UNKNOWN = "unknown"

Asker = Callable[[str], str]

_ABANDONED_WORDS = ("timeout", "timed out", "deadline", "cancel", "abort")
_FAILED_WORDS = ("disconnect", "reset", "refused", "closed", "unreachable", "broken pipe")


def error_kind(error_text: str, http_status: int | None, ask: Asker | None = None) -> str:
    """Rules first; `ask` only sees text the rules cannot place."""
    if http_status is not None:
        return FAILED  # the callee replied, so the caller did not give up
    text = error_text.lower()
    if any(word in text for word in _ABANDONED_WORDS):
        return ABANDONED
    if any(word in text for word in _FAILED_WORDS):
        return FAILED
    return ask(error_text) if ask and error_text.strip() else UNKNOWN


# --- the Jev-backed asker ----------------------------------------------------------------------------------------

_URL = re.compile(r"https?://\S+")
_EMAIL = re.compile(r"\S+@\S+\.\S+")
_TOKEN = re.compile(r"\b[A-Za-z0-9_\-]{24,}\b")


def scrub(text: str, limit: int = 200) -> str:
    """Remove what must never leave the building (URLs, emails, long token-like strings) and cap the length."""
    text = _URL.sub("<url>", text)
    text = _EMAIL.sub("<email>", text)
    return _TOKEN.sub("<token>", text)[:limit]


def jev_asker(client=None, min_confidence: float = 0.8) -> Asker:
    """Build an `ask(error_text) -> label` backed by Jev (TypeSafe's typed-decision model).

    Needs TYPESAFE_API_KEY unless a client is passed in. Only the scrubbed error message is sent. Any failure
    (offline, rate limit, bad key) or low confidence gives UNKNOWN, never a guess. Answers are cached by text.
    """
    from typesafe_sdk import Choice, RetryPolicy, TypeSafeClient, TypeSafeError

    client = client or TypeSafeClient(timeout=5.0, retry=RetryPolicy(max_retries=1))
    question = Choice(
        instructions=(
            "An HTTP or RPC client call ended in an error. Judging only by the error message, did the CALLER stop "
            "waiting, or did the call fail some other way?"
        ),
        criteria={
            ABANDONED: "The caller gave up or cancelled: a timeout, a deadline exceeded, an abort by the client.",
            FAILED: "The call failed without the caller leaving: the callee returned an error, or the connection broke.",
            UNKNOWN: "The message does not say which.",
        },
    )

    @lru_cache(maxsize=1024)
    def ask(error_text: str) -> str:
        try:
            response = client.system_one(state={"error_message": scrub(error_text)}, questions={"kind": question})
        except TypeSafeError:
            return UNKNOWN
        answer = response.answers["kind"]
        trusted = answer.confidence >= min_confidence and answer.choice in (ABANDONED, FAILED)
        return answer.choice if trusted else UNKNOWN

    return ask
