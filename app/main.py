import json
import logging
import re
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path, PureWindowsPath
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import config, db, insights, jobs, prompt_fixes, root_causes
from app.pipeline.import_csv import import_csv, import_folder, pick_sample

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # it logs full request URLs at INFO

APP_DIR = Path(__file__).parent
PAGE_SIZE = 50
RISK_FLAGS = ["wrong_requirement", "frustration", "loop", "low_confidence", "human_request"]


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    db.conn()
    jobs.mark_interrupted()
    if db.val("SELECT COUNT(*) FROM call WHERE source='csv'") == 0 and config.CSV_PATH.is_file():
        import_csv()
    if db.val("SELECT COUNT(*) FROM call WHERE source='folder'") == 0:
        import_folder()
    yield


app = FastAPI(title="CallLens", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=APP_DIR / "templates")

# ---- display helpers -----------------------------------------------------------------------

_PHONE_RE = re.compile(r"(?<!\d)(?:\+?91[\s-]?)?\d(?:[\s-]?\d){9}(?!\d)")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def mask_pii(text: Any) -> str:
    """Mask phone numbers and emails for display. The database keeps the raw text."""
    return _PHONE_RE.sub("[phone]", _EMAIL_RE.sub("[email]", str(text or "")))


def mask_id(value: Any) -> str:
    digits = str(value or "").removesuffix(".0")
    return f"••••{digits[-4:]}" if len(digits) > 4 else digits


def fmt_duration(seconds: Any) -> str:
    if seconds is None:
        return "—"
    s = max(0, round(float(seconds)))
    return f"{s // 60}:{s % 60:02d}"


def pct(part: Any, whole: Any) -> str:
    return f"{100 * part / whole:.0f}%" if whole else "—"


templates.env.filters.update(mask=mask_pii, mask_id=mask_id, dur=fmt_duration, loads=db.loads)
templates.env.globals.update(pct=pct)


def page(request: Request, name: str, **context: Any) -> HTMLResponse:
    return templates.TemplateResponse(request, name, {"job": jobs.current(), "nav": name, **context})


# ---- dashboard -----------------------------------------------------------------------------


def overview() -> dict[str, Any]:
    stats = dict(
        db.one(
            """SELECT COUNT(*) AS total,
                      SUM(has_recording) AS recorded,
                      SUM(audio_path IS NOT NULL) AS downloaded,
                      SUM(has_recording=1 AND status='error' AND audio_path IS NULL) AS unreachable,
                      SUM(status='error' AND audio_path IS NOT NULL) AS failed,
                      (SELECT COUNT(*) FROM grade) AS audited,
                      SUM(source='folder') AS from_folder
               FROM call"""
        )
    )
    grades = {r["grade"]: r["n"] for r in db.q("SELECT grade, COUNT(*) AS n FROM grade GROUP BY grade")}
    stats["fatal"], stats["non_fatal"] = grades.get("FATAL", 0), grades.get("NON_FATAL", 0)
    stats["non_fatal_with_issues"] = db.val(
        "SELECT COUNT(*) FROM grade WHERE grade='NON_FATAL' AND non_fatal_issues NOT IN ('[]', '')"
    )
    stats["open_reviews"] = db.val("SELECT COUNT(DISTINCT call_id) FROM review_item WHERE status='open'")
    stats["spent_inr"] = db.val("SELECT COALESCE(SUM(cost_inr), 0) FROM api_call")
    stats["unsupported"] = db.val(
        "SELECT COUNT(*) FROM grade WHERE json_extract(disposition_check, '$.supported') = 'no'"
    )
    flags: Counter[str] = Counter()
    for row in db.q("SELECT risk_flags FROM grade"):
        flags.update(db.loads(row["risk_flags"], []))
    dispositions = db.q(
        """SELECT c.disposition, c.disposition_group,
                  COUNT(*) AS recorded,
                  SUM(g.call_id IS NOT NULL) AS audited,
                  SUM(g.grade='FATAL') AS fatal,
                  SUM(g.grade='NON_FATAL') AS non_fatal,
                  SUM(json_extract(g.disposition_check, '$.supported') = 'no') AS unsupported
           FROM call c LEFT JOIN grade g ON g.call_id = c.call_id
           WHERE c.has_recording = 1
           GROUP BY c.disposition ORDER BY recorded DESC"""
    )
    flag_counts = [(f, flags.get(f, 0)) for f in RISK_FLAGS]
    trend_data, spend_data = insights.trends(), insights.spend_breakdown()
    return {
        "stats": stats,
        "flags": flag_counts,
        "notes": insights.chart_notes(trend_data, stats, flag_counts, spend_data),
        "dispositions": dispositions,
        "findings": insights.computed(),
        "ai_summary": insights.saved_ai_summary(),
        "trends": trend_data,
        "fix_report": prompt_fixes.saved(),
        "spend": spend_data,
        "top_causes": root_causes.ranked()[:5],
    }


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    return page(request, "dashboard.html", **overview())


