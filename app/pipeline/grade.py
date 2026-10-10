"""Grade one call with app/prompts/grader.md (used as written: the system prompt, user template
and JSON schema are all read out of that file at runtime)."""

import hashlib
import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from app import config, db, sarvam
from app.pipeline.signals import Signal, Turn, load_turns

logger = logging.getLogger(__name__)

FatalCode = Literal["F1", "F2", "F3", "F4", "F5", "F6", "F7"]
RiskFlag = Literal["wrong_requirement", "frustration", "loop", "low_confidence", "human_request"]
REVIEW_CONFIDENCE = 0.6


class Requirement(BaseModel):
    read_back_segment: str | None
    read_back_by_bot: str | None
    caller_response: Literal["confirmed", "corrected", "rejected", "unclear", "no_read_back"]
    caller_stated: str | None
    final_captured_by_bot: str | None
    match: Literal["match", "mismatch", "partial", "not_captured", "not_applicable"]


class DispositionCheck(BaseModel):
    logged: str
    supported: Literal["yes", "no", "unclear"]
    transcript_shows: str


class EvidenceItem(BaseModel):
    segment_id: str
    quote: str
    supports: str


class SignalReview(BaseModel):
    signal: str
    verdict: Literal["confirmed", "dismissed"]


class GraderOutput(BaseModel):
    requirement: Requirement
    who_ended_call: Literal["bot_proper_close", "caller_hung_up", "bot_cut_off", "technical_drop", "unclear"]
    disposition_check: DispositionCheck
    outcome: str
    grade: Literal["FATAL", "NON_FATAL"]
    fatal_code: FatalCode | None
    reason: str
    failure_category: Literal["prompt", "asr", "knowledge", "flow", "technical", "none"]
    root_cause_hint: str
    risk_flags: list[RiskFlag]
    evidence: list[EvidenceItem]
    non_fatal_issues: list[str]
    signals_review: list[SignalReview]
    confidence: float


@dataclass(frozen=True)
class GraderPrompt:
    system: str
    user_template: str
    schema: dict[str, Any]
    sha256: str


def _section(text: str, heading: str) -> str:
    return text.split(heading, 1)[1]


@lru_cache
def load_grader() -> GraderPrompt:
    text = (config.PROMPT_DIR / "grader.md").read_text(encoding="utf-8")
    bot_goal = (config.RUBRIC_DIR / "bot_goal.md").read_text(encoding="utf-8").strip()
    rubric = (config.RUBRIC_DIR / "fatal_rubric.md").read_text(encoding="utf-8").strip()
    system = _section(text, "## SYSTEM PROMPT").split("\n---\n", 1)[0].strip()
    system = system.replace("{{BOT_GOAL}}", bot_goal).replace("{{FATAL_RUBRIC}}", rubric)
    template = re.search(r"```\n(.*?)```", _section(text, "## USER MESSAGE TEMPLATE"), re.S)
    schema = re.search(r"```json\n(.*?)```", _section(text, "## JSON SCHEMA"), re.S)
    if not template or not schema:
        raise ValueError("grader.md is missing its user template or JSON schema block")
    digest = hashlib.sha256("\x00".join((text, bot_goal, rubric)).encode("utf-8")).hexdigest()
    return GraderPrompt(system, template.group(1).strip(), json.loads(schema.group(1)), digest)


