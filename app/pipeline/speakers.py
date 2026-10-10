"""Turn raw Saaras output into BOT / CALLER turns.

Sarvam's diarization of these 8 kHz mono calls swaps speakers mid-call and merges short replies
into the other speaker's entry (seen on real recordings). So the transcript is split into
sentences and an LLM labels each one from its content; phrases only the bot says are then forced
to BOT. If that fails, the rule-based mapping of whole diarized speakers is the fallback:
(a) the speaker with the read-back / greeting patterns is the bot, (b) else the first speaker.
"""

import logging
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import yaml
from pydantic import BaseModel
from rapidfuzz import fuzz

from app import config, sarvam

logger = logging.getLogger(__name__)

TURN_GAP_S = 1.5  # consecutive sentences from one speaker closer than this are one turn
_SENTENCE_END_RE = re.compile(r"(?<=[।?!.])\s+")


@dataclass(frozen=True)
class Seg:
    speaker: str  # Sarvam's id before labelling, then BOT / CALLER / UNKNOWN
    start_s: float
    end_s: float
    text: str
    raw: str = ""  # Sarvam's text when `text` was repaired; empty means unchanged

    @property
    def original(self) -> str:
        return self.raw or self.text


@lru_cache
def phrases(key: str) -> tuple[re.Pattern[str], ...]:
    data = yaml.safe_load((config.RUBRIC_DIR / "signal_phrases.yaml").read_text(encoding="utf-8")) or {}
    return tuple(re.compile(p, re.IGNORECASE) for p in data.get(key, []))


def matches(key: str, text: str) -> bool:
    return any(p.search(text) for p in phrases(key))


def entries_from_raw(raw: dict[str, Any]) -> list[Seg]:
    """Raw Saaras output as time-ordered segments carrying Sarvam's own speaker ids."""
    entries = (raw.get("diarized_transcript") or {}).get("entries") or []
    segs = [
        Seg(
            str(e.get("speaker_id", "?")),
            float(e.get("start_time_seconds") or 0.0),
            float(e.get("end_time_seconds") or 0.0),
            re.sub(r"\s+", " ", str(e.get("transcript") or "")).strip(),
        )
        for e in entries
    ]
    segs = [s for s in segs if s.text]
    if not segs:  # not diarized: fall back to plain phrase timestamps, speaker unknown
        ts = raw.get("timestamps") or {}
        texts = ts.get("chunks") or ts.get("words") or []
        starts, ends = ts.get("start_time_seconds") or [], ts.get("end_time_seconds") or []
        segs = [
            Seg("?", float(s), float(e), str(t).strip())
            for t, s, e in zip(texts, starts, ends, strict=False)
            if str(t).strip()
        ]
    return sorted(segs, key=lambda s: (s.start_s, s.end_s))


def split_sentences(segs: list[Seg]) -> list[Seg]:
    """Split segments into sentences so a reply merged into the other speaker's entry can be
    relabelled. Sarvam gives one timestamp per phrase, so times inside it are interpolated."""
    out: list[Seg] = []
    for seg in segs:
        parts = [p for p in _SENTENCE_END_RE.split(seg.text) if p.strip()]
        total = sum(len(p) for p in parts) or 1
        pos = 0
        for part in parts:
            start = seg.start_s + (seg.end_s - seg.start_s) * pos / total
            pos += len(part)
            end = seg.start_s + (seg.end_s - seg.start_s) * pos / total
            out.append(Seg(seg.speaker, round(start, 2), round(end, 2), part.strip()))
    return out


def merge_turns(segs: list[Seg], max_gap_s: float = TURN_GAP_S) -> list[Seg]:
    out: list[Seg] = []
    for seg in segs:
        prev = out[-1] if out else None
        if prev and prev.speaker == seg.speaker and seg.start_s - prev.end_s <= max_gap_s:
            text, original = f"{prev.text} {seg.text}", f"{prev.original} {seg.original}"
            out[-1] = Seg(
                prev.speaker, prev.start_s, max(prev.end_s, seg.end_s), text, "" if original == text else original
            )
        else:
            out.append(seg)
    return out