# ---- runs ----------------------------------------------------------------------------------


@app.get("/runs", response_class=HTMLResponse)
async def runs(request: Request, notice: str = "") -> HTMLResponse:
    statuses = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) AS n FROM call GROUP BY status")}
    errors = db.q(
        "SELECT error, COUNT(*) AS n FROM call WHERE error IS NOT NULL GROUP BY error ORDER BY n DESC LIMIT 12"
    )
    history = db.q("SELECT * FROM job ORDER BY id DESC LIMIT 10")
    return page(
        request, "runs.html",
        statuses=statuses, errors=errors, history=history, notice=notice,
        csv_present=config.CSV_PATH.is_file(), key_set=bool(config.SARVAM_API_KEY),
        sample_size=config.DEMO_SAMPLE_SIZE, stats=overview()["stats"], recordings_dir=str(config.RECORDINGS_DIR),
    )  # fmt: skip


def _back(notice: str) -> RedirectResponse:
    return RedirectResponse(f"/runs?{urlencode({'notice': notice})}", status_code=303)


@app.post("/runs/import")
async def runs_import() -> RedirectResponse:
    if not config.CSV_PATH.is_file():
        return _back(f"Put the organisers' CSV at {config.CSV_PATH} first.")
    result = import_csv()
    return _back(f"Imported {result['rows']} rows, {result['recorded']} with a recording.")


def _recorded_ids() -> list[str]:
    return [r["call_id"] for r in db.q("SELECT call_id FROM call WHERE has_recording=1 ORDER BY call_id")]


def _start(kind: str, work: Any, started: str) -> RedirectResponse:
    if jobs.start(kind, work) is None:
        return _back("A job is already running. Wait for it to finish.")
    return _back(started)


@app.post("/runs/download")
async def runs_download() -> RedirectResponse:
    ids = _recorded_ids()
    return _start("download", lambda p: jobs.download_only(p, ids), "Downloading recordings…")


@app.post("/runs/audit")
async def runs_audit(scope: Annotated[str, Form()], size: Annotated[int, Form()] = 0) -> RedirectResponse:
    if not config.SARVAM_API_KEY:
        return _back("SARVAM_API_KEY is not set in .env.")
    if scope == "all":
        ids, label = _recorded_ids(), "all recorded calls"
    else:
        ids = pick_sample(max(1, min(size or config.DEMO_SAMPLE_SIZE, 717)))
        label = f"a sample of {len(ids)} calls"
    if not ids:
        return _back("No calls to audit. Import the CSV first.")
    return _start("audit", lambda p: jobs.audit(p, ids), f"Auditing {label}…")


@app.post("/runs/regrade")
async def runs_regrade() -> RedirectResponse:
    """Grade every audited call again, e.g. after editing the rubric or bot goal.

    Transcripts are kept. Calls whose grader request is unchanged come straight from the cache.
    """
    if jobs.current() is not None:
        return _back("A job is already running. Wait for it to finish.")
    ids = [r["call_id"] for r in db.q("SELECT call_id FROM call WHERE status IN ('graded', 'transcribed')")]
    if not ids:
        return _back("No transcribed calls to grade yet.")
    db.ex("UPDATE call SET status='transcribed', error=NULL WHERE status='graded'")
    return _start("regrade", lambda p: jobs.audit(p, ids), f"Regrading {len(ids)} calls…")


