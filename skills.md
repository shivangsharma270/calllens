# skills.md: build journey and tools used

CallLens by TeamStellarx · IndiaMART Voice AI Hackathon · Problem 1: Quality Audit and Fix Loop

## Tools used

**Speech and language models (Sarvam AI)**

| Tool | Used for |
|---|---|
| Saaras v4, batch speech-to-text | Transcribing Hindi-English call audio, `codemix` mode, diarization on, 20 files per job |
| `sarvam-105b` chat completions | Labelling each sentence as bot or buyer, repairing misheard terms, grading calls, grouping root causes, writing the summary and prompt-fix suggestions |
| Strict JSON schema output | Every model answer comes back in a fixed shape and is validated before it is stored |

**Application**

| Tool | Used for |
|---|---|
| Python 3, FastAPI, Uvicorn | The web app and background jobs, in one process |
| SQLite (standard `sqlite3`) | All data, in one file |
| Jinja2, HTMX | Server-rendered pages and the live progress bar |
| Tailwind (CDN), Chart.js | Styling and the dashboard graphs |
| pandas | Reading the organisers' call log |
| httpx | Downloading recordings |
| mutagen, ffprobe | Reading audio length and channels |
| rapidfuzz | Detecting repeated bot lines and repeated buyer answers |
| Pydantic | Validating model output |
| pytest | 26 unit tests |

**Building and presenting**

| Tool | Used for |
|---|---|
| Claude Code (Anthropic) | AI pair-programmer: wrote the code, ran the experiments against the real data and reported what failed |
| Playwright with Chromium | Recording the demo video with a scripted browser |
| ffmpeg | Encoding the clips, transitions and narration; also the audio clean-up experiment |
| ElevenLabs | Narration audio for the demo video |
| pypdf | Extracting the bot's prompt text from its PDF |
| docx (npm), Claude Slides | The approach note and the one-slide presentation |

## Build journey

**1. We started too big, and cut it down.** The first plan was a production stack: Docker,
PostgreSQL with pgvector, Redis workers, MinIO storage and a Next.js front end. It could not even
start on our laptop, which had no Docker. We dropped all of it for one FastAPI process, one SQLite
file and server-rendered pages. Nothing we needed for the demo was lost, and we could finally run
real calls.

**2. The first real call broke our assumptions about speakers.** Sarvam's diarization swapped the
bot and the buyer partway through calls, and merged a buyer's short "जी" into the bot's sentence.
Mapping whole speakers by rule could not fix that. We split each transcript into sentences and had
`sarvam-105b` label every sentence from its content, then forced a short list of bot-only phrases
to BOT. On the calls we read, that fixed the labels.

**3. The data was not what the brief described.** The call log listed 717 recordings, but 152 links
returned a login page and 2 returned 403, so only 563 were downloadable. The 713 recordings we had
been given locally turned out to be a different batch with no call-log rows. We made the app accept
both sources, and made the downloader check that it had received real audio.

**4. The grader's own settings did not work.** The grading prompt specified low reasoning effort
with a 4,000-token limit. On all 20 test calls the model spent the whole budget thinking and
returned nothing. We turned thinking off and relied on the output schema's field order to walk the
model through the procedure. We also found two bugs of our own on the way: our segment-id parser
rejected correct quotes, and our retry format made the model answer with nothing.

**5. We tested whether cleaner audio gives better transcripts. It does not.** We transcribed the
same calls four ways: original, resampled and loudness-normalised, noise-reduced, and with a Hindi
language hint. The outputs differed only by noise, so we adopted none of them. What did help was
context: one small request per call that lists misheard product terms, such as "K2ATAA" for
"K2 EDTA", applied only when no number changes.

**6. Reading the bot's real prompt was the turning point.** The 77-page prompt showed that these
are inbound calls redirected from an unavailable seller, not outbound calls as we had assumed. It
also contradicts itself: retry limits differ between sections, one rule says never claim to be
human while another says not to reveal it is a virtual assistant, and three rules log a lead as
successful without the buyer confirming it. Those findings do not depend on any automatic grade,
and the app checks every quoted rule against the prompt file.

**7. We built the ranking and the review queue to the judging criteria.** Failed calls are grouped
into root causes and ranked by priority = frequency × severity × 100, with the inputs shown for
every cause. Risky calls are queued under the five required flags, each with a quoted line as evidence.

**8. We added cost tracking per call.** Every Sarvam request is tagged with its call, so each call
page shows what it cost. A fully tracked call costs about ₹1.80.

## What we learned

- **Run on real data early.** Every major change above came from a real call, not from the plan.
- **Verify what the model says.** Quotes are checked word for word against the transcript, and
  prompt rules against the prompt file, so the app never shows evidence that is not there.
- **A simple stack was enough.** One process and one file carried 1,276 recordings and 517 analysed calls.
- **Measure before adopting.** The audio clean-up idea sounded right and tested wrong.

## What is not done

- **No agreement score.** Grades have not been compared with human auditors, and the grader
  currently marks more calls Fatal than a person would.
- **No replay test.** Prompt fixes are suggested but not yet measured before and after.
- **Root-cause grouping at scale.** Past 250 failed calls the AI grouping falls back to a simpler
  grouping by cause type and fatal code.
