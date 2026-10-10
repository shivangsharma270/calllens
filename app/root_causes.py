"""Root causes: group failed calls, then rank the groups by frequency and severity.

Priority score (shown on the page, with its inputs):
    frequency = calls in the root cause / all analysed calls
    severity  = average call weight / 3, where a call weighs
                3 if it is Fatal,
                2 if it is Non-Fatal but the buyer was frustrated or the call did not reach its goal,
                1 otherwise
    priority  = frequency x severity x 100
So a cause scores 100 only if every analysed call has it and every one of those is Fatal.
"""

import logging
from collections import Counter, defaultdict
from typing import Any

from pydantic import BaseModel

from app import config, db, sarvam

logger = logging.getLogger(__name__)

MAX_CALLS = 250
GOAL_REACHED = {"requirement_confirmed", "requirement_updated"}
CATEGORIES = {"prompt", "asr", "knowledge", "flow", "technical"}


def call_weight(grade: str, risk_flags: list[str], outcome: str | None) -> int:
    if grade == "FATAL":
        return 3
    if "frustration" in risk_flags or (outcome or "") not in GOAL_REACHED:
        return 2
    return 1


def score(weights: list[int], audited: int) -> dict[str, float]:
    """Frequency, severity and priority for one root cause."""
    if not weights or not audited:
        return {"frequency": 0.0, "severity": 0.0, "priority": 0.0}
    frequency = len(weights) / audited
    severity = sum(weights) / len(weights) / 3
    return {"frequency": frequency, "severity": severity, "priority": frequency * severity * 100}


class _Cause(BaseModel):
    name: str
    description: str
    category: str
    call_ids: list[str]


class _Causes(BaseModel):
    causes: list[_Cause]


_SCHEMA = {
    "name": "root_causes",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["causes"],
        "properties": {
            "causes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["name", "description", "category", "call_ids"],
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "category": {"type": "string", "enum": sorted(CATEGORIES)},
                        "call_ids": {"type": "array", "items": {"type": "string"}},
                    },
                },
            }
        },
    },
}


def failed_calls() -> list[Any]:
    """Fatal calls, plus Non-Fatal calls that still had issues."""
    return db.q(
        """SELECT call_id, grade, fatal_code, failure_category, root_cause_hint, risk_flags, outcome
           FROM grade WHERE grade = 'FATAL' OR non_fatal_issues NOT IN ('[]', '')
           ORDER BY (grade = 'FATAL') DESC, confidence DESC LIMIT ?""",
        MAX_CALLS,
    )


def assign(proposed: list[_Cause], known: list[str]) -> list[dict[str, Any]]:
    """Make the model's grouping safe to use: each known call in exactly one cause, the first
    that claims it; unknown ids dropped; unassigned calls collected under "Other"."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    valid = set(known)
    for cause in proposed:
        ids = [c for c in dict.fromkeys(cause.call_ids) if c in valid and c not in seen]
        seen.update(ids)
        if ids:
            category = cause.category if cause.category in CATEGORIES else "flow"
            out.append({"name": cause.name.strip(), "description": cause.description.strip(), "category": category, "call_ids": ids})
    left = [c for c in known if c not in seen]
    if left:
        other = next((c for c in out if c["name"].lower() == "other"), None)
        if other:
            other["call_ids"] += left
        else:
            out.append({"name": "Other", "description": "Calls that did not fit another root cause.", "category": "flow", "call_ids": left})
    return out


def fallback_groups(rows: list[Any]) -> list[_Cause]:
    """If the model cannot group, group by cause type and fatal code so there is still a ranking."""
    labels = {"prompt": "Bot instructions", "asr": "Bot misheard the caller", "knowledge": "Bot lacked a fact",
              "flow": "Conversation flow broke", "technical": "Telephony or system fault", "none": "No cause given"}  # fmt: skip
    groups: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for r in rows:
        groups[(r["failure_category"] or "none", r["fatal_code"] or "no fatal code")].append(r)
    out = []
    for (category, code), members in groups.items():
        hint = Counter(m["root_cause_hint"] for m in members).most_common(1)[0][0]
        out.append(_Cause(
            name=f"{labels.get(category, category)} ({code})", description=hint or "",
            category=category if category in CATEGORIES else "flow", call_ids=[m["call_id"] for m in members],
        ))  # fmt: skip
    return out


async def _ask_groups(rows: list[Any], key: str, extra: str = "") -> list[_Cause] | None:
    lines = "\n".join(
        f"{r['call_id']} | {r['grade']} {r['fatal_code'] or '-'} | {r['failure_category']} | {r['root_cause_hint']}"
        for r in rows
    )
    for temperature in (0.0, 0.2, 0.4):
        try:
            result = await sarvam.chat(
                "root_causes",
                key,
                [
                    {"role": "system", "content": (config.PROMPT_DIR / "cluster_grouper.md").read_text(encoding="utf-8")},
                    {"role": "user", "content": f"<<<CALLS START>>>\n{lines}\n<<<CALLS END>>>{extra}"},
                ],
                json_schema=_SCHEMA,
                reasoning_effort=None,
                temperature=temperature,
                max_tokens=4000,
            )
            return _Causes.model_validate_json(result.content).causes
        except sarvam.ChatTruncated:
            logger.warning("root-cause grouping answer was cut off at temperature %s", temperature)
        except Exception:
            logger.exception("root-cause grouping failed")
            return None
    return None


def merge_causes(causes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Combine causes that were given the same name in different passes."""
    merged: dict[str, dict[str, Any]] = {}
    for c in causes:
        key = c["name"].strip().lower()
        if key in merged:
            merged[key]["call_ids"] += [i for i in c["call_ids"] if i not in merged[key]["call_ids"]]
        else:
            merged[key] = c
    return list(merged.values())


