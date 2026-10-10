import json
import random
from collections import defaultdict
from pathlib import Path

import pandas as pd
import yaml

from app import config, db


def load_disposition_groups() -> dict[str, str]:
    data = yaml.safe_load((config.RUBRIC_DIR / "dispositions.yaml").read_text(encoding="utf-8"))
    return {name: group for group, names in data["groups"].items() for name in names}


def clean_id(value: str) -> str:
    """The CSV stores ids as floats ("91046386.0"); keep the integer part as text."""
    value = (value or "").strip()
    return value[:-2] if value.endswith(".0") else value


def import_csv(path: Path | None = None) -> dict[str, int]:
    """Load every row of the organisers' CSV. Safe to run again: pipeline results are kept."""
    path = path or config.CSV_PATH
    frame = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    groups = load_disposition_groups()
    rows = []
    for record in frame.to_dict(orient="records"):
        call_id = clean_id(record["fk_leap_call_records_id"])
        if not call_id:
            continue
        url = record["call_recording_url"].strip()
        disposition = record["disposition_remarks"].strip()
        rows.append(
            (
                call_id,
                url or None,
                1 if url else 0,
                disposition or "(blank)",
                groups.get(disposition, "other"),
                clean_id(record["is_call_answered"]),
                record["entered_on"].strip(),
                clean_id(record["fk_eto_ofr_display_id"]),
                json.dumps(record, ensure_ascii=False),
                "imported" if url else "no_recording",
            )
        )
    with db.tx():
        # Only the CSV-derived columns are refreshed; status and results of earlier work survive.
        db.many(
            """INSERT INTO call (call_id, recording_url, has_recording, disposition, disposition_group,
                                 is_call_answered, entered_on, lead_id, raw_json, status)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(call_id) DO UPDATE SET
                 recording_url=excluded.recording_url, has_recording=excluded.has_recording,
                 disposition=excluded.disposition, disposition_group=excluded.disposition_group,
                 is_call_answered=excluded.is_call_answered, entered_on=excluded.entered_on,
                 lead_id=excluded.lead_id, raw_json=excluded.raw_json""",
            rows,
        )
    return {"rows": len(rows), "recorded": sum(r[2] for r in rows)}


FOLDER_DISPOSITION = "Not in call log"
AUDIO_SUFFIXES = {".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac"}


def folder_call_id(path: Path, taken: set[str]) -> str:
    """Recordings are named <n>_<call id>_<key>.mp3: use the id part when it is a free number."""
    parts = path.stem.split("_")
    candidate = parts[1] if len(parts) >= 3 and parts[1].isdigit() else path.stem
    return candidate if candidate not in taken else path.stem


def import_folder(folder: Path | None = None) -> dict[str, int]:
    """Add every recording in the local recordings folder as a call (files are used in place).

    These calls have no row in the organisers' log, so they carry no logged disposition.
    Safe to run again: files already imported are skipped.
    """
    from app.pipeline.download import NotAudio, probe  # local import: avoids a cycle at startup

    folder = folder or config.RECORDINGS_DIR
    if not folder.is_dir():
        return {"found": 0, "added": 0, "unreadable": 0}
    files = sorted(p for p in folder.iterdir() if p.suffix.lower() in AUDIO_SUFFIXES)
    known = {r["audio_path"] for r in db.q("SELECT audio_path FROM call WHERE source='folder'")}
    taken = {r["call_id"] for r in db.q("SELECT call_id FROM call")}
    rows, unreadable = [], 0
    for path in files:
        if str(path) in known:
            continue
        try:
            duration, channels = probe(path)
        except (NotAudio, OSError):  # one bad file must not stop the import
            unreadable += 1
            continue
        call_id = folder_call_id(path, taken)
        taken.add(call_id)
        rows.append((call_id, FOLDER_DISPOSITION, "unlogged", str(path), duration, channels, path.name))
    with db.tx():
        db.many(
            """INSERT INTO call (call_id, has_recording, disposition, disposition_group, audio_path,
                                 duration_s, channels, original_name, source, status, raw_json)
               VALUES (?, 1, ?, ?, ?, ?, ?, ?, 'folder', 'downloaded', '{}')""",
            rows,
        )
    return {"found": len(files), "added": len(rows), "unreadable": unreadable}


def pick_sample(size: int, seed: int = 7) -> list[str]:
    """Stratified demo sample of calls that still need auditing.

    Every disposition present gets at least one call (so every non-success group is covered),
    and the rest of the sample is shared out in proportion to each disposition's size.
    Calls whose recording could not be fetched are left out.
    """
    by_disposition: dict[str, list[str]] = defaultdict(list)
    for row in db.q(
        "SELECT call_id, disposition FROM call WHERE has_recording=1 AND status != 'error' ORDER BY call_id"
    ):
        by_disposition[row["disposition"]].append(row["call_id"])
    total = sum(len(ids) for ids in by_disposition.values())
    if total <= size:
        return [cid for ids in by_disposition.values() for cid in ids]

    rng = random.Random(seed)
    quota = {d: 1 for d in by_disposition}
    spare = max(size - len(quota), 0)
    for d, ids in by_disposition.items():
        quota[d] = min(len(ids), quota[d] + int(spare * len(ids) / total))
    # Hand out what rounding left over, largest dispositions first.
    for d in sorted(by_disposition, key=lambda d: -len(by_disposition[d])):
        if sum(quota.values()) >= size:
            break
        quota[d] = min(len(by_disposition[d]), quota[d] + size - sum(quota.values()))
    picked: list[str] = []
    for d, ids in by_disposition.items():
        picked += rng.sample(ids, min(quota[d], len(ids)))
    return picked
