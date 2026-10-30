from __future__ import annotations

import os
import subprocess
import sys
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select
from test_api import client as client
from test_api import csrf, login

from app.models import (
    AgentEvent,
    AssistanceFeedback,
    AssistanceRun,
    Commitment,
    GrowthEvent,
    Thread,
    ThreadRevision,
    User,
)


@pytest.fixture()
def records(client):
    import app.main as main

    login(client)
    user_id = client.get("/api/auth/me").json()["id"]
    with main.SessionLocal() as db:
        other = User(username="other")
        db.add(other)
        db.flush()
        project = Thread(user_id=user_id, title="Private project", kind="project",
                         description="Old description", why="Old reason", next_action="Old step",
                         epistemic_status="inference", derivation="test")
        foreign = Thread(user_id=other.id, title="Other project", kind="project", derivation="test")
        db.add_all([project, foreign])
        db.flush()
        idea = Thread(user_id=user_id, title="Linked idea", kind="idea",
                      parent_thread_id=project.id, derivation="test")
        commitment = Commitment(user_id=user_id, title="Linked task", confidence=1,
                                related_thread_id=project.id, derivation="test")
        source = AgentEvent(user_id=user_id, external_id="source", kind="user_message",
                            request_fingerprint="f" * 64)
        db.add_all([idea, commitment, source])
        db.flush()
        run = AssistanceRun(user_id=user_id, event_id=source.id,
                            intervention_type="contextual_help", related_thread_id=project.id,
                            related_commitment_id=commitment.id, execution_status="delivered")
        db.add(run)
        db.commit()
        return {"project": project.id, "idea": idea.id, "foreign": foreign.id,
                "run": run.id, "commitment": commitment.id, "user": user_id}


def test_companion_shared_version_validation_and_guard(client):
    login(client)
    initial = client.get("/api/companion").json()
    assert initial == {"name": "Mica", "temperament": "calm", "version": 1,
                       "model_mode": "deterministic"}
    body = {"name": "  Pip  ", "temperament": "curious", "version": 1}
    assert client.patch("/api/companion", json=body).status_code == 403
    result = client.patch("/api/companion", json=body, headers=csrf(client))
    assert result.status_code == 200
    assert result.json()["name"] == "Pip"
    assert result.json()["version"] == 2
    assert client.patch("/api/companion", json=body, headers=csrf(client)).status_code == 409
    preferences = client.get("/api/preferences").json()
    assert preferences["version"] == 2
    assert client.patch("/api/preferences", json={**preferences, "version": 1},
                        headers=csrf(client)).status_code == 409
    for invalid in ({"name": " "}, {"name": "x" * 61}, {"temperament": "angry"}, {"version": 0}):
        assert client.patch("/api/companion", json={**body, **invalid},
                            headers=csrf(client)).status_code == 422
    login(client, username="another")
    assert client.get("/api/companion").json()["name"] == "Mica"


def test_active_context_validation_and_event_fields(client, records, monkeypatch):
    import app.main as main

    seen = []

    def handle(db, user_id, runtime_event):
        seen.append(runtime_event)
        return {"idempotent": False}

    monkeypatch.setattr(main.runtime, "handle", handle)
    body = {"content": "Explore this", "event_id": "active", "allow_memory_write": True}
    for thread_id in (records["foreign"], str(uuid4())):
        assert client.post("/api/chat/messages", json={**body, "active_thread_id": thread_id},
                           headers=csrf(client)).status_code == 404
    assert client.post("/api/chat/messages", json={**body, "active_thread_id": "not-a-uuid"},
                       headers=csrf(client)).status_code == 422
    assert not seen
    assert client.post("/api/chat/messages",
                       json={**body, "active_thread_id": records["project"]},
                       headers=csrf(client)).status_code == 201
    assert seen[-1].active_thread_id == records["project"]
    assert seen[-1].allow_memory_write is True
    assert client.post("/api/chat/messages", json={"content": "Hello", "event_id": "default"},
                       headers=csrf(client)).status_code == 201
    assert seen[-1].active_thread_id is None
    assert seen[-1].allow_memory_write is False
    assert client.delete(f"/api/threads/{records['project']}",
                         headers=csrf(client)).status_code == 204
    assert client.post("/api/chat/messages",
                       json={**body, "active_thread_id": records["project"]},
                       headers=csrf(client)).status_code == 404
    assert len(seen) == 2