def normalise(text: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace. Letters and vowel signs are kept, so a
    quote must still match word for word; only "।" vs "." or stray quote marks are forgiven."""
    kept = "".join(" " if unicodedata.category(ch)[0] in "PS" else ch for ch in text.lower())
    return re.sub(r"\s+", " ", kept).strip()


def build_user_message(call: Any, turns: list[Turn], signals: list[Signal]) -> str:
    probability = call["language_probability"]
    values = {
        "CALL_ID": call["call_id"],
        "DURATION_S": f"{call['duration_s'] or 0:.0f}",
        "DISPOSITION": call["disposition"],
        "DISPOSITION_GROUP": call["disposition_group"],
        "IS_CALL_ANSWERED": call["is_call_answered"] or "unknown",
        "LANGUAGE_CODE": call["language_code"] or "unknown",
        "LANGUAGE_PROBABILITY": "unknown" if probability is None else f"{probability:.2f}",
        "SPEAKER_METHOD": call["speaker_method"] or "unknown",
        "TRANSCRIPT_LINES": "\n".join(f"[S{t.idx}] {t.speaker} ({t.start_s:.1f}s): {t.text}" for t in turns),
        "SIGNALS_LINES": "\n".join(
            f"- {s.type} at {'call' if s.utterance_idx is None else f'S{s.utterance_idx}'}: {s.detail}"
            for s in signals
        )
        or "- none",
    }
    message = load_grader().user_template
    for key, value in values.items():
        message = message.replace("{{" + key + "}}", str(value))
    return message


def segment_index(segment_id: str) -> int | None:
    """Accept "S12", "[S12]", "12" or a whole copied line like "[S12] BOT (34.2s): …"."""
    match = re.match(r"\s*\[?\s*S?\s*(\d+)", segment_id, re.I)
    return int(match.group(1)) if match else None


def bad_quotes(out: GraderOutput, turns: list[Turn]) -> list[EvidenceItem]:
    """Evidence whose quote is not word-for-word inside the segment it cites."""
    by_idx = {t.idx: normalise(t.text) for t in turns}
    return [
        e
        for e in out.evidence
        if not normalise(e.quote)
        or (idx := segment_index(e.segment_id)) not in by_idx
        or normalise(e.quote) not in by_idx[idx]
    ]


def repoint_evidence(out: GraderOutput, turns: list[Turn]) -> None:
    """A real quote cited under the wrong segment id is moved to the segment it appears in
    (the nearest one, if several). Quotes found nowhere are left alone and fail validation."""
    texts = {t.idx: normalise(t.text) for t in turns}
    for e in bad_quotes(out, turns):
        quote = normalise(e.quote)
        homes = [idx for idx, text in texts.items() if quote and quote in text]
        if homes:
            cited = segment_index(e.segment_id)
            e.segment_id = f"S{min(homes, key=lambda i: abs(i - (cited if cited is not None else i)))}"


def validate(out: GraderOutput, turns: list[Turn], disposition: str) -> list[str]:
    """The server-side checks from grader.md that are worth a retry. Empty means accepted.

    Two of grader.md's checks are handled without a retry, because on real calls the model repeated
    the same answer and each retry cost a request (see `conflicts`):
    - NON_FATAL with a fatal_code is a contradiction → straight to human review.
    - NON_FATAL with minor issues but failure_category "none" → accepted; it does not put the
      grade in doubt, and flagging it would fill the review queue (about 30% of calls).
    """
    errors = []
    if out.grade == "FATAL":
        if out.fatal_code is None:
            errors.append("grade is FATAL but fatal_code is null")
        if not any(e.supports == "grade" for e in out.evidence):
            errors.append('grade is FATAL but no evidence item has supports == "grade"')
        if out.failure_category == "none":
            errors.append('failure_category cannot be "none" for a FATAL grade')
    for e in bad_quotes(out, turns):
        errors.append(f'quote "{e.quote}" is not exact text from segment {e.segment_id}; copy it exactly')
    if normalise(out.disposition_check.logged) != normalise(disposition):
        errors.append(f'disposition_check.logged must be exactly "{disposition}"')
    if not 0 <= out.confidence <= 1:
        errors.append("confidence must be between 0 and 1")
    return errors


def conflicts(out: GraderOutput) -> set[str]:
    """Contradictions inside an accepted answer that a human should look at."""
    found = set()
    if out.grade == "NON_FATAL" and out.fatal_code is not None:
        found.add("non_fatal_but_fatal_code_given")
    # F2 means a wrong requirement was captured or logged. If the same answer says the buyer
    # confirmed, nothing mismatched and the disposition holds, the F2 contradicts its own findings.
    if (
        out.fatal_code == "F2"
        and out.grade == "FATAL"
        and out.requirement.caller_response == "confirmed"
        and out.requirement.match in ("match", "not_applicable")
        and out.disposition_check.supported == "yes"
    ):
        found.add("f2_but_requirement_confirmed")
    return found


async def _ask(call_id: str, messages: list[dict[str, str]]) -> str:
    # grader.md's header asks for reasoning_effort="low" with max_tokens=4000. Measured on 20 real
    # calls, that ran out of tokens while thinking on all 20 and returned nothing usable. So thinking
    # is off; the schema's field order (requirement → who ended → disposition check → grade) still
    # walks the model through the procedure before it commits to a grade.
    #
    # In strict-JSON mode the model now and then gets stuck emitting whitespace until max_tokens.
    # It is random (the same request succeeds when repeated), so a cut-off answer is asked for
    # again, with a little temperature on the repeats to break the loop.
    for temperature in (0.0, 0.2, 0.4):
        try:
            result = await sarvam.chat(
                "grade",
                call_id,
                messages,
                json_schema=load_grader().schema,
                reasoning_effort=None,
                temperature=temperature,
                max_tokens=4000,
            )
            return result.content
        except sarvam.ChatTruncated:
            logger.warning("grader answer for %s was cut off at temperature %s", call_id, temperature)
    raise sarvam.ChatTruncated(f"grader answer for {call_id} was cut off three times")


def _parse(content: str) -> tuple[GraderOutput | None, list[str]]:
    try:
        return GraderOutput.model_validate_json(content), []
    except ValidationError as exc:
        problems = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:6]]
        return None, [f"JSON does not match the schema ({'; '.join(problems)})"]


def save_grade(call: Any, out: GraderOutput, turns: list[Turn], needs_review: bool, review_reasons: set[str]) -> None:
    call_id = call["call_id"]
    by_idx = {t.idx: t for t in turns}
    if out.disposition_check.supported == "no":
        review_reasons.add("disposition_not_supported")
    if out.confidence < REVIEW_CONFIDENCE:
        review_reasons.add("low_confidence_grade")
    review_reasons.update(out.risk_flags)
    if found := conflicts(out):
        needs_review = True
        review_reasons.update(found)
    fatal_code = out.fatal_code if out.grade == "FATAL" else None  # the raw answer keeps what it said
    # Consistency nudge from grader.md: flag, never override.
    if (
        call["disposition_group"] == "success"
        and out.requirement.caller_response == "rejected"
        and out.grade == "NON_FATAL"
    ):
        needs_review = True
        review_reasons.add("success_logged_but_caller_rejected")
    with db.tx():
        db.ex(
            """INSERT OR REPLACE INTO grade (call_id, grade, fatal_code, reason, failure_category,
                 root_cause_hint, outcome, who_ended_call, risk_flags, requirement, disposition_check,
                 non_fatal_issues, confidence, needs_review, prompt_hash, raw_json, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
            call_id, out.grade, fatal_code, out.reason, out.failure_category, out.root_cause_hint,
            out.outcome, out.who_ended_call, json.dumps(out.risk_flags),
            out.requirement.model_dump_json(), out.disposition_check.model_dump_json(),
            json.dumps(out.non_fatal_issues, ensure_ascii=False), out.confidence, int(needs_review),
            load_grader().sha256, out.model_dump_json(),
        )  # fmt: skip
        db.ex("DELETE FROM evidence WHERE call_id=?", call_id)
        for e in out.evidence:
            idx = segment_index(e.segment_id)
            turn = by_idx.get(idx) if idx is not None else None
            db.ex(
                "INSERT INTO evidence (call_id, utterance_idx, quote, supports, start_s) VALUES (?,?,?,?,?)",
                call_id, idx, e.quote, e.supports, turn.start_s if turn else None,
            )  # fmt: skip
        db.ex("DELETE FROM review_item WHERE call_id=? AND status='open'", call_id)
        for reason in sorted(review_reasons):
            db.ex("INSERT OR IGNORE INTO review_item (call_id, reason) VALUES (?,?)", call_id, reason)
        db.ex("UPDATE call SET status='graded', error=NULL WHERE call_id=?", call_id)


def no_speech_grade(disposition: str) -> GraderOutput:
    """grader.md: a silent recording is stored without calling the LLM."""
    return GraderOutput(
        requirement=Requirement(
            read_back_segment=None, read_back_by_bot=None, caller_response="no_read_back",
            caller_stated=None, final_captured_by_bot=None, match="not_applicable",
        ),  # fmt: skip
        who_ended_call="unclear",
        disposition_check=DispositionCheck(
            logged=disposition, supported="unclear", transcript_shows="No speech in recording"
        ),
        outcome="call_dropped",
        grade="NON_FATAL",
        fatal_code=None,
        reason="No speech in recording",
        failure_category="technical",
        root_cause_hint="recording contains no speech",
        risk_flags=[],
        evidence=[],
        non_fatal_issues=["No speech in recording"],
        signals_review=[],
        confidence=0.5,
    )


async def grade_call(call_id: str, signals: list[Signal]) -> None:
    call = db.one("SELECT * FROM call WHERE call_id=?", call_id)
    turns = load_turns(call_id)
    if not turns:
        save_grade(call, no_speech_grade(call["disposition"]), turns, True, {"no_speech"})
        return

    prompt = load_grader()
    messages = [
        {"role": "system", "content": prompt.system},
        {"role": "user", "content": build_user_message(call, turns, signals)},
    ]
    def check(content: str) -> tuple[GraderOutput | None, list[str]]:
        parsed, problems = _parse(content)
        if parsed is not None:
            if normalise(parsed.disposition_check.logged) == normalise(call["disposition"]):
                parsed.disposition_check.logged = call["disposition"]  # an echo field: keep our spelling
            repoint_evidence(parsed, turns)
            problems = validate(parsed, turns, call["disposition"])
        return parsed, problems

    out, errors = check(await _ask(call_id, messages))
    if errors:
        # One retry with the validation error appended, as grader.md specifies. The error goes in
        # the same user message: an assistant turn made Sarvam reply with nothing, and pasting the
        # previous answer back in made it run on to the token limit.
        retry = [
            messages[0],
            {
                "role": "user",
                "content": f"{messages[1]['content']}\n\nYour previous answer failed validation: "
                f"{'; '.join(errors)}. Return corrected JSON only.",
            },
        ]
        try:
            second, second_errors = check(await _ask(call_id, retry))
        except sarvam.ChatTruncated:
            logger.warning("grader retry for %s was cut off; keeping the first answer for review", call_id)
        else:
            if second is not None:
                out, errors = second, second_errors
    if out is None:
        raise ValueError(f"grader output unusable after retry: {errors[0]}")

    reasons: set[str] = set()
    if errors:
        # Still invalid: keep the grade for a human to check, but never show an unverified quote.
        reasons.add("validation_failed")
        dropped = {id(e) for e in bad_quotes(out, turns)}
        out.evidence = [e for e in out.evidence if id(e) not in dropped]
        out.disposition_check.logged = call["disposition"]
        logger.warning("grade for %s kept with validation errors: %s", call_id, errors)
    save_grade(call, out, turns, bool(errors), reasons)
