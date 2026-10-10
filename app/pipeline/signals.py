"""Rule-based signals (no LLM). Each one points at the utterance that triggered it.

They are hints for the grader and filters for the review queue, not verdicts.
"""

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from app import db
from app.pipeline.speakers import matches

LOOP_RATIO = 85
LOOP_WINDOW = 5
CALLER_REPEAT_RATIO = 80
MIN_REPEAT_CHARS = 12  # ignore "ठीक है" / "जी" repeats, which are normal
LONG_SILENCE_S = 6.0
LOW_LANGUAGE_PROBABILITY = 0.6


@dataclass(frozen=True)
class Turn:
    idx: int
    speaker: str
    start_s: float
    end_s: float
    text: str


@dataclass(frozen=True)
class Signal:
    type: str
    utterance_idx: int | None
    detail: str


def _norm(text: str) -> str:
    return re.sub(r"[^\w\s]", "", text.lower()).strip()


def _similar(a: str, b: str, threshold: int) -> bool:
    a, b = _norm(a), _norm(b)
    return min(len(a), len(b)) >= MIN_REPEAT_CHARS and fuzz.ratio(a, b) >= threshold


def detect(
    turns: list[Turn],
    *,
    duration_s: float | None = None,
    language_probability: float | None = None,
    disposition_group: str | None = None,
) -> list[Signal]:
    out: list[Signal] = []
    bot = [t for t in turns if t.speaker == "BOT"]
    caller = [t for t in turns if t.speaker == "CALLER"]

    # loop: two bot turns alike within 5 turns, or the same line 3+ times anywhere
    for i, later in enumerate(bot):
        earlier_alike = [e for e in bot[:i] if _similar(e.text, later.text, LOOP_RATIO)]
        near = [e for e in earlier_alike if later.idx - e.idx <= LOOP_WINDOW]
        if near or len(earlier_alike) >= 2:
            first = (near or earlier_alike)[0]
            out.append(Signal("loop", later.idx, f"bot repeats S{first.idx}"))

    # human_request
    for t in caller:
        if matches("human_request", t.text):
            out.append(Signal("human_request", t.idx, "caller asks for a person"))

    # frustration: phrases, the caller repeating themselves, or 3+ bare "hello?" turns
    for i, t in enumerate(caller):
        if matches("frustration", t.text):
            out.append(Signal("frustration", t.idx, "frustration phrase"))
        elif any(_similar(e.text, t.text, CALLER_REPEAT_RATIO) for e in caller[:i]):
            out.append(Signal("frustration", t.idx, "caller repeats an earlier answer"))
    hellos = [t for t in caller if matches("hello_turn", t.text.strip())]
    if len(hellos) >= 3:
        out.append(Signal("frustration", hellos[2].idx, f"{len(hellos)} bare hello/haan turns"))

    # requirement_rejected: first caller turn after the bot's read-back says it is wrong
    read_back = next((t for t in bot if matches("read_back", t.text)), None)
    if read_back:
        reply = next((t for t in caller if t.idx > read_back.idx), None)
        if reply and matches("requirement_rejected", reply.text):
            out.append(Signal("requirement_rejected", reply.idx, f"reply to read-back S{read_back.idx}"))

    # abrupt_end
    if turns:
        last = turns[-1]
        last_bot = bot[-1] if bot else None
        if last.speaker == "BOT" and last.text.rstrip().endswith("?"):
            out.append(Signal("abrupt_end", last.idx, "call ends on an unanswered bot question"))
        elif last_bot and not matches("closing", last_bot.text):
            out.append(Signal("abrupt_end", last_bot.idx, "last bot turn is not a closing"))
        elif (
            last.speaker == "CALLER"
            and last.text.rstrip().endswith("?")
            and duration_s is not None
            and duration_s - last.end_s < 2.0
        ):
            out.append(Signal("abrupt_end", last.idx, "call ends right after a caller question"))

    # low_confidence
    if language_probability is not None and language_probability < LOW_LANGUAGE_PROBABILITY:
        out.append(Signal("low_confidence", None, f"language probability {language_probability:.2f}"))
    confused = [t for t in bot if matches("bot_did_not_understand", t.text)]
    if len(confused) >= 2:
        out.append(Signal("low_confidence", confused[1].idx, f"bot asked to repeat {len(confused)} times"))

    # long_silence
    for prev, nxt in zip(turns, turns[1:], strict=False):
        gap = nxt.start_s - prev.end_s
        if gap > LONG_SILENCE_S:
            out.append(Signal("long_silence", nxt.idx, f"{gap:.0f} s of silence before this turn"))

    # disposition_suspect: logged as success, yet the caller rejected the requirement or asked for a human
    fired = {s.type for s in out}
    if disposition_group == "success" and fired & {"requirement_rejected", "human_request"}:
        why = " and ".join(sorted(fired & {"requirement_rejected", "human_request"}))
        out.append(Signal("disposition_suspect", None, f"logged as success but {why} fired"))
    return out


def load_turns(call_id: str) -> list[Turn]:
    return [
        Turn(r["idx"], r["speaker"], r["start_s"] or 0.0, r["end_s"] or 0.0, r["text"])
        for r in db.q("SELECT * FROM utterance WHERE call_id=? ORDER BY idx", call_id)
    ]


def compute_signals(call_id: str) -> list[Signal]:
    call = db.one("SELECT duration_s, language_probability, disposition_group FROM call WHERE call_id=?", call_id)
    signals = detect(
        load_turns(call_id),
        duration_s=call["duration_s"],
        language_probability=call["language_probability"],
        disposition_group=call["disposition_group"],
    )
    with db.tx():
        db.ex("DELETE FROM signal WHERE call_id=?", call_id)
        db.many(
            "INSERT INTO signal (call_id, type, utterance_idx, detail) VALUES (?,?,?,?)",
            [(call_id, s.type, s.utterance_idx, s.detail) for s in signals],
        )
    return signals
