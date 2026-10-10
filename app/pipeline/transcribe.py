import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from app import config, db, sarvam
from app.pipeline.speakers import labelled_turns

logger = logging.getLogger(__name__)


async def store_transcript(call_id: str, raw: dict[str, Any]) -> None:
    """Label speakers and write utterance rows. Replaces any earlier rows for the call."""
    turns, method = await labelled_turns(call_id, raw)
    probability = raw.get("language_probability")
    with db.tx():
        db.ex("DELETE FROM utterance WHERE call_id=?", call_id)
        db.many(
            "INSERT INTO utterance (call_id, idx, speaker, start_s, end_s, text, raw_text) VALUES (?,?,?,?,?,?,?)",
            [(call_id, i, t.speaker, t.start_s, t.end_s, t.text, t.raw or None) for i, t in enumerate(turns)],
        )
        db.ex(
            "UPDATE call SET status='transcribed', error=NULL, language_code=?, language_probability=?,"
            " speaker_method=? WHERE call_id=?",
            raw.get("language_code"), float(probability) if probability is not None else None, method, call_id,
        )  # fmt: skip


async def transcribe_calls(call_ids: list[str], on_progress) -> None:  # noqa: ANN001
    """Transcribe downloaded calls: cached output first, the rest in Saaras batch jobs of 20."""
    rows = db.q(
        f"SELECT call_id, audio_path, duration_s FROM call WHERE status='downloaded'"
        f" AND call_id IN ({db.placeholders(call_ids)}) ORDER BY call_id",
        *call_ids,
    )
    todo = []
    for row in rows:
        cached = sarvam.cache_get("stt", row["call_id"])
        if cached is None:
            todo.append(row)
            continue
        await _finish(row["call_id"], cached, on_progress)

    batches = [todo[i : i + config.STT_BATCH_SIZE] for i in range(0, len(todo), config.STT_BATCH_SIZE)]
    semaphore = asyncio.Semaphore(config.STT_JOB_CONCURRENCY)

    async def run_batch(batch: list[Any]) -> None:
        async with semaphore:
            by_name = {Path(r["audio_path"]).name: r["call_id"] for r in batch}
            seconds = sum(r["duration_s"] or 0 for r in batch)
            length = {r["call_id"]: r["duration_s"] or 0 for r in batch}
            started = time.monotonic()
            try:
                results = await sarvam.transcribe_batch([Path(r["audio_path"]) for r in batch], seconds)
            except Exception as exc:
                logger.exception("STT batch failed")
                results = {name: f"stt batch failed: {type(exc).__name__}: {exc}" for name in by_name}
            for name, call_id in by_name.items():
                raw = results.get(name, "missing from job result")
                if isinstance(raw, str):
                    db.ex("UPDATE call SET status='error', error=? WHERE call_id=?", f"stt: {raw}"[:500], call_id)
                    on_progress(ok=False)
                    continue
                sarvam.cache_put("stt", call_id, raw)
                # Sarvam bills transcription per second of audio, so each call's share is exact.
                sarvam.log_call(
                    "stt_transcription", config.SARVAM_STT_MODEL, started, "200",
                    cost_inr=sarvam.stt_cost_inr(length[call_id]), call_id=call_id,
                )  # fmt: skip
                await _finish(call_id, raw, on_progress)

    await asyncio.gather(*(run_batch(b) for b in batches))


async def _finish(call_id: str, raw: dict[str, Any], on_progress) -> None:  # noqa: ANN001
    token = sarvam.current_call.set(call_id)  # speaker labelling and term repair are costed to this call
    try:
        await store_transcript(call_id, raw)
        on_progress(ok=True)
    except Exception as exc:
        logger.exception("could not store transcript for %s", call_id)
        db.ex("UPDATE call SET status='error', error=? WHERE call_id=?", f"transcript: {exc}"[:500], call_id)
        on_progress(ok=False)
    finally:
        sarvam.current_call.reset(token)
