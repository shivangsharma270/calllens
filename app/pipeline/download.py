import asyncio
import logging
import shutil
import subprocess
from pathlib import Path

import httpx
import mutagen

from app import config, db

logger = logging.getLogger(__name__)


class NotAudio(ValueError):
    pass


def sniff_extension(head: bytes) -> str:
    """Decide the file type from its first bytes. Some recording URLs answer 200 with a login
    web page, so the content is checked rather than trusting the URL or the status code."""
    if head[:4] == b"RIFF":
        return ".wav"
    if head[:3] == b"ID3" or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        return ".mp3"
    if head[:4] == b"OggS":
        return ".ogg"
    kind = "a web page (the link needs a login)" if head.lstrip()[:1] == b"<" else "an unknown file type"
    raise NotAudio(f"recording URL returned {kind}, not audio")


def probe(path: Path) -> tuple[float, int]:
    """(duration in seconds, channel count) read from the file header."""
    info = mutagen.File(path)
    if info is None:
        raise NotAudio("file is not readable audio")
    duration = float(getattr(info.info, "length", 0) or 0)
    # mutagen misreads the length of some telephony WAVs (non-PCM codecs); ffprobe gets it right.
    if path.suffix == ".wav" and shutil.which("ffprobe"):
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30, check=False,
        )  # fmt: skip
        try:
            duration = float(result.stdout.strip())
        except ValueError:
            pass
    if duration <= 0:
        raise NotAudio("file is not readable audio")
    return duration, int(getattr(info.info, "channels", 1) or 1)


async def _download_one(client: httpx.AsyncClient, call_id: str, url: str) -> None:
    existing = db.val("SELECT audio_path FROM call WHERE call_id=?", call_id)
    if existing and Path(existing).is_file():
        return
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        ext = sniff_extension(resp.content[:16])
        path = config.AUDIO_DIR / f"{call_id}{ext}"
        await asyncio.to_thread(path.write_bytes, resp.content)
        duration, channels = await asyncio.to_thread(probe, path)
    except (httpx.HTTPError, NotAudio, OSError, mutagen.MutagenError) as exc:
        if isinstance(exc, NotAudio):
            message = str(exc)
        elif isinstance(exc, httpx.HTTPStatusError):  # keep the URL out of the message
            message = f"download failed: the recording link returned HTTP {exc.response.status_code}"
        else:
            message = f"download failed: {type(exc).__name__}"
        db.ex("UPDATE call SET status='error', error=? WHERE call_id=?", message[:500], call_id)
        raise
    db.ex(
        "UPDATE call SET audio_path=?, duration_s=?, channels=?, status='downloaded', error=NULL"
        " WHERE call_id=? AND status IN ('imported','error')",
        str(path), duration, channels, call_id,
    )  # fmt: skip


async def download_calls(call_ids: list[str], on_progress) -> None:  # noqa: ANN001
    """Fetch recordings 8 at a time. A failed download is recorded on the call; the run goes on."""
    semaphore = asyncio.Semaphore(config.DOWNLOAD_CONCURRENCY)
    urls = {
        r["call_id"]: r["recording_url"]
        for r in db.q(
            f"SELECT call_id, recording_url FROM call WHERE has_recording=1"
            f" AND call_id IN ({db.placeholders(call_ids)})",
            *call_ids,
        )
    }

    async with httpx.AsyncClient(timeout=90, follow_redirects=True) as client:

        async def one(call_id: str) -> None:
            async with semaphore:
                try:
                    await _download_one(client, call_id, urls[call_id])
                    on_progress(ok=True)
                except Exception:
                    on_progress(ok=False)

        await asyncio.gather(*(one(cid) for cid in urls))
