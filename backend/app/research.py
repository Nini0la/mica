"""Bounded goal-aware Tavily research through existing Mica tools and response traces."""

from __future__ import annotations

import ipaddress
import json
import os
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .models import AssistanceRun, Commitment, Thread


class ResearchGoal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal_id: str = Field(min_length=1, max_length=36)
    query: str = Field(min_length=3, max_length=350)
    verify_top_source: bool = True


def public_url(value):
    if not isinstance(value, str) or len(value) > 1500:
        return False
    try:
        url = urlsplit(value)
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            return False
        if url.port not in {None, 443} or url.hostname.lower().endswith(
            (".local", ".internal", ".localhost")
        ):
            return False
        if "." not in url.hostname:
            return False
        try:
            return ipaddress.ip_address(url.hostname).is_global
        except ValueError:
            return True
    except ValueError:
        return False


def request_tavily(path, payload, key):
    with httpx.Client(timeout=6, trust_env=False, follow_redirects=False) as client:
        with client.stream(
            "POST",
            "https://api.tavily.com/" + path,
            headers={"Authorization": "Bearer " + key},
            json=payload,
        ) as response:
            if response.status_code != 200:
                raise ValueError("research_unavailable")
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > 512_000:
                    raise ValueError("research_response_too_large")
    return json.loads(body)


def fetch(query, verify, key):
    started = time.monotonic()
    data = request_tavily(
        "search",
        {
            "query": query,
            "search_depth": "basic",
            "max_results": 3,
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
            "include_usage": True,
            "include_published_date": True,
            "auto_parameters": False,
        },
        key,
    )
    sources = [
        {
            "title": str(row.get("title", ""))[:250],
            "url": row["url"],
            "snippet": str(row.get("content", ""))[:1400],
            "published_date": row.get("published_date"),
            "verification": "search_snippet_only",
        }
        for row in data.get("results", [])[:3]
        if public_url(row.get("url"))
    ]
    calls = [
        {
            "endpoint": "search",
            "request_id": data.get("request_id"),
            "usage": data.get("usage"),
            "response_time": data.get("response_time"),
        }
    ]
    if verify and sources:
        # Prefer a publicly readable source over common login/anti-bot job aggregators.
        target = next(
            (
                source
                for source in sources
                if not any(
                    (urlsplit(source["url"]).hostname or "").endswith(host)
                    for host in ("indeed.com", "linkedin.com")
                )
            ),
            sources[0],
        )
        try:
            extracted = request_tavily(
                "extract",
                {
                    "urls": [target["url"]],
                    "query": query,
                    "extract_depth": "basic",
                    "chunks_per_source": 3,
                    "format": "text",
                    "timeout": 4,
                    "include_usage": True,
                },
                key,
            )
            calls.append(
                {
                    "endpoint": "extract",
                    "request_id": extracted.get("request_id"),
                    "usage": extracted.get("usage"),
                    "response_time": extracted.get("response_time"),
                    "failed_urls": [r.get("url") for r in extracted.get("failed_results", [])],
                }
            )
            for row in extracted.get("results", []):
                if row.get("url") == target["url"] and row.get("raw_content"):
                    target["extracted_text"] = row["raw_content"][:2600]
                    target["verification"] = "source_text_extracted"
                    target["snippet"] = ""  # Do not mix search chunks with the extracted passage.
        except (ValueError, httpx.HTTPError):
            calls.append({"endpoint": "extract", "status": "unavailable"})
    return {
        "sources": sources,
        "provider_calls": calls,
        "latency_ms": int((time.monotonic() - started) * 1000),
    }


def execute(db, owner, args, context, now):
    key = os.getenv("TAVILY_API_KEY")
    if not key:
        return {"status": "rejected", "reason": "research_not_configured"}
    goal = db.scalar(
        select(Thread).where(
            Thread.user_id == owner, Thread.id == args.goal_id, Thread.deleted.is_(False)
        )
    )
    if goal is None:
        goal = db.scalar(
            select(Commitment).where(
                Commitment.user_id == owner,
                Commitment.id == args.goal_id,
                Commitment.deleted.is_(False),
            )
        )
    if goal is None:
        return {
            "status": "rejected",
            "reason": "unknown_research_goal",
            "question": "Which saved goal should this research support?",
        }
    # Current-turn selected context, not an arbitrary private record supplied by a model.
    selected = {r.get("id") for r in context.get("records", [])}
    selected.update(
        r.get("id") for r in context.get("conversation_state", {}).get("commitments", [])
    )
    if goal.id not in selected:
        return {
            "status": "rejected",
            "reason": "goal_not_in_context",
            "question": "Please name the goal you'd like me to research.",
        }
    recent = db.scalars(
        select(AssistanceRun.tool_results).where(
            AssistanceRun.user_id == owner, AssistanceRun.created_at >= now - timedelta(days=1)
        )
    )
    count = sum(
        item.get("tool") == "research_goal" for results in recent for item in (results or [])
    )
    if count >= int(os.getenv("MICA_RESEARCH_DAILY_LIMIT", "60")):
        return {
            "status": "rejected",
            "reason": "research_daily_limit",
            "question": "Today's research allowance is used. Please try again tomorrow.",
        }
    payload = {"query": args.query, "verify": args.verify_top_source, "key": key}
    try:
        result = subprocess.run(
            [sys.executable, "-m", "app.research"],
            input=json.dumps(payload).encode(),
            capture_output=True,
            timeout=12,
            cwd=Path(__file__).resolve().parent.parent,
        )
        if result.returncode or len(result.stdout) > 32_000:
            raise ValueError("research_unavailable")
        evidence = json.loads(result.stdout)
        if evidence.get("error"):
            raise ValueError("research_unavailable")
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return {
            "status": "rejected",
            "reason": "research_unavailable",
            "question": "Research is unavailable right now. I haven't verified any new findings.",
        }
    goal_data = {"id": goal.id, "title": goal.title}
    if isinstance(goal, Thread):
        goal_data.update(
            description=goal.description, purpose=goal.why, next_action=goal.next_action
        )
    else:
        goal_data.update(
            purpose=goal.purpose, constraints=goal.constraints, next_action=goal.next_action
        )
    return {
        "status": "read",
        "kind": "research",
        "provider": "tavily",
        "query": args.query,
        "retrieved_at": now.isoformat(),
        "provider_calls": evidence["provider_calls"],
        "latency_ms": evidence["latency_ms"],
        "context": {
            "goal": goal_data,
            "sources": evidence["sources"],
            "limitations": "Search/extracted text is source evidence, not proof an opportunity "
            "is still open or meets every constraint. "
            "Distinguish facts, fit assessments and unknowns. Aggregator pages mix different "
            "listings: never combine their salary, seniority or eligibility attributes. "
            "An extracted page is not independent verification of an employer's claims.",
        },
    }


if __name__ == "__main__":
    try:
        incoming = json.loads(sys.stdin.buffer.read(8000))
        answer = fetch(incoming["query"], incoming["verify"], incoming["key"])
        print(json.dumps(answer))
    except Exception:
        print(json.dumps({"error": "research_unavailable"}))
