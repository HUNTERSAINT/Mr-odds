# Mr Odds (Telegram bot)

Pick days + target odds → Gemini searches Google for fixtures and odds, predicts, and the bot builds a slip near your target. Every slip is saved, and results are checked automatically so the bot shows its real hit rate.

## Commands
- `/start` – choose days and target odds
- `/stats` – bot-wide and personal hit rate
- `/history` – your last 5 slips with results

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