@app.post("/runs/retry-errors")
async def runs_retry_errors() -> RedirectResponse:
    """Put failed calls back in line: those with audio are re-transcribed, the rest re-downloaded."""
    n = db.ex(
        "UPDATE call SET status = CASE WHEN audio_path IS NULL THEN 'imported' ELSE 'downloaded' END,"
        " error = NULL WHERE status = 'error'"
    ).rowcount
    return _back(f"{n} failed calls will be tried again on the next run.")


@app.get("/jobs/current", response_class=HTMLResponse)
async def job_progress(request: Request, was: str = "") -> Response:
    job = jobs.current()
    if job is None and was == "running":
        return Response(headers={"HX-Refresh": "true"})  # the job just ended: reload to show results
    return templates.TemplateResponse(request, "_progress.html", {"job": job})


# ---- calls ---------------------------------------------------------------------------------


@app.get("/calls", response_class=HTMLResponse)
async def calls(
    request: Request,
    grade: str = "",
    fatal_code: str = "",
    disposition: str = "",
    group: str = "",
    flag: str = "",
    status: str = "",
    supported: str = "",
    review: str = "",
    page_no: Annotated[int, Query(alias="page", ge=1)] = 1,
) -> HTMLResponse:
    where, params = ["1=1"], []
    for column, value in (
        ("g.grade", grade), ("g.fatal_code", fatal_code), ("c.disposition", disposition),
        ("c.disposition_group", group), ("c.status", status),
    ):  # fmt: skip
        if value:
            where.append(f"{column} = ?")
            params.append(value)
    if flag in RISK_FLAGS:
        where.append("g.risk_flags LIKE ?")
        params.append(f'%"{flag}"%')
    if supported in ("yes", "no", "unclear"):
        where.append("json_extract(g.disposition_check, '$.supported') = ?")
        params.append(supported)
    if review:
        where.append("c.call_id IN (SELECT call_id FROM review_item WHERE status = 'open')")
    if not status:
        where.append("c.has_recording = 1")
    clause = " AND ".join(where)
    total = db.val(f"SELECT COUNT(*) FROM call c LEFT JOIN grade g ON g.call_id=c.call_id WHERE {clause}", *params)
    rows = db.q(
        f"""SELECT c.*, g.grade, g.fatal_code, g.reason, g.risk_flags, g.confidence, g.needs_review,
                   g.non_fatal_issues, json_extract(g.disposition_check, '$.supported') AS supported,
                   (SELECT SUM(a.cost_inr) FROM api_call a WHERE a.call_id = c.call_id) AS spend
            FROM call c LEFT JOIN grade g ON g.call_id = c.call_id
            WHERE {clause}
            ORDER BY (g.grade = 'FATAL') DESC, g.grade IS NULL, c.call_id
            LIMIT ? OFFSET ?""",
        *params, PAGE_SIZE, (page_no - 1) * PAGE_SIZE,
    )  # fmt: skip
    filters = {
        "grade": grade, "fatal_code": fatal_code, "disposition": disposition, "group": group, "flag": flag,
        "status": status, "supported": supported, "review": review,
    }  # fmt: skip
    active = {k: v for k, v in filters.items() if v}
    return page(
        request, "calls.html",
        rows=rows, total=total, page_no=page_no, pages=max(1, -(-total // PAGE_SIZE)),
        filters=filters, query=urlencode(active), flags=RISK_FLAGS,
        dispositions=[r["disposition"] for r in db.q(
            "SELECT disposition, COUNT(*) n FROM call WHERE has_recording=1 GROUP BY disposition ORDER BY n DESC"
        )],
    )  # fmt: skip


@app.get("/calls/{call_id}", response_class=HTMLResponse)
async def call_detail(request: Request, call_id: str) -> HTMLResponse:
    call = db.one("SELECT * FROM call WHERE call_id=?", call_id)
    if call is None:
        raise HTTPException(404, "Call not found")
    evidence = db.q("SELECT * FROM evidence WHERE call_id=? ORDER BY utterance_idx", call_id)
    by_turn: dict[int, list[Any]] = {}
    for e in evidence:
        by_turn.setdefault(e["utterance_idx"], []).append(e)
    raw = json.loads(call["raw_json"] or "{}")
    raw_fields = []
    for key, value in raw.items():
        if key == "fk_glusr_usr_id" and value:
            value = mask_id(value)
        elif key == "call_recording_url":
            value = value.split("//", 1)[-1].split("/", 1)[0] if value else ""  # host only: paths can hold phone numbers
        raw_fields.append((key, mask_pii(value)))
    grade_row = db.one("SELECT * FROM grade WHERE call_id=?", call_id)
    return page(
        request, "call.html",
        call=call, grade=grade_row, spend=insights.call_spend(call_id),
        turns=db.q("SELECT * FROM utterance WHERE call_id=? ORDER BY idx", call_id),
        signals=db.q("SELECT * FROM signal WHERE call_id=? ORDER BY utterance_idx", call_id),
        evidence=evidence, by_turn=by_turn, raw_fields=raw_fields,
        has_audio=bool(call["audio_path"]) and Path(call["audio_path"]).is_file(),
    )  # fmt: skip


@app.get("/audio/{call_id}")
async def audio(call_id: str) -> FileResponse:
    """The local recording. FileResponse answers Range requests, so the player can seek."""
    path = db.val("SELECT audio_path FROM call WHERE call_id=?", call_id)
    if not path:
        raise HTTPException(404, "Recording not downloaded")
    # The stored path is from the machine that loaded the call; on another machine look the file up by name.
    name = PureWindowsPath(path).name
    folders = (config.AUDIO_DIR, config.RECORDINGS_DIR, config.SEED_DIR / "recordings")
    found = next((p for p in (Path(path), *(f / name for f in folders)) if p.is_file()), None)
    if found is None:
        raise HTTPException(404, "Recording not downloaded")
    return FileResponse(found, content_disposition_type="inline")


# ---- recordings folder, run analysis on chosen calls, AI summary ---------------------------


@app.post("/runs/import-folder")
async def runs_import_folder() -> RedirectResponse:
    if not config.RECORDINGS_DIR.is_dir():
        return _back(f"Recordings folder not found: {config.RECORDINGS_DIR}")
    result = import_folder()
    return _back(
        f"Recordings folder: {result['found']} audio files, {result['added']} added"
        + (f", {result['unreadable']} unreadable" if result["unreadable"] else "")
        + "."
    )


@app.get("/analyse", response_class=HTMLResponse)
async def analyse_pick(request: Request, q: str = "", show: str = "new", notice: str = "") -> HTMLResponse:
    """Pick calls to analyse. `show`: new = not analysed yet, done = analysed, all = both."""
    where, params = ["c.has_recording = 1", "c.status != 'no_recording'"], []
    if show == "new":
        where.append("g.call_id IS NULL")
    elif show == "done":
        where.append("g.call_id IS NOT NULL")
    if q.strip():
        where.append("(c.call_id LIKE ? OR c.original_name LIKE ? OR c.disposition LIKE ?)")
        params += [f"%{q.strip()}%"] * 3
    rows = db.q(
        f"""SELECT c.call_id, c.original_name, c.disposition, c.duration_s, c.status, c.source, c.error,
                   c.audio_path, g.grade, g.fatal_code
            FROM call c LEFT JOIN grade g ON g.call_id = c.call_id
            WHERE {' AND '.join(where)}
            ORDER BY (c.audio_path IS NULL), c.source DESC, c.call_id LIMIT 200""",
        *params,
    )
    return page(request, "analyse.html", rows=rows, q=q, show=show, notice=notice, key_set=bool(config.SARVAM_API_KEY))


@app.post("/analyse")
async def analyse_run(
    call_ids: Annotated[list[str] | None, Form()] = None,
    redo_transcript: Annotated[str, Form()] = "",
    next_url: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Run the full analysis on the chosen calls, again from scratch if they were analysed before."""

    def back(message: str) -> RedirectResponse:
        return RedirectResponse(f"/analyse?{urlencode({'notice': message, 'show': 'all'})}", status_code=303)

    ids = [i for i in (call_ids or []) if db.val("SELECT 1 FROM call WHERE call_id=? AND has_recording=1", i)]
    if not ids:
        return back("Select at least one call.")
    if not config.SARVAM_API_KEY:
        return back("SARVAM_API_KEY is not set in .env.")
    if jobs.current() is not None:
        return back("Another analysis is still running. Wait for it to finish.")
    marks = db.placeholders(ids)
    # Put each chosen call back to the earliest stage that needs redoing. Transcription itself is
    # cached per recording, so "redo transcript" re-runs speaker labelling and repair, not the paid STT.
    db.ex(
        f"UPDATE call SET error=NULL, status = CASE WHEN audio_path IS NULL THEN 'imported' ELSE 'downloaded' END"
        f" WHERE status='error' AND call_id IN ({marks})",
        *ids,
    )
    again = "('graded','transcribed')" if redo_transcript else "('graded')"
    target = "downloaded" if redo_transcript else "transcribed"
    db.ex(f"UPDATE call SET status='{target}' WHERE status IN {again} AND call_id IN ({marks})", *ids)
    jobs.start("analysis", lambda p: jobs.audit(p, ids))
    if next_url.startswith("/calls/"):
        return RedirectResponse(next_url, status_code=303)
    return RedirectResponse(f"/calls/{ids[0]}" if len(ids) == 1 else "/calls?status=graded", status_code=303)


@app.post("/summary/generate")
async def summary_generate() -> RedirectResponse:
    if config.SARVAM_API_KEY:
        try:
            await insights.generate_ai_summary()
        except Exception:
            logging.getLogger(__name__).exception("AI summary failed")
    return RedirectResponse("/#summary", status_code=303)

# ---- prompt fixes --------------------------------------------------------------------------


@app.get("/prompt-fixes", response_class=HTMLResponse)
async def prompt_fixes_page(request: Request, notice: str = "") -> HTMLResponse:
    prompt = prompt_fixes.bot_prompt()
    available = len(prompt_fixes.findings()) if prompt else 0
    return page(
        request, "prompt_fixes.html",
        report=prompt_fixes.saved(), notice=notice, has_prompt=prompt is not None, reviewed=prompt_fixes.reviewed(),
        prompt_chars=len(prompt or ""), findings_available=available,
        ready=bool(prompt and available and config.SARVAM_API_KEY),
    )  # fmt: skip


@app.post("/prompt-fixes/generate")
async def prompt_fixes_generate() -> RedirectResponse:
    notice = ""
    try:
        await prompt_fixes.generate()
    except Exception as exc:
        logging.getLogger(__name__).exception("prompt fixes failed")
        notice = f"Could not build recommendations: {exc}"
    return RedirectResponse(f"/prompt-fixes?{urlencode({'notice': notice})}" if notice else "/prompt-fixes", status_code=303)

# ---- root causes and review queue ----------------------------------------------------------


@app.get("/root-causes", response_class=HTMLResponse)
async def root_causes_page(request: Request, notice: str = "") -> HTMLResponse:
    causes = root_causes.ranked()
    available = len(root_causes.failed_calls())
    return page(
        request, "root_causes.html",
        causes=causes, meta=root_causes.meta(), notice=notice, available=available,
        ready=bool(available and config.SARVAM_API_KEY),
        chart_data=[{"name": c["name"], "priority": round(c["priority"], 1), "calls": c["call_count"]} for c in causes],
    )  # fmt: skip


@app.post("/root-causes/build")
async def root_causes_build() -> RedirectResponse:
    try:
        await root_causes.build()
    except Exception as exc:
        logging.getLogger(__name__).exception("root cause grouping failed")
        return RedirectResponse(f"/root-causes?{urlencode({'notice': f'Could not group: {exc}'})}", status_code=303)
    return RedirectResponse("/root-causes", status_code=303)


@app.get("/review", response_class=HTMLResponse)
async def review_page(request: Request, status: str = "open") -> HTMLResponse:
    status = "done" if status == "done" else "open"
    groups = root_causes.review_queue(status)
    return page(
        request, "review.html", groups=groups, status=status,
        total=sum(len(g["items"]) for g in groups),
        calls=len({i["call_id"] for g in groups for i in g["items"]}),
    )  # fmt: skip


@app.post("/review/{item_id}/done")
async def review_done(item_id: int) -> RedirectResponse:
    db.ex("UPDATE review_item SET status='done' WHERE id=?", item_id)
    return RedirectResponse("/review", status_code=303)