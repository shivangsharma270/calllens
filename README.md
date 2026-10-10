# CallLens by TeamStellarx

IndiaMART Voice AI Hackathon · Problem 1: Quality Audit and Fix Loop

CallLens audits recorded calls of IndiaMART's AI voice bot. For each call it transcribes the
audio, separates the bot from the buyer, grades the call Fatal or Non-Fatal with quoted evidence,
and checks the outcome the system logged against what was actually said. Across calls it groups
failures into ranked root causes, keeps a review queue of risky calls, and proposes changes to the
bot's prompt.

It is one Python process with one SQLite file. No Docker, no Node, no login.

## What it does

| Requirement | Where in the app |
|---|---|
| Grade every call Fatal / Non-Fatal with evidence | **Calls** and each call page: grade, reason, evidence quotes that seek the audio |
| Group failures into root causes | **Root Causes**: failed calls grouped, each cause opens to its calls |
| Rank by frequency and severity | **Root Causes**: the priority formula and its inputs are shown for every cause |
| Suggest the prompt fix | **Prompt Fixes**: current wording next to replacement text |
| One dashboard | **Dashboard**: six graphs, key numbers, summary, top root causes, spend |
| Flag risky calls | **Review Queue**: wrong requirement, frustration, loops, low confidence, human request |

Not built yet: a labelling screen with an agreement score against human auditors, and replaying
calls to measure a prompt fix before and after.

## Run it

Needs Python 3.11 or newer and internet access (the pages load Tailwind, HTMX, Chart.js and a font
from CDNs). `ffprobe` on PATH is optional and only used to read WAV durations.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env            # put your SARVAM_API_KEY in .env
.\.venv\Scripts\python.exe run.py  # then open http://127.0.0.1:8000
```

Data goes in two places, and both are imported on first start:

- `data\Call_Logs_Hackathon.csv`: the organisers' call log.
- A folder of recordings, `..\call_recordings` by default. Set `RECORDINGS_DIR` in `.env` to use
  another folder. These recordings are used in place and have no call-log entry, so they show
  "Not in call log" instead of a disposition.

To analyse calls, press **Run Analysis**, tick one or more calls (or **Select all 200 calls**), and
press **Run analysis on selected**. Picking a single call lands on its analysis. Everything is
resumable: finished calls are skipped, and every Sarvam answer is cached in `data\cache\`, so
repeating a run costs nothing.

## How a call is processed

1. **Import** (`app/pipeline/import_csv.py`): call-log rows and folder recordings become calls.
2. **Download** (`download.py`): recordings from call-log links, 8 at a time. The file's first
   bytes are checked, because some links answer with a login page instead of audio.
3. **Transcribe** (`transcribe.py`, `app/sarvam.py`): Sarvam Saaras v4 batch speech-to-text,
   `codemix` mode, diarization on, 20 files per job.
4. **Label speakers and repair terms** (`speakers.py`): the transcript is split into sentences and
   `sarvam-105b` labels each one bot or buyer. A second small request lists misheard product terms
   and their corrections; a correction is applied only if the term is really in the transcript and
   no number changes. The original text stays visible under any repaired line.
5. **Signals** (`signals.py`, no LLM): loops, human requests, frustration, rejected requirement,
   abrupt end, low confidence, long silence. Phrase lists are in `rubric/signal_phrases.yaml`, in
   both Devanagari and Latin script.
6. **Grade** (`grade.py`): one `sarvam-105b` request per call using `app/prompts/grader.md`
   unmodified, with a strict JSON schema.

Then, across calls:

- **Root causes** (`app/root_causes.py`): `sarvam-105b` groups failed calls; every call lands in
  exactly one cause. If the model cannot group, calls are grouped by cause type and fatal code.
- **Review queue** (`app/root_causes.py`): one item per risk flag per call.
- **Prompt fixes** (`app/prompt_fixes.py`): the bot's real prompt (`data/bot_prompt.txt`) is
  compared with the failed calls.
- **Dashboard** (`app/insights.py`): counts, trends and spend.

## Grading logic

A call is **Fatal** if any one of these is clearly shown; the most severe code is reported.

| Code | Condition |
|---|---|
| F2 | Wrong requirement captured, or a successful lead logged without the buyer confirming |
| F1 | Wrong information given |
| F5 | Compliance or consent breach |
| F7 | Request for a human ignored |
| F3 | Stuck in a loop |
| F4 | Bot or system dropped the call |
| F6 | Call ended without the goal, bot at fault |

Otherwise it is **Non-Fatal**, with any minor issues listed. Each answer is then checked:

- Every evidence quote must appear word for word in the segment it cites (punctuation ignored). A
  real quote cited under the wrong segment is re-pointed to the right one.
- A Fatal grade needs a fatal code and at least one piece of grade evidence.
- On failure the grader is asked once more with the error; if that fails the grade is kept and
  flagged for review.
- Contradictions go straight to review: Non-Fatal with a fatal code, or F2 although the buyer confirmed.

## Priority score for root causes

```
frequency = calls with this root cause / all analysed calls
severity  = average call weight / 3
            weight 3: Fatal
            weight 2: Non-Fatal, but the buyer was frustrated or the call did not reach its goal
            weight 1: otherwise
