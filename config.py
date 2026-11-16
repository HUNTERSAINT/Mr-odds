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
_fallbacks = os.getenv("GEMINI_FALLBACK_MODELS", "gemini-3.7-flash,gemini-3.6-flash,gemini-3.5-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite")
MODELS = [GEMINI_MODEL] + [m.strip() for m in _fallbacks.split(",") if m.strip() and m.strip() != GEMINI_MODEL]
CACHE_SECONDS = int(os.getenv("CACHE_MINUTES", "30")) * 60
TIMEZONE = "Africa/Lagos"

# The Odds API (free plan: 500 credits/month). When set, it is the main data source.
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
ODDS_REGIONS = os.getenv("ODDS_REGIONS", "eu")
MIN_BOOKMAKERS = int(os.getenv("MIN_BOOKMAKERS", "5"))
# A "strong" match = the market gives the favourite at least this win probability.
HIGH_PROB = float(os.getenv("HIGH_PROB", "0.55"))
# How many days ahead the day menu looks (odds usually exist ~1-2 weeks out).
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "14"))
# Cached odds are reused this long (menu and general use)...
ODDS_CACHE_SECONDS = int(float(os.getenv("ODDS_CACHE_HOURS", "24")) * 3600)
# ...but when someone builds a slip, leagues with games on the chosen days are refreshed if older than this.
SLIP_FRESH_SECONDS = int(float(os.getenv("SLIP_FRESH_HOURS", "12")) * 3600)
# At most this many in-season leagues are fetched (each fetch costs 1 credit).
MAX_LEAGUES = int(os.getenv("MAX_LEAGUES", "14"))
# Below this many credits left, stop fetching and serve saved data (keeps credits for results).
CREDIT_RESERVE = int(os.getenv("CREDIT_RESERVE", "40"))

# On Railway, mount a volume at /data and set DB_PATH=/data/bot.db so history survives deploys.
DB_PATH = os.getenv("DB_PATH", "bot.db")
SETTLE_EVERY_HOURS = float(os.getenv("SETTLE_EVERY_HOURS", "8"))
