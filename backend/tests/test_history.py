import pytest

from app.history import SharedHistoryRecallSkill


@pytest.mark.parametrize(
    "message",
    [
        "I deployed commit 0123456789abcdef0123456789abcdef01234567.",
        "I deployed release 550e8400-e29b-41d4-a716-446655440000.",
        "I deployed build 01ARZ3NDEKTSV4RRFFQ69G5FAV.",
        "I deployed image sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef.",
        "I deployed package @continuity/shared-history@1.2.3.",
        "I deployed resource i-0abcdef1234567890 with PostgreSQL connection pooling.",
    ],
    ids=["git-sha", "uuid", "ulid", "image-digest", "package", "technical-prose"],
)
def test_normal_technical_text_remains_eligible_for_episodic_capture(message):
    assert SharedHistoryRecallSkill().actor_for(message) == "user"