priority  = frequency x severity x 100        (0 to 100)
```

## Sarvam spend

Every Sarvam request is logged with its tokens and rupee cost, tagged with the call it was for.
Each call page shows its own spend split into transcription, speaker labelling and grading; the
Calls list has a spend column; the dashboard shows the total and the average per call (about ₹1.80
for a fully tracked call). Transcription is billed per second of audio, so that part is exact.

## What the real data showed

- **154 of the 717 call-log recordings cannot be downloaded.** 152 links return a login page
  instead of audio and 2 return HTTP 403.
- **Sarvam's speaker separation is unreliable on these 8 kHz mono calls.** It swaps speakers
  mid-call and merges short replies into the other speaker's line, which is why speakers are
  labelled per sentence by the LLM.
- **Cleaning the audio did not improve transcripts.** We tested ffmpeg resampling, loudness
  normalisation, noise reduction and a Hindi language hint; none helped, so none is used.
- **The grader cannot run with `reasoning_effort="low"` and `max_tokens=4000`** as `grader.md`'s
  header asks: on all 20 test calls the model used the whole budget thinking and returned nothing.
  Grading runs with thinking off.
- **In strict-JSON mode the model sometimes emits whitespace until the token limit.** It is
  random, so a cut-off answer is asked for again, up to three times.
- **The bot's own prompt contradicts itself.** `rubric/prompt_review.yaml` lists five issues with
  the exact rules quoted; the app checks each quote against the prompt file.

## Known limits

- **Grades are not yet verified.** The automatic grader marks more calls Fatal than a person
  would (for example, it often treats a recording that simply ends as "the bot dropped the call"),
  and re-analysing a call can change its grade. Read Fatal counts as calls flagged for attention.
- **Root-cause grouping is capped at 250 calls** per request. Beyond that the model's answer can be
  cut off and the app falls back to grouping by cause type and fatal code.
- Speaker labels can still be wrong where speech overlaps.
- Turn start times inside a split line are estimates; Sarvam gives one timestamp per phrase.
- AI spend from before per-call tracking was added is counted in the total but not split per call.
- One process only: the rate limiter and job runner live in memory.

## Files the team can edit without touching code

- `rubric/bot_goal.md`: what the bot is supposed to do, summarised from its real prompt.
- `rubric/fatal_rubric.md`: the fatal conditions.
- `rubric/signal_phrases.yaml`: phrase lists for signals and speaker labelling.
- `rubric/prompt_review.yaml`: the reviewed prompt issues shown on the Prompt Fixes page.
- `rubric/dispositions.yaml`: disposition to group mapping.
- `rubric/keyterms.txt`: domain words sent to Saaras.
- `app/prompts/*.md`: the LLM prompts.

After editing the rubric or bot goal, press **Regrade audited calls** on the Data Runs page.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

26 tests cover the signal rules, quote validation, grading checks, the priority score, root-cause
assignment, transcript repair, CSV import and audio sniffing.

See `skills.md` for the build journey and the tools used.