def map_by_rules(segs: list[Seg]) -> tuple[str | None, str]:
    """Which diarized speaker is the bot? Returns (speaker id or None, method)."""
    ids = list(dict.fromkeys(s.speaker for s in segs))
    if len(ids) < 2:
        return None, "single_speaker" if ids else "empty"
    hits: Counter[str] = Counter()
    for seg in segs:
        hits[seg.speaker] += sum(1 for p in phrases("bot_patterns") if p.search(seg.text))
    top = max(ids, key=lambda i: hits[i])
    runner_up = max(hits[i] for i in ids if i != top)
    if hits[top] > runner_up:
        return top, "pattern"
    return segs[0].speaker, "first_speaker"


class _Label(BaseModel):
    i: int
    speaker: str
    text: str = ""


class _Labels(BaseModel):
    labels: list[_Label]


MIN_FIX_SIMILARITY = 70  # a "repair" less similar to the original than this is a rewrite: rejected


def accept_fix(original: str, fixed: str) -> str:
    """Keep the model's repaired sentence only if it is a small, safe edit; else the original.

    Safe means: every number is untouched, and the text is still mostly the same words.
    """
    fixed = re.sub(r"\s+", " ", fixed).strip()
    if not fixed or fixed == original:
        return original
    if re.findall(r"\d+", fixed) != re.findall(r"\d+", original):
        return original
    if fuzz.ratio(original.lower(), fixed.lower()) < MIN_FIX_SIMILARITY:
        return original
    return fixed


_SCHEMA = {
    "name": "sentence_speakers",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["labels"],
        "properties": {
            "labels": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["i", "speaker", "text"],
                    "properties": {
                        "i": {"type": "integer"},
                        "speaker": {"type": "string", "enum": ["bot", "caller"]},
                        "text": {"type": "string"},
                    },
                },
            }
        },
    },
}


CHUNK_SENTENCES = 30


async def _ask_labels(key: str, prompt: str, user: str) -> str:
    # Strict-JSON mode sometimes runs on emitting whitespace until max_tokens; it is random, so a
    # cut-off answer is asked for again with a little temperature to break the loop.
    for temperature in (0.0, 0.2, 0.4):
        try:
            result = await sarvam.chat(
                "speaker_turns",
                key,
                [{"role": "system", "content": prompt}, {"role": "user", "content": user}],
                json_schema=_SCHEMA,
                # Reasoning off: with it on the model can spend the whole budget thinking and
                # return nothing (seen on real calls); this is per-line classification and repair.
                reasoning_effort=None,
                temperature=temperature,
                max_tokens=4000,
            )
            return result.content
        except sarvam.ChatTruncated:
            logger.warning("speaker labelling answer for %s was cut off at temperature %s", key, temperature)
    raise sarvam.ChatTruncated(f"speaker labelling for {key} was cut off three times")


async def label_sentences_llm(call_id: str, sentences: list[Seg]) -> list[Seg] | None:
    """Each sentence with its speaker set to BOT/CALLER and obvious mis-hearings repaired
    (`raw` keeps Sarvam's text). None if the model's answer is unusable."""
    prompt = (config.PROMPT_DIR / "speaker_turns.md").read_text(encoding="utf-8")

    def line(i: int) -> str:
        s = sentences[i]
        return f"[{i}] spk{s.speaker} ({s.start_s:.1f}s): {s.text}"

    by_index: dict[int, _Label] = {}
    try:
        # The answer repeats every sentence, so long calls go in chunks to stay inside max_tokens.
        # Each chunk is shown the first lines of the call (product name) and the lines just before it.
        for start in range(0, len(sentences), CHUNK_SENTENCES):
            end = min(start + CHUNK_SENTENCES, len(sentences))
            context = sorted({*range(min(3, start)), *range(max(0, start - 5), start)})
            user = ""
            if context:
                user += "Earlier lines, for context only (do not return them):\n"
                user += "\n".join(line(i) for i in context) + "\n\n"
            user += "<<<TRANSCRIPT START>>>\n" + "\n".join(line(i) for i in range(start, end))
            user += f"\n<<<TRANSCRIPT END>>>\n\nReturn sentence indexes {start} to {end - 1}."
            result = await _ask_labels(f"{call_id}-{start}", prompt, user)
            for x in _Labels.model_validate_json(result).labels:
                if start <= x.i < end:
                    by_index[x.i] = x
    except Exception:
        logger.exception("speaker labelling failed for call %s", call_id)
        return None
    speakers = {x.speaker.upper() for x in by_index.values()}
    if set(by_index) != set(range(len(sentences))) or not speakers <= {"BOT", "CALLER"}:
        logger.warning("speaker labelling incomplete for call %s", call_id)
        return None
    out = []
    for i, s in enumerate(sentences):
        text = accept_fix(s.text, by_index[i].text)
        speaker = "BOT" if matches("bot_only_patterns", text) else by_index[i].speaker.upper()
        out.append(Seg(speaker, s.start_s, s.end_s, text, raw="" if text == s.text else s.text))
    return out


