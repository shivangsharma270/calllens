"""Background jobs: asyncio tasks in this process, with a `job` row the UI polls for progress."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app import db, sarvam
from app.pipeline.download import download_calls
from app.pipeline.grade import grade_call
from app.pipeline.signals import compute_signals
from app.pipeline.transcribe import transcribe_calls

logger = logging.getLogger(__name__)

_task: asyncio.Task[Any] | None = None


class Progress:
    def __init__(self, job_id: int) -> None:
        self.job_id = job_id

    def stage(self, message: str, total: int) -> None:
        db.ex("UPDATE job SET message=?, total=?, done=0 WHERE id=?", message, total, self.job_id)

    def tick(self, ok: bool = True) -> None:
        db.ex("UPDATE job SET done=done+1, errors=errors+? WHERE id=?", 0 if ok else 1, self.job_id)


def current() -> Any:
    return db.one("SELECT * FROM job WHERE status='running' ORDER BY id DESC LIMIT 1")


def latest() -> Any:
    return db.one("SELECT * FROM job ORDER BY id DESC LIMIT 1")


def mark_interrupted() -> None:
    """Jobs die with the process. Finished work is kept, so starting the job again resumes it."""
    db.ex(
        "UPDATE job SET status='interrupted', finished_at=CURRENT_TIMESTAMP,"
        " message='Stopped when the app restarted. Start it again to continue.' WHERE status='running'"
    )


def start(kind: str, work: Callable[[Progress], Awaitable[None]]) -> int | None:
    """Start a job unless one is running. Returns the job id, or None if busy."""
    global _task
    if current() is not None:
        return None
    job_id = db.ex("INSERT INTO job (kind, status, message) VALUES (?, 'running', 'Starting')", kind).lastrowid
    assert job_id is not None

    async def runner() -> None:
        try:
            await work(Progress(job_id))
            db.ex("UPDATE job SET status='done', finished_at=CURRENT_TIMESTAMP WHERE id=?", job_id)
        except Exception as exc:
            logger.exception("job %s failed", job_id)
            db.ex(
                "UPDATE job SET status='failed', finished_at=CURRENT_TIMESTAMP, message=? WHERE id=?",
                f"{type(exc).__name__}: {exc}"[:500], job_id,
            )  # fmt: skip

    _task = asyncio.create_task(runner(), name=f"job:{job_id}")
    return job_id


def _with_status(call_ids: list[str], *statuses: str) -> list[str]:
    rows = db.q(
        f"SELECT call_id FROM call WHERE call_id IN ({db.placeholders(call_ids)})"
        f" AND status IN ({db.placeholders(statuses)}) ORDER BY call_id",
        *call_ids, *statuses,
    )  # fmt: skip
    return [r["call_id"] for r in rows]


async def download_only(progress: Progress, call_ids: list[str]) -> None:
    todo = _with_status(call_ids, "imported")
    progress.stage("Downloading recordings", len(todo))
    await download_calls(todo, progress.tick)
    failed = db.val("SELECT errors FROM job WHERE id=?", progress.job_id)
    db.ex(
        "UPDATE job SET message=? WHERE id=?",
        f"Downloaded {len(todo) - failed} recordings; {failed} could not be fetched", progress.job_id,
    )  # fmt: skip


async def audit(progress: Progress, call_ids: list[str]) -> None:
    """Download → transcribe → signals → grade. Each stage only touches calls that still need it."""
    todo = _with_status(call_ids, "imported")
    progress.stage("Downloading recordings", len(todo))
    await download_calls(todo, progress.tick)

    todo = _with_status(call_ids, "downloaded")
    progress.stage("Transcribing and labelling speakers", len(todo))
    await transcribe_calls(todo, progress.tick)

    todo = _with_status(call_ids, "transcribed")
    progress.stage("Grading calls", len(todo))

    async def grade_one(call_id: str) -> None:
        sarvam.current_call.set(call_id)  # each grading task has its own context, so this is per call
        try:
            await grade_call(call_id, compute_signals(call_id))
            progress.tick(ok=True)
        except Exception as exc:
            logger.exception("grading failed for %s", call_id)
            # Stays 'transcribed' so the next run retries it; the error is shown on the call.
            db.ex("UPDATE call SET error=? WHERE call_id=?", f"grading: {type(exc).__name__}: {exc}"[:500], call_id)
            progress.tick(ok=False)

    await asyncio.gather(*(grade_one(cid) for cid in todo))
    graded = len(_with_status(call_ids, "graded"))
    db.ex(
        "UPDATE job SET message=? WHERE id=?",
        f"Finished: {graded} of {len(call_ids)} selected calls are graded", progress.job_id,
    )  # fmt: skip
