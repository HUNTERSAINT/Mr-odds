# Mr Odds (Telegram bot)

Pick days + target odds → Gemini searches Google for fixtures and odds, predicts, and the bot builds a slip near your target. Every slip is saved, and results are checked automatically so the bot shows its real hit rate.

## Commands
- `/start` – choose days and target odds
- `/stats` – bot-wide and personal hit rate
- `/history` – your last 5 slips with results

## Data source
With `ODDS_API_KEY` set, fixtures/odds come from The Odds API (cached in SQLite, 7 leagues, only matches priced by 5+ bookmakers) and Gemini just analyses them. Without it, the bot falls back to Gemini + Google Search (needs search-grounding quota on the Gemini key).

## Markets
Slips can mix: home/draw/away, double chance (1X, X2, 12), draw no bet, over/under 0.5 / 1.5 / 2.5 / 3.5 goals and both teams to score (GG/NG). One pick per match.

- Home/draw/away use real bookmaker prices (median across bookmakers).
- The other markets are **estimated** from those prices (Poisson goals model, see `markets.py`) with an assumed 6% margin, and are marked `(est.)` in the slip. Real prices differ a little, so check them before betting.
- Real prices for the extra markets cost extra Odds API credits per match, which the free plan (500/month) cannot afford at scale.
- Target odds up to 1000x work: the slip builder uses dynamic programming over log-odds, so it can combine up to 20 games in well under a second.
- If Gemini is unavailable, picks are ranked by probability from the odds instead.

## How it works
1. Gemini + Google Search writes research notes (fixtures, odds, form, injuries).
2. A structured Gemini call turns them into JSON picks; code rejects wrong dates and impossible odds.
3. The slip builder finds the combination closest to your target odds.
4. Every few hours a job finds final scores for finished games (Gemini + Google Search), marks picks won/lost and messages the user when a slip is settled.

## Environment variables
See `.env.example`. Never commit real keys.

## Run locally
```
pip install -r requirements.txt
cp .env.example .env   # fill in keys
python bot.py
```

## Railway
Worker service, start command `python bot.py` (set in `railway.json`). Add a volume mounted at `/data` and set `DB_PATH=/data/bot.db` so history survives redeploys.
