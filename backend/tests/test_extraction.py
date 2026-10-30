import pytest

from app.runtime import StateExtractor


def test_extracts_commitment_with_deadline_and_effort():
    p = StateExtractor().extract("I need to send Patrick the plan before Friday; it takes 4 hours")
    assert (p.kind, p.title, p.deadline_hint, p.effort_minutes) == (
        "commitment",
        "send Patrick the plan",
        "Friday",
        240,
    )
    assert p.confidence >= 0.9


def test_imperative_send_with_deadline_is_a_commitment():
    proposal = StateExtractor().extract("send Patrick the plan before Friday")
    assert proposal.kind == "commitment"
    assert proposal.title == "send Patrick the plan"
    assert proposal.deadline_hint == "Friday"


def test_aspiration_is_not_a_commitment():
    assert StateExtractor().extract("I'd like to become a better painter someday").kind == "none"


def test_exploration_and_explicit_idea_are_threads():
    assert StateExtractor().extract("I'm considering learning boxing").kind == "thread"
    assert StateExtractor().extract("hang onto this idea: local inference club").kind == "thread"


def test_ambiguous_reminder_requests_clarification():
    assert StateExtractor().extract("remind me about that").kind == "clarification"


def test_transitions_are_detected():
    e = StateExtractor()
    assert e.extract("Actually, I finished that already").transition == "complete"
    assert e.extract("I am abandoning the portfolio idea").transition == "abandon"
    assert e.extract("Let's reactivate the portfolio idea").transition == "reactivate"
    assert e.extract("Make the portfolio idea dormant").transition == "dormant"


@pytest.mark.parametrize(
    "text",
    [
        "I haven't finished the frontend",
        "I haven’t finished the frontend",
        "I have not finished the frontend",
        "I didn't finish the frontend",
        "I did not finish the frontend",
        "The frontend is not finished",
    ],
)
def test_negated_completion_is_not_a_completion_transition(text):
    assert StateExtractor().extract(text).transition is None