class _TermFix(BaseModel):
    heard: str
    correct: str


class _TermFixes(BaseModel):
    fixes: list[_TermFix]


_TERMS_SCHEMA = {
    "name": "term_fixes",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["fixes"],
        "properties": {
            "fixes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["heard", "correct"],
                    "properties": {"heard": {"type": "string"}, "correct": {"type": "string"}},
                },
            }
        },
    },
}
MAX_TERM_CHARS = 40


def usable_fixes(fixes: list[tuple[str, str]], transcript: str) -> list[tuple[str, str]]:
    """Keep replacements that are short term swaps really present in the transcript and that
    leave every number alone. Longest first, so "K2ATAA" is replaced before a bare "ATAA"."""
    kept = []
    for heard, correct in fixes:
        heard, correct = heard.strip(), correct.strip()
        if (
            len(heard) >= 3
            and correct
            and heard != correct
            and max(len(heard), len(correct)) <= MAX_TERM_CHARS
            and heard in transcript
            and re.findall(r"\d+", heard) == re.findall(r"\d+", correct)
        ):
            kept.append((heard, correct))
    return sorted(set(kept), key=lambda pair: -len(pair[0]))


async def term_fixes(call_id: str, turns: list[Seg]) -> list[tuple[str, str]]:
    """Ask the model which product / technical terms were misheard in this call."""
    transcript = "\n".join(f"{t.speaker}: {t.text}" for t in turns)
    try:
        result = await sarvam.chat(
            "transcript_terms",
            call_id,
            [
                {"role": "system", "content": (config.PROMPT_DIR / "transcript_terms.md").read_text(encoding="utf-8")},
                {"role": "user", "content": f"<<<TRANSCRIPT START>>>\n{transcript}\n<<<TRANSCRIPT END>>>"},
            ],
            json_schema=_TERMS_SCHEMA,
            reasoning_effort=None,
            max_tokens=1200,
        )
        found = [(f.heard, f.correct) for f in _TermFixes.model_validate_json(result.content).fixes]
    except Exception:
        logger.exception("term repair failed for call %s; transcript left as recognised", call_id)
        return []
    return usable_fixes(found, transcript)


def apply_term_fixes(turns: list[Seg], fixes: list[tuple[str, str]]) -> list[Seg]:
    out = []
    for t in turns:
        text = t.text
        for heard, correct in fixes:
            text = text.replace(heard, correct)
        out.append(t if text == t.text else Seg(t.speaker, t.start_s, t.end_s, text, raw=t.original))
    return out


async def labelled_turns(call_id: str, raw: dict[str, Any]) -> tuple[list[Seg], str]:
    """Turns with speaker BOT / CALLER / UNKNOWN, plus the method that labelled them."""
    entries = entries_from_raw(raw)
    if not entries:
        return [], "empty"
    sentences = split_sentences(entries)
    if len(sentences) > 1:
        relabelled = await label_sentences_llm(call_id, sentences)
        if relabelled is not None:
            turns = merge_turns(relabelled)
            return apply_term_fixes(turns, await term_fixes(call_id, turns)), "llm_sentences"
    bot_id, method = map_by_rules(entries)
    named = [
        Seg("UNKNOWN" if bot_id is None else ("BOT" if s.speaker == bot_id else "CALLER"), s.start_s, s.end_s, s.text)
        for s in entries
    ]
    return merge_turns(named), method
