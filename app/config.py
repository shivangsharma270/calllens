import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
AUDIO_DIR = DATA_DIR / "audio"
CACHE_DIR = DATA_DIR / "cache"
DB_PATH = DATA_DIR / "calllens.db"
CSV_PATH = DATA_DIR / "Call_Logs_Hackathon.csv"
RUBRIC_DIR = ROOT / "rubric"
# Folder of call recordings on this machine; every audio file in it becomes a call.
RECORDINGS_DIR = Path(os.getenv("RECORDINGS_DIR", str(ROOT.parent / "call_recordings")))
PROMPT_DIR = ROOT / "app" / "prompts"

# Read once at import; never passed to templates or written to logs.
SARVAM_API_KEY = os.getenv("SARVAM_API_KEY", "").strip()
SARVAM_LLM_RPM = int(os.getenv("SARVAM_LLM_RPM", "40"))
SARVAM_STT_RPM = int(os.getenv("SARVAM_STT_RPM", "20"))
SARVAM_STT_MODEL = os.getenv("SARVAM_STT_MODEL", "saaras:v4")
# "unknown" = auto-detect the language (also returns language_probability); or e.g. "hi-IN".
SARVAM_STT_LANGUAGE = os.getenv("SARVAM_STT_LANGUAGE", "unknown")
SARVAM_LLM_MODEL = os.getenv("SARVAM_LLM_MODEL", "sarvam-105b")
SARVAM_DIALOG_MODEL = os.getenv("SARVAM_DIALOG_MODEL", "sarvam-105b-conversations")
MAX_CONCURRENT_LLM = int(os.getenv("MAX_CONCURRENT_LLM", "4"))
DEMO_SAMPLE_SIZE = int(os.getenv("DEMO_SAMPLE_SIZE", "120"))

DOWNLOAD_CONCURRENCY = 8
STT_BATCH_SIZE = 20
STT_JOB_CONCURRENCY = 2

for _d in (AUDIO_DIR, CACHE_DIR):
    _d.mkdir(parents=True, exist_ok=True)