def test_context_plates_are_linked_bounded_and_tenant_scoped(client, records):
    import app.main as main

    with main.SessionLocal() as db:
        db.add_all(Thread(user_id=records["user"], title=f"Idea {index}", kind="idea",
                          parent_thread_id=records["project"], derivation="test")
                   for index in range(22))
        db.add_all(Commitment(user_id=records["user"], title=f"Task {index}", confidence=1,
                             related_thread_id=records["project"], derivation="test")
                   for index in range(22))
        db.commit()
    response = client.get("/api/context/plates").json()
    assert records["foreign"] not in {item["id"] for item in response["items"]}
    project = next(item for item in response["items"] if item["id"] == records["project"])
    assert len(project["ideas"]) == len(project["commitments"]) == 20
    assert project["collection_meta"]["ideas"]["has_more"] is True
    assert project["epistemic_status"] == "inference"
    with main.SessionLocal() as db:
        db.add_all(Thread(user_id=records["user"], title=f"Thread {index}", derivation="test")
                   for index in range(60))
        db.commit()
    response = client.get("/api/context/plates").json()
    assert len(response["items"]) == 50
    assert response["has_more"] is True


def test_correction_revisions_cas_and_delete_scrubbing(client, records):
    import app.main as main

    path = f"/api/threads/{records['project']}"
    body = {"title": "New title", "why": " New reason ", "version": 1}
    result = client.patch(path, json=body, headers=csrf(client))
    assert result.status_code == 200
    assert result.json()["why"] == "New reason"
    assert result.json()["epistemic_status"] == "inference"
    revisions = client.get(path + "/revisions").json()["items"]
    assert [revision["version"] for revision in revisions] == [2, 1]
    assert revisions[1]["snapshot"]["title"] == "Private project"
    assert client.patch(path, json=body, headers=csrf(client)).status_code == 409
    assert client.patch(path, json={**body, "version": 2, "kind": "idea"},
                        headers=csrf(client)).status_code == 409
    assert len(client.get(path + "/revisions").json()["items"]) == 2
    result = client.patch(path, json={**body, "version": 2, "epistemic_status": "fact",
                                     "parent_thread_id": records["foreign"]}, headers=csrf(client))
    assert result.json()["epistemic_status"] == "fact"
    assert result.json()["parent_thread_id"] is None
    assert client.delete(path + "?version=1", headers=csrf(client)).status_code == 409
    assert client.delete(path + "?version=3", headers=csrf(client)).status_code == 204
    assert client.get(path + "/revisions").status_code == 404
    with main.SessionLocal() as db:
        thread = db.get(Thread, records["project"])
        assert (thread.title, thread.description, thread.why, thread.next_action) == (
            "[deleted]", "", "", "")
        revisions = db.scalars(select(ThreadRevision).where(
            ThreadRevision.thread_id == thread.id)).all()
        assert len(revisions) == 3
        assert all(revision.snapshot == {} for revision in revisions)


@pytest.mark.parametrize(("verdict", "outcome", "dimension", "event_type"), [
    ("helpful", "user_confirmed_helpful", "judgement", "helpfulness_reported"),
    ("completed", "user_reported_completed", "human_empowerment", "outcome_reported"),
    ("unhelpful", "user_confirmed_unhelpful", None, None),
    ("acknowledged", "acknowledged", None, None),
])
def test_feedback_is_idempotent_explicit_evidence_only(
    client, records, verdict, outcome, dimension, event_type,
):
    import app.main as main

    path = f"/api/assistance/{records['run']}/feedback"
    body = {"event_id": "feedback", "verdict": verdict}
    assert client.post(path, json=body).status_code == 403
    first = client.post(path, json=body, headers=csrf(client))
    assert first.status_code == 201
    replay = client.post(path, json=body, headers=csrf(client))
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert client.post(path, json={**body, "verdict": "helpful" if verdict != "helpful"
                                  else "unhelpful"}, headers=csrf(client)).status_code == 409
    history = client.get("/api/assistance").json()
    assert history["items"][0]["observed_outcome"] == outcome
    assert len(history["items"][0]["feedback"]) == 1
    assert len(history["growth"]) == (1 if dimension else 0)
    if dimension:
        assert history["growth"][0]["dimension"] == dimension
        assert history["growth"][0]["event_type"] == event_type
        assert history["growth"][0]["assessment"] == "milestone"
        assert history["growth"][0]["evidence_id"] == first.json()["id"]
    with main.SessionLocal() as db:
        assert db.get(Commitment, records["commitment"]).state == "open"
    assert client.get("/api/assistance?limit=51").status_code == 422
    login(client, username="other")
    assert client.post(path, json=body, headers=csrf(client)).status_code == 404
    assert client.get("/api/assistance").json()["items"] == []
    assert client.get("/api/assistance").json()["growth"] == []
    assert client.get(f"/api/threads/{records['project']}/revisions").status_code == 404


