from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import evaluate
from app.gateway import GatewayError, ModelReply, OpenAICompatibleGateway
from app.models import Base, Message, User


@pytest.fixture
def fake_gateway(monkeypatch):
    calls = []
    gateway = OpenAICompatibleGateway(
        base_url="https://synthetic.invalid/v1", api_key="synthetic-key", model="same-model",
        max_output_tokens=123,
    )
    monkeypatch.setattr(OpenAICompatibleGateway, "from_env", classmethod(lambda cls: gateway))

    def complete(self, messages, tools):
        calls.append((self, messages, tools))
        # Deterministic echo tests plumbing and metrics, not model quality.
        memory = json.loads(messages[1]["content"])["memory"]
        return ModelReply(json.dumps(memory), [], self.model, 20, 10, 7, 0.001)

    monkeypatch.setattr(OpenAICompatibleGateway, "complete", complete)
    return gateway, calls


def by_id(report):
    return {case["scenario"]["id"]: case for case in report["results"]}


def test_offline_never_initializes_gateway_or_reads_personal_database(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Offline must not initialize or call a provider")

    monkeypatch.setattr(OpenAICompatibleGateway, "from_env", forbidden)
    monkeypatch.setattr(OpenAICompatibleGateway, "complete", forbidden)
    monkeypatch.setenv("DATABASE_URL", "this-is-not-a-valid-database-url")
    report = evaluate.run_evaluation()
    assert report == evaluate.run_evaluation()
    assert report["mode"] == "offline"
    assert report["human_review_required"] is True
    assert report["totals"]["scenarios"] == 6
    assert report["totals"]["calls_attempted"] == 0
    assert set(by_id(report)) == {
        "creative-continuity", "correction", "unknown-memory", "source-recall",
        "commitment", "intervention-feedback",
    }
    for case in report["results"]:
        for arm in case["arms"].values():
            assert arm["output"] is None
            assert arm["answer_metrics"] is None
            assert arm["usage"] is None
            assert arm["context_chars"] == len(json.dumps(arm["context"]))
            assert arm["context_chars"] <= evaluate.MAX_CONTEXT_CHARS
            assert arm["error"] is None
    for arm in report["totals"]["arms"].values():
        assert arm["outputs"] == 0
        assert arm["answer_metrics"] is None
        assert arm["cost_usd"]["known_sum"] is None


def test_corrected_state_and_retrieval_differ_from_recent_baseline():
    cases = by_id(evaluate.run_evaluation())
    correction = cases["correction"]["arms"]
    current = correction["context"]["retrieval_checks"]
    recent = correction["recent_only"]["retrieval_checks"]
    assert current["expected_matches"] == ["Cedar Hall"]
    assert current["forbidden_matches"] == []
    assert recent["expected_matches"] == []
    assert recent["forbidden_matches"] == ["Birch Hall"]
    assert correction["context"]["context"]["records"][0]["version"] == 2
    assert correction["context"]["context"]["recent_conversation"] == []
    assert len(correction["recent_only"]["context"]["recent_conversation"]) == 3
    for name, case in cases.items():
        if name != "correction":
            arms = case["arms"]
            messages = arms["context"]["context"]["recent_conversation"]
            assert messages == arms["recent_only"]["context"]["recent_conversation"]
            assert all("content" in message for message in messages)
    for name in ("creative-continuity", "source-recall", "commitment", "intervention-feedback"):
        arms = cases[name]["arms"]
        assert arms["context"]["retrieval_checks"]["expected_coverage"] == 1
        assert arms["context"]["retrieval_checks"]["source_id_coverage"] == 1
        assert arms["recent_only"]["retrieval_checks"]["expected_coverage"] == 0
        assert arms["recent_only"]["retrieval_checks"]["source_id_coverage"] == 0
    unknown = cases["unknown-memory"]["arms"]["context"]
    assert unknown["context"]["records"] == []
    assert unknown["retrieval_checks"]["expected_coverage"] is None
    feedback = cases["intervention-feedback"]["arms"]["context"]["context"]["feedback"]
    assert feedback["verdict"] == "unhelpful"
    assert feedback["intervention_type"] == "clarify"


def test_correction_fixture_reset_is_after_source_messages_and_allows_later_context():
    scenarios = json.loads(evaluate.SCENARIOS_PATH.read_text(encoding="utf-8"))
    scenario = next(case for case in scenarios if case["id"] == "correction")
    reset_at = datetime.fromisoformat(scenario["context_reset_at"])
    engine = create_engine("sqlite:///:memory:")
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            evaluate.seed_scenario(db, scenario)
            user = db.get(User, "synthetic-user")
            assert user.context_reset_at == reset_at.replace(tzinfo=None)
            assert all(message.created_at < user.context_reset_at
                       for message in db.scalars(select(Message)))
            for ordinal, instant in enumerate((reset_at, reset_at + timedelta(minutes=1)), 3):
                db.add(Message(
                    id=f"message-{ordinal}", user_id=user.id, conversation_id="synthetic-chat",
                    role="user", actor="user", content="Continue planning.", ordinal=ordinal,
                    created_at=instant,
                ))
            db.flush()
            bundle = evaluate.build_context(db, user.id, scenario["query"])
            assert [message["id"] for message in bundle.payload["recent_conversation"]] == [
                "message-4",
            ]
            assert "Cedar Hall" in json.dumps(bundle.payload)
            assert "Birch Hall" not in json.dumps(bundle.payload)
    finally:
        engine.dispose()


def test_live_equal_model_budgets_no_tools_and_no_answer_key_leak(fake_gateway, monkeypatch):
    gateway, calls = fake_gateway
    monkeypatch.setenv("DATABASE_URL", "never-open-the-personal-database")
    report = evaluate.run_evaluation(live=True)
    assert report["budget"] == {
        "calls_per_arm_per_scenario": 1, "max_context_chars": 12000, "tools": [],
        "provider": "openai_compatible", "model": "same-model", "max_output_tokens": 123,
    }
    assert len(calls) == report["totals"]["calls_attempted"] == 12
    for instance, messages, tools in calls:
        assert instance is gateway
        assert instance.max_output_tokens == 123
        assert instance.model == "same-model"
        assert tools == []
        assert messages[0] == {"role": "system", "content": evaluate.SYSTEM_PROMPT}
        assert len(messages) == 2
        assert set(json.loads(messages[1]["content"])) == {"memory", "query"}
    for index, case in enumerate(report["results"]):
        for offset, name in enumerate(case["call_order"]):
            payload = json.loads(calls[index * 2 + offset][1][1]["content"])
            assert payload == {"memory": case["arms"][name]["context"],
                               "query": case["scenario"]["query"]}
    for name, total in report["totals"]["arms"].items():
        assert total["calls_attempted"] == total["outputs"] == 6
        assert total["errors"] == 0
        assert total["input_tokens"] == {"known_sum": 120, "reported_calls": 6, "complete": True}
        assert total["output_tokens"]["known_sum"] == 60
        assert total["latency_ms"]["known_sum"] == 42
        assert total["cost_usd"]["known_sum"] == pytest.approx(0.006)
        assert total["context_chars"] == sum(
            case["arms"][name]["context_chars"] for case in report["results"]
        )
    recall = by_id(report)["source-recall"]["arms"]["context"]["answer_metrics"]
    assert recall["source_id_matches"] == ["message-river"]
    assert "message-river" in recall["available_evidence_id_mentions"]


@pytest.mark.parametrize("error", [
    GatewayError("unavailable"), RuntimeError("SECRET provider body"),
])
def test_live_errors_are_safe_no_retries_and_other_arm_continues(fake_gateway, monkeypatch, error):
    _, calls = fake_gateway

    def complete(self, messages, tools):
        calls.append((self, messages, tools))
        if len(calls) == 1:
            raise error
        return ModelReply("Unknown", [], self.model, None, None, 3, None)

    monkeypatch.setattr(OpenAICompatibleGateway, "complete", complete)
    report = evaluate.run_evaluation(live=True)
    assert len(calls) == 12
    assert "SECRET" not in json.dumps(report)
    assert "synthetic-key" not in json.dumps(report)
    totals = report["totals"]["arms"]
    assert totals["recent_only"]["errors"] == 1
    assert totals["recent_only"]["outputs"] == 5
    assert totals["context"]["outputs"] == 6
    assert totals["recent_only"]["answer_metrics"]["scored_cases"] == 5
    for total in totals.values():
        assert total["cost_usd"] == {"known_sum": None, "reported_calls": 0, "complete": False}
        assert total["input_tokens"]["known_sum"] is None


def test_cli_json_defaults_offline_and_live_config_error_is_safe(monkeypatch, capsys):
    assert evaluate.main([]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "offline"
    assert report["totals"]["scenarios"] == 6
    assert set(report["rubric"]) >= {"correctness", "unsupported_claims",
                                     "intervention_appropriateness"}

    def invalid_config(cls):
        raise GatewayError("invalid_config")

    monkeypatch.setattr(OpenAICompatibleGateway, "from_env", classmethod(invalid_config))
    assert evaluate.main(["--live"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["error"] == "invalid_config"
    assert report["totals"]["calls_attempted"] == 0
    assert len(report["results"]) == 6


def test_metrics_are_case_insensitive_proxies_not_semantic_judgement():
    scenario = {"expected_strings": ["Cedar Hall"], "forbidden_strings": ["Birch Hall"],
                "source_ids": ["source-1"]}
    metrics = evaluate.string_metrics("CEDAR HALL, not Birch Hall [source-1]", scenario)
    assert metrics["expected_coverage"] == 1
    assert metrics["forbidden_matches"] == ["Birch Hall"]
    assert metrics["source_id_coverage"] == 1