async def build() -> dict[str, Any]:
    rows = failed_calls()
    if not rows:
        raise ValueError("No Fatal calls or calls with issues have been analysed yet")
    method, proposed = "ai", await _ask_groups(rows, "grouping")
    if not proposed:
        method, proposed = "rules", fallback_groups(rows)

    causes = assign(proposed, [r["call_id"] for r in rows])
    by_id = {r["call_id"]: r for r in rows}

    # Second pass: when many calls were left in "Other", ask again about just those, with the
    # names already in use so the model can reuse them instead of inventing near-duplicates.
    other = next((c for c in causes if c["name"].lower() == "other"), None)
    if method == "ai" and other and len(other["call_ids"]) > max(3, len(rows) // 8):
        names = "; ".join(c["name"] for c in causes if c is not other)
        extra = (
            f"\n\nRoot causes already found in other calls: {names}. Reuse one of these names, exactly, when a "
            'call fits it; otherwise create a new specific root cause. Use "Other" only as a last resort.'
        )
        again = await _ask_groups([by_id[i] for i in other["call_ids"]], "grouping-leftovers", extra)
        if again:
            regrouped = assign(again, other["call_ids"])
            causes = merge_causes([c for c in causes if c is not other] + regrouped)
    audited = db.val("SELECT COUNT(*) FROM grade") or 0
    with db.tx():
        db.ex("DELETE FROM root_cause_call")
        db.ex("DELETE FROM root_cause")
        for cause in causes:
            weights = [
                call_weight(by_id[c]["grade"], db.loads(by_id[c]["risk_flags"], []), by_id[c]["outcome"])
                for c in cause["call_ids"]
            ]
            s = score(weights, audited)
            cause_id = db.ex(
                "INSERT INTO root_cause (name, description, category, call_count, frequency, severity, priority)"
                " VALUES (?,?,?,?,?,?,?)",
                cause["name"], cause["description"], cause["category"], len(weights),
                s["frequency"], s["severity"], s["priority"],
            ).lastrowid  # fmt: skip
            db.many("INSERT INTO root_cause_call (root_cause_id, call_id) VALUES (?,?)", [(cause_id, c) for c in cause["call_ids"]])
        db.ex(
            "INSERT INTO kv (key, value, updated_at) VALUES ('root_causes_meta', ?, CURRENT_TIMESTAMP)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
            f'{{"method": "{method}", "calls": {len(rows)}, "audited": {audited}}}',
        )
    return {"causes": len(causes), "method": method}


def ranked() -> list[dict[str, Any]]:
    """Root causes, highest priority first, with the numbers behind each score."""
    out = []
    # "Other" is the ungrouped remainder, not a cause anyone can fix, so it is always listed last.
    for rc in db.q("SELECT * FROM root_cause ORDER BY (lower(name) = 'other'), priority DESC, call_count DESC"):
        members = db.q(
            """SELECT g.call_id, g.grade, g.fatal_code, g.reason, g.risk_flags, g.outcome
               FROM root_cause_call r JOIN grade g USING (call_id) WHERE r.root_cause_id = ?
               ORDER BY (g.grade = 'FATAL') DESC, g.confidence DESC""",
            rc["id"],
        )
        weights = Counter(call_weight(m["grade"], db.loads(m["risk_flags"], []), m["outcome"]) for m in members)
        out.append({**dict(rc), "members": members, "fatal": weights[3], "serious": weights[2], "minor": weights[1]})
    return out


def meta() -> dict[str, Any] | None:
    row = db.one("SELECT value, updated_at FROM kv WHERE key='root_causes_meta'")
    return {**db.loads(row["value"], {}), "updated_at": row["updated_at"]} if row else None


# ---- review queue --------------------------------------------------------------------------

FLAG_INFO = [
    ("wrong_requirement", "Wrong requirement", "The bot captured something different from what the buyer wanted."),
    ("frustration", "Frustration", "The buyer sounds annoyed, repeats themselves or says they are unhappy."),
    ("loop", "Loops", "The bot repeats itself or the conversation goes in circles."),
    ("low_confidence", "Low confidence", "Speech recognition or the grader was unsure at a key moment."),
    ("human_request", "Human request", "The buyer asks to speak to a person."),
]
OTHER_REASONS = {
    "disposition_not_supported": ("Logged outcome not supported", "The transcript does not back up the outcome the system logged."),
    "low_confidence_grade": ("Low confidence", "The grader's confidence in its own grade was below 0.6."),
    "non_fatal_but_fatal_code_given": ("Grade contradicts itself", "Graded Non-Fatal but a fatal code was given."),
    "f2_but_requirement_confirmed": ("Grade contradicts itself", "Graded wrong-requirement although the buyer confirmed."),
    "success_logged_but_caller_rejected": ("Logged outcome not supported", "Logged as success but the buyer rejected the requirement."),
    "validation_failed": ("Grade needs checking", "The grader's answer failed a check and was kept for review."),
    "no_speech": ("No speech", "Nothing was recognised in the recording."),
}


def review_queue(status: str = "open") -> list[dict[str, Any]]:
    """Review items grouped by flag, the five required risk flags first."""
    rows = db.q(
        """SELECT r.id, r.call_id, r.reason, r.status, g.grade, g.fatal_code, g.reason AS grade_reason,
                  g.confidence, c.disposition, c.duration_s
           FROM review_item r JOIN call c USING (call_id) LEFT JOIN grade g USING (call_id)
           WHERE r.status = ? ORDER BY (g.grade = 'FATAL') DESC, g.confidence""",
        status,
    )
    groups: dict[str, dict[str, Any]] = {}
    for key, title, text in FLAG_INFO:
        groups[title] = {"title": title, "text": text, "flag": key, "items": [], "required": True}
    for r in rows:
        if r["reason"] in dict((k, t) for k, t, _ in FLAG_INFO):
            title = next(t for k, t, _ in FLAG_INFO if k == r["reason"])
        else:
            title, text = OTHER_REASONS.get(r["reason"], (r["reason"].replace("_", " ").capitalize(), ""))
            groups.setdefault(title, {"title": title, "text": text, "flag": None, "items": [], "required": False})
        quote = db.one(
            """SELECT e.quote, e.utterance_idx, u.speaker FROM evidence e
               LEFT JOIN utterance u ON u.call_id = e.call_id AND u.idx = e.utterance_idx
               WHERE e.call_id = ? ORDER BY (e.supports = ?) DESC, (e.supports = 'grade') DESC LIMIT 1""",
            r["call_id"], r["reason"],
        )  # fmt: skip
        groups[title]["items"].append({**dict(r), "quote": quote})
    return list(groups.values())
