import os
from dotenv import load_dotenv

load_dotenv()


def _req(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing {name} in environment")
    return v


TELEGRAM_TOKEN = _req("TELEGRAM_TOKEN")
GEMINI_API_KEY = _req("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
# Tried in order when a model returns 404 (retired) or 429 (quota exhausted).
_fallbacks = os.getenv("GEMINI_FALLBACK_MODELS", "gemini-3.8-flash-lite,gemini-2.5-flash-lite,gemini-2.0-flash")
MODELS = [GEMINI_MODEL] + [m.strip() for m in _fallbacks.split(",") if m.strip() and m.strip() != GEMINI_MODEL]
CACHE_SECONDS = int(os.getenv("CACHE_MINUTES", "30")) * 60
TIMEZONE = "Africa/Lagos"

# On Railway, mount a volume at /data and set DB_PATH=/data/bot.db so history survives deploys.
DB_PATH = os.getenv("DB_PATH", "bot.db")
SETTLE_EVERY_HOURS = float(os.getenv("SETTLE_EVERY_HOURS", "3"))
