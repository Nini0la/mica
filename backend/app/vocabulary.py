from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .daily import local_date_for
from .models import ProvenanceLink, User, VocabularyAttempt, VocabularyItem
from .persistence import append_message, get_or_create_conversation

POLICY_VERSION = "vocab-v1"
SOURCE = "continuity-curated-lexicon"
SOURCE_VERSION = "2026-10-01"
MAX_ANSWER = 1000
CORRECTION_WINDOW = timedelta(hours=24)
LEXICON = (
    (
        "inchoate",
        "only partly in existence; imperfectly formed",
        "The proposal was still inchoate, but its central idea was clear.",
    ),
    (
        "lucid",
        "expressed clearly; easy to understand",
        "Her lucid explanation made the difficult idea approachable.",
    ),
    (
        "salient",
        "most noticeable or important",
        "The summary captured the most salient risk.",
    ),
    (
        "tacit",
        "understood without being stated",
        "They worked with a tacit agreement to share credit.",
    ),
)


def normalize(value: str) -> str:
    value = value.casefold().replace("’", "'")
    value = re.sub(r"[^a-z0-9\s'-]", " ", value)
    return " ".join(value.split())


class VocabularyLearningSkill:
    name = "vocabulary-learning"

    def morning_section(
        self, db: Session, user_id: str, event_id: str, now: datetime, local_date: date
    ) -> dict[str, Any]:
        due = db.scalar(
            select(VocabularyItem)
            .where(
                VocabularyItem.user_id == user_id,
                VocabularyItem.deleted.is_(False),
                VocabularyItem.mastery_state != "mastered",
                VocabularyItem.next_review_at <= now,
            )
            .order_by(
                VocabularyItem.correct_recalls,
                VocabularyItem.last_reviewed_at.asc().nullsfirst(),
                VocabularyItem.introduced_at,
                VocabularyItem.id,
            )
            .with_for_update()
        )
        if due is not None:
            due.exposure_count += 1
            due.updated_at = now
            due.version += 1
            return self._section(due, "review", "weak word due for review")

        known = set(
            db.scalars(
                select(VocabularyItem.normalized_word).where(
                    VocabularyItem.user_id == user_id, VocabularyItem.deleted.is_(False)
                )
            )
        )
        entry = next((entry for entry in LEXICON if entry[0] not in known), None)
        if entry is None:
            newest = db.scalar(
                select(VocabularyItem)
                .where(VocabularyItem.user_id == user_id, VocabularyItem.deleted.is_(False))
                .order_by(VocabularyItem.introduced_at.desc())
            )
            return self._section(newest, "rest", "no weak word is due") if newest else {}
        word, definition, example = entry
        item = VocabularyItem(
            user_id=user_id,
            normalized_word=word,
            display_word=word,
            definition=definition,
            natural_example=example,
            introduced_at=now,
            introduced_local_date=local_date,
            recall_attempts=0,
            correct_recalls=0,
            user_sentences=[],
            exposure_count=1,
            mastery_state="new",
            next_review_at=now + timedelta(days=1),
            source=SOURCE,
            source_version=SOURCE_VERSION,
            policy_version=POLICY_VERSION,
            created_at=now,
            updated_at=now,
        )
        try:
            with db.begin_nested():
                db.add(item)
                db.flush()
        except IntegrityError:
            db.expire_all()
            concurrent_item = db.scalar(
                select(VocabularyItem).where(
                    VocabularyItem.user_id == user_id,
                    VocabularyItem.normalized_word == word,
                )
            )
            if concurrent_item is None:
                raise
            item = concurrent_item
        db.add(
            ProvenanceLink(
                user_id=user_id,
                target_id=item.id,
                source_type="curated_lexicon",
                source_id=None,
                confidence=1.0,
                derivation=POLICY_VERSION,
                metadata_only=True,
                created_at=now,
            )
        )
        return self._section(item, "new", "no weak word is due")

    @staticmethod
    def _section(item: VocabularyItem | None, mode: str, reason: str) -> dict[str, Any]:
        if item is None:
            return {}
        return {
            "item_id": item.id,
            "mode": mode,
            "word": item.display_word,
            "definition": item.definition,
            "example": item.natural_example,
            "mastery_state": item.mastery_state,
            "next_review_at": item.next_review_at.isoformat() if item.next_review_at else None,
            "reason": reason,
            "policy_version": item.policy_version,
            "source": item.source,
        }

    def prompt(
        self,
        db: Session,
        user_id: str,
        event_id: str,
        now: datetime,
        *,
        append: bool = True,
        target_word: str | None = None,
    ) -> str | None:
        user = db.get(User, user_id)
        if user is None:
            raise ValueError("Unknown user")
        local_date = local_date_for(now, user.timezone)
        conditions = [
            VocabularyItem.user_id == user_id,
            VocabularyItem.deleted.is_(False),
            VocabularyItem.introduced_local_date <= local_date,
        ]
        if target_word:
            conditions.append(VocabularyItem.normalized_word == normalize(target_word))
        else:
            conditions.extend(
                [
                    VocabularyItem.mastery_state != "mastered",
                    VocabularyItem.next_review_at.is_not(None),
                    VocabularyItem.next_review_at <= now,
                ]
            )
        item = db.scalar(
            select(VocabularyItem)
            .where(*conditions)
            .order_by(
                VocabularyItem.next_review_at.asc().nullsfirst(),
                VocabularyItem.correct_recalls,
                VocabularyItem.id,
            )
            .with_for_update()
        )
        if item is None:
            return None
        window = local_date.isoformat() + (":revisit" if target_word else "")
        existing = db.scalar(
            select(VocabularyAttempt).where(
                VocabularyAttempt.user_id == user_id,
                VocabularyAttempt.vocabulary_item_id == item.id,
                VocabularyAttempt.review_window == window,
                VocabularyAttempt.policy_version == POLICY_VERSION,
            )
        )
        if existing is not None:
            return None
        text = f"Quick word check: what does “{item.display_word}” mean?"
        db.add(
            VocabularyAttempt(
                user_id=user_id,
                vocabulary_item_id=item.id,
                prompt=text,
                review_window=window,
                source_event_id=event_id,
                policy_version=POLICY_VERSION,
                created_at=now,
            )
        )
        db.flush()
        if append:
            conversation = get_or_create_conversation(db, user_id, created_at=now)
            append_message(
                db,
                user_id=user_id,
                conversation_id=conversation.id,
                role="assistant",
                actor="assistant",
                content=text,
                created_at=now,
            )
        return text

    def capture_if_pending(
        self, db: Session, user_id: str, event_id: str, content: str, now: datetime
    ) -> tuple[bool, str | None]:
        explicit_correction = re.match(r"^\s*self[- ]correction\s*:\s*", content, re.I)
        if explicit_correction:
            attempt = db.scalar(
                select(VocabularyAttempt)
                .where(
                    VocabularyAttempt.user_id == user_id,
                    VocabularyAttempt.user_answer.is_not(None),
                    VocabularyAttempt.reviewed_at >= now - CORRECTION_WINDOW,
                )
                .order_by(VocabularyAttempt.reviewed_at.desc(), VocabularyAttempt.id.desc())
                .with_for_update()
            )
        else:
            attempt = db.scalar(
                select(VocabularyAttempt)
                .where(
                    VocabularyAttempt.user_id == user_id,
                    VocabularyAttempt.user_answer.is_(None),
                )
                .order_by(VocabularyAttempt.created_at.desc(), VocabularyAttempt.id.desc())
                .with_for_update()
            )
        if attempt is None:
            if explicit_correction:
                return True, "I couldn’t find a recent vocabulary attempt to correct."
            return False, None
        item = db.scalar(
            select(VocabularyItem).where(
                VocabularyItem.user_id == user_id,
                VocabularyItem.id == attempt.vocabulary_item_id,
                VocabularyItem.deleted.is_(False),
            ).with_for_update()
        )
        if item is None:
            return False, None
        if explicit_correction and attempt.correct is True:
            return True, "That vocabulary attempt was already corrected."
        bounded = content.strip()[:MAX_ANSWER]
        sentence_match = re.search(r"(?:^|[.;]\s*)sentence:\s*(.+)$", bounded, re.I)
        sentence = sentence_match.group(1).strip()[:500] if sentence_match else None
        answer = bounded[: sentence_match.start()].strip(" .;") if sentence_match else bounded
        normalized = normalize(answer)
        expected = normalize(item.definition)
        if explicit_correction:
            normalized = normalize(answer[explicit_correction.end() :])
        correct: bool | None
        if normalized == expected:
            correct = True
        elif normalized in {"i don't know", "i do not know", "incorrect", "not sure"}:
            correct = False
        else:
            correct = None
        previous_grade = attempt.correct
        if not explicit_correction:
            attempt.user_answer = bounded
            attempt.user_sentence = sentence
            attempt.reviewed_at = now
        if explicit_correction and correct is not True:
            return True, (
                "I’m not certain enough to apply that correction. "
                f"Use “self-correction: {item.definition}”."
            )
        attempt.correct = correct
        if correct is not None:
            if not explicit_correction or previous_grade is None:
                item.recall_attempts += 1
            if correct:
                item.correct_recalls += 1
                if item.correct_recalls >= 4:
                    item.mastery_state = "mastered"
                    item.next_review_at = None
                elif item.correct_recalls >= 2:
                    item.mastery_state = "familiar"
                    item.next_review_at = now + timedelta(days=7)
                else:
                    item.mastery_state = "developing"
                    item.next_review_at = now + timedelta(days=2)
            else:
                item.mastery_state = "developing"
                item.next_review_at = now + timedelta(days=1)
            item.last_reviewed_at = now
            item.updated_at = now
            item.version += 1
        if explicit_correction:
            db.add(
                ProvenanceLink(
                    user_id=user_id,
                    target_id=attempt.id,
                    source_type="agent_event",
                    source_id=event_id,
                    confidence=1.0,
                    derivation="explicit-vocabulary-correction-v1",
                    metadata_only=True,
                    created_at=now,
                )
            )
        if sentence:
            item.user_sentences = [*item.user_sentences, sentence][-20:]
        if correct is True:
            return True, "That’s right. I saved your sentence." if sentence else (
                "That’s right. Could you use it in a sentence?"
            )
        if correct is False:
            return True, f"Not quite. “{item.display_word}” means {item.definition}."
        return True, (
            "I’m not certain enough to grade that. Could you restate it, or say "
            f"“self-correction: {item.definition}”?"
        )

    @staticmethod
    def contextual_reuse(text: str, item: VocabularyItem) -> str | None:
        if item.normalized_word != "inchoate":
            return None
        clauses = re.split(r"[,.;!?]|\b(?:but|however|although|whereas)\b", text, flags=re.I)
        for raw_clause in clauses:
            clause = normalize(raw_clause)
            if not clause:
                continue
            negative_or_contrast = (
                clause.startswith("unlike ")
                or re.search(r"\bnot (?:a |an )?(?:rough idea|early draft)\b", clause)
                or re.search(r"\b(?:rough idea|early draft)\b.*\b(?:anymore|no longer)\b", clause)
                or re.search(r"\bnot fully formed\b.*\banymore\b", clause)
                or re.search(r"\b(?:now|already) fully formed\b", clause)
            )
            if negative_or_contrast:
                continue
            if re.search(
                r"\bstill (?:a |an )?(?:early draft|rough idea)\b|\bnot fully formed\b",
                clause,
            ):
                return "That sounds inchoate — still only partly formed."
        return None
