from typesafe_sdk import ChoiceAnswer, SystemOneResponse, TypeSafeError, Usage

from xray.classify import ABANDONED, FAILED, UNKNOWN, error_kind, jev_asker, scrub


def never_ask(text):
    raise AssertionError(f"the model must not be asked about {text!r}")


class FakeJev:
    """Stands in for TypeSafeClient but returns the SDK's real response objects."""

    def __init__(self, choice=ABANDONED, confidence=0.95, error=None):
        self.choice, self.confidence, self.error = choice, confidence, error
        self.sent = []

    def system_one(self, state, questions, **kwargs):
        self.sent.append(state)
        if self.error:
            raise self.error
        probabilities = {ABANDONED: 0.0, FAILED: 0.0, UNKNOWN: 0.0, self.choice: self.confidence}
        answer = ChoiceAnswer(choice=self.choice, confidence=self.confidence, probabilities=probabilities)
        return SystemOneResponse(model="jev-test", usage=Usage(), answers={"kind": answer})


# --- rules ---------------------------------------------------------------------------------------------------

def test_an_http_reply_means_the_caller_did_not_give_up():
    assert error_kind("", 503, never_ask) == FAILED


def test_timeouts_and_cancellations_mean_the_caller_left():
    assert error_kind("ReadTimeout: ", None, never_ask) == ABANDONED
    assert error_kind("context deadline exceeded", None, never_ask) == ABANDONED


def test_a_dropped_connection_is_a_failure_not_an_abandonment():
    assert error_kind("RemoteProtocolError: Server disconnected without sending a response.", None, never_ask) == FAILED


def test_unplaceable_text_without_a_model_is_unknown():
    assert error_kind("upstream error 9031 from pricing engine", None) == UNKNOWN
    assert error_kind("", None, never_ask) == UNKNOWN  # nothing to ask about


def test_the_model_is_only_asked_when_the_rules_cannot_place_the_text():
    asked = []
    verdict = error_kind("upstream error 9031 from pricing engine", None, lambda text: asked.append(text) or FAILED)

    assert verdict == FAILED and asked == ["upstream error 9031 from pricing engine"]


# --- the Jev-backed asker ------------------------------------------------------------------------------------

def test_a_confident_answer_is_used():
    assert jev_asker(FakeJev(ABANDONED, 0.95))("caller walked away") == ABANDONED
    assert jev_asker(FakeJev(FAILED, 0.9))("callee exploded") == FAILED


def test_a_hesitant_answer_stays_unknown():
    assert jev_asker(FakeJev(ABANDONED, 0.55))("hmm") == UNKNOWN


def test_the_model_saying_unknown_is_unknown():
    assert jev_asker(FakeJev(UNKNOWN, 0.99))("hmm") == UNKNOWN


def test_any_sdk_failure_fails_safe_to_unknown():
    assert jev_asker(FakeJev(error=TypeSafeError("offline")))("whatever") == UNKNOWN


def test_the_same_text_is_only_sent_once():
    client = FakeJev()
    ask = jev_asker(client)

    ask("same message")
    ask("same message")

    assert len(client.sent) == 1


def test_urls_emails_and_tokens_never_leave_the_machine():
    client = FakeJev()
    secret = "fake_credential_9f8e7d6c5b4a3921"  # fake, and not shaped like a vendor's key, so secret scanners ignore it

    jev_asker(client)(f"call to https://internal.example.com/pay?token={secret} for bob@corp.example failed {secret}")

    (state,) = client.sent
    sent = state["error_message"]
    assert "internal.example.com" not in sent and "bob@corp" not in sent and secret not in sent


def test_scrub_caps_the_length():
    assert len(scrub("x " * 500)) == 200
