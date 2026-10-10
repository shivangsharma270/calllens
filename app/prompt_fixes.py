"""Prompt fixes: compare the bot's real prompt with what the audit found, and propose changes."""

import hashlib
import json
import logging
import re
from typing import Any

from pydantic import BaseModel

from app import config, db, sarvam

logger = logging.getLogger(__name__)

BOT_PROMPT_PATH = config.DATA_DIR / "bot_prompt.txt"
MAX_FINDINGS = 60
KV_KEY = "prompt_fixes"


class Fix(BaseModel):
    title: str
    what_goes_wrong: str
    calls_affected: int
    fatal_codes: list[str]
    prompt_section: str
    current_instruction: str
    suggested_text: str
    why_it_helps: str
    example_call_ids: list[str]


class FixReport(BaseModel):
    summary: str
    fixes: list[Fix]
    not_prompt_issues: list[str]


def _strings(*names: str) -> dict[str, Any]:
    return {n: {"type": "string"} for n in names}


_SCHEMA = {
    "name": "prompt_fixes",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["summary", "fixes", "not_prompt_issues"],
        "properties": {
            "summary": {"type": "string"},
            "fixes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": list(Fix.model_fields),
                    "properties": {
                        **_strings(
                            "title", "what_goes_wrong", "prompt_section", "current_instruction",
                            "suggested_text", "why_it_helps",
                        ),  # fmt: skip
                        "calls_affected": {"type": "integer"},
                        "fatal_codes": {"type": "array", "items": {"type": "string"}},
                        "example_call_ids": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
            "not_prompt_issues": {"type": "array", "items": {"type": "string"}},
        },
    },
}


def bot_prompt() -> str | None:
    return BOT_PROMPT_PATH.read_text(encoding="utf-8") if BOT_PROMPT_PATH.is_file() else None


def _squash(text: str) -> str:
    return re.sub(r"[^\w]+", " ", text.lower()).strip()


def findings() -> list[dict[str, Any]]:
    """Fatal calls first, then Non-Fatal calls that still had issues, with their quoted evidence."""
    rows = db.q(
        """SELECT g.call_id, g.grade, g.fatal_code, g.failure_category, g.root_cause_hint, g.reason,
                  g.non_fatal_issues, g.who_ended_call, c.disposition
           FROM grade g JOIN call c USING (call_id)
           WHERE g.grade = 'FATAL' OR g.non_fatal_issues NOT IN ('[]', '')
           ORDER BY (g.grade = 'FATAL') DESC, g.confidence DESC LIMIT ?""",
        MAX_FINDINGS,
    )
    out = []
    for r in rows:
        quotes = db.q(
            """SELECT e.quote, u.speaker FROM evidence e
               LEFT JOIN utterance u ON u.call_id = e.call_id AND u.idx = e.utterance_idx
               WHERE e.call_id = ? LIMIT 3""",
            r["call_id"],
        )
        out.append({**dict(r), "quotes": [f"{q['speaker'] or '?'}: {q['quote']}" for q in quotes]})
    return out


def _findings_text(items: list[dict[str, Any]]) -> str:
    lines = []
    for f in items:
        lines.append(
            f"CALL {f['call_id']} | {f['grade']} {f['fatal_code'] or '-'} | logged: {f['disposition']}"
            f" | cause: {f['failure_category']} | ended: {f['who_ended_call']}\n"
            f"  hint: {f['root_cause_hint']}\n  reason: {f['reason']}"
        )
        issues = db.loads(f["non_fatal_issues"], [])
        if issues:
            lines.append("  issues: " + "; ".join(issues[:3]))
        lines += [f"  quote — {q}" for q in f["quotes"]]
    return "\n".join(lines)


async def generate() -> dict[str, Any]:
    prompt = bot_prompt()
    if prompt is None:
        raise ValueError(f"Bot prompt not found at {BOT_PROMPT_PATH}")
    items = findings()
    if not items:
        raise ValueError("No Fatal calls or calls with issues have been analysed yet")
    system = (config.PROMPT_DIR / "fix_writer.md").read_text(encoding="utf-8")
    user = (
        f"<<<BOT PROMPT START>>>\n{prompt}\n<<<BOT PROMPT END>>>\n\n"
        f"<<<AUDIT FINDINGS START>>>\n{_findings_text(items)}\n<<<AUDIT FINDINGS END>>>"
    )
    content = None
    # Strict-JSON answers are sometimes cut off by a run of whitespace; ask again if so.
    for temperature in (0.0, 0.2, 0.4):
        try:
            result = await sarvam.chat(
                "prompt_fixes",
                "report",
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                json_schema=_SCHEMA,
                reasoning_effort=None,
                temperature=temperature,
                max_tokens=4000,
            )
            content = result.content
            break
        except sarvam.ChatTruncated:
            logger.warning("prompt-fix answer was cut off at temperature %s", temperature)
    if content is None:
        raise RuntimeError("The model's answer was cut off three times; try again")

    report = FixReport.model_validate_json(content).model_dump()
    known = {f["call_id"] for f in items}
    squashed_prompt = _squash(prompt)
    for fix in report["fixes"]:
        # Only keep call ids we actually supplied, and say whether the quoted instruction is really
        # in the prompt, so nobody edits the prompt on the strength of an invented quote.
        fix["example_call_ids"] = [c for c in fix["example_call_ids"] if c in known]
        quoted = _squash(fix["current_instruction"])
        fix["missing_rule"] = "no instruction covers" in fix["current_instruction"].lower()
        fix["quote_verified"] = bool(quoted) and not fix["missing_rule"] and quoted in squashed_prompt
    report.update(
        findings_used=len(items),
        fatal_used=sum(1 for f in items if f["grade"] == "FATAL"),
        prompt_chars=len(prompt),
        prompt_sha=hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12],
    )
    db.ex(
        "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
        KV_KEY, json.dumps(report, ensure_ascii=False),
    )  # fmt: skip
    return report


def saved() -> dict[str, Any] | None:
    row = db.one("SELECT value, updated_at FROM kv WHERE key=?", KV_KEY)
    return {**json.loads(row["value"]), "updated_at": row["updated_at"]} if row else None


def reviewed() -> list[dict[str, Any]]:
    """Hand-reviewed prompt issues from rubric/prompt_review.yaml, each quote checked against the prompt."""
    import yaml

    path = config.RUBRIC_DIR / "prompt_review.yaml"
    prompt = bot_prompt()
    if not path.is_file() or prompt is None:
        return []
    squashed = _squash(prompt)
    items = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("items", [])
    known = {r["call_id"] for r in db.q("SELECT call_id FROM call")}
    for item in items:
        item["quotes"] = [{"text": q, "verified": _squash(q) in squashed} for q in item.get("quotes", [])]
        item["seen_in"] = [s for s in item.get("seen_in", []) if str(s.get("call")) in known]
    return items