def test_feedback_growth_failure_rolls_back_all_evidence(client, records):
    import app.main as main

    def fail(*_args):
        raise RuntimeError("forced growth failure")

    event.listen(GrowthEvent, "before_insert", fail)
    try:
        with pytest.raises(RuntimeError, match="forced growth failure"):
            client.post(f"/api/assistance/{records['run']}/feedback",
                        json={"event_id": "failed", "verdict": "helpful"}, headers=csrf(client))
    finally:
        event.remove(GrowthEvent, "before_insert", fail)
    with main.SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(AssistanceFeedback)) == 0
        assert db.scalar(select(func.count()).select_from(GrowthEvent)) == 0
        assert db.get(AssistanceRun, records["run"]).observed_outcome == "unknown"


def test_feedback_event_is_bound_to_run_and_replay_does_not_overwrite_outcome(client, records):
    import app.main as main

    with main.SessionLocal() as db:
        source = AgentEvent(user_id=records["user"], external_id="second-source",
                            kind="user_message", request_fingerprint="f" * 64)
        db.add(source)
        db.flush()
        second = AssistanceRun(user_id=records["user"], event_id=source.id,
                               intervention_type="contextual_help")
        db.add(second)
        db.commit()
        second_id = second.id
    path = f"/api/assistance/{records['run']}/feedback"
    body = {"event_id": "bound-event", "verdict": "helpful"}
    assert client.post(path, json=body, headers=csrf(client)).status_code == 201
    assert client.post(f"/api/assistance/{second_id}/feedback", json=body,
                       headers=csrf(client)).status_code == 409
    assert client.post(path, json={"event_id": "later", "verdict": "unhelpful"},
                       headers=csrf(client)).status_code == 201
    assert client.post(path, json=body, headers=csrf(client)).status_code == 200
    with main.SessionLocal() as db:
        assert db.get(AssistanceRun, records["run"]).observed_outcome == "user_confirmed_unhelpful"
        assert db.get(AssistanceRun, second_id).observed_outcome == "unknown"
        assert db.scalar(select(func.count()).select_from(AssistanceFeedback)) == 2
        assert db.scalar(select(func.count()).select_from(GrowthEvent)) == 1


@pytest.mark.parametrize(("mode", "app_env", "configured", "expected"), [
    ("live", "test", True, None),
    ("live", "test", False, "Live model configuration is missing or invalid"),
    ("deterministic", "production", False, "Deterministic model disabled in production"),
])
def test_model_mode_startup(mode, app_env, configured, expected):
    env = {key: value for key, value in os.environ.items() if not key.startswith("MODEL_")}
    env.update(APP_ENV=app_env, MODEL_MODE=mode, DATABASE_URL="sqlite:///:memory:")
    if configured:
        env.update(MODEL_BASE_URL="https://example.com/v1", MODEL_API_KEY="test-secret",
                   MODEL_NAME="test-model", PROVIDER="openai_compatible")
    result = subprocess.run(
        [sys.executable, "-c", "from app.main import runtime; assert runtime.gateway is not None"],
        env=env, capture_output=True, text=True, timeout=20,
    )
    if expected:
        assert result.returncode != 0
        assert expected in result.stderr
    else:
        assert result.returncode == 0, result.stderr
    assert "test-secret" not in result.stdout + result.stderr
