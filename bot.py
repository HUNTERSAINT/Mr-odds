import logging
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
import db
import results
from research import LOCAL, client, get_picks
from slip import build_slip

logging.basicConfig(level=logging.INFO)
# httpx logs full request URLs, which contain the Telegram bot token
logging.getLogger("httpx").setLevel(logging.WARNING)

DISCLAIMER = (
    "\n\nOdds come from web search and can be out of date: confirm them on your "
    "bookmaker before betting. Analysis only, not guaranteed. Bet responsibly, 18+ only."
)
PICK_LABEL = {"home": "Home win", "draw": "Draw", "away": "Away win"}
STATUS_ICON = {"won": "✅", "lost": "❌", "pending": "⏳", "void": "➖"}


def _today():
    return datetime.now(LOCAL).date()


def _days_keyboard(selected: set):
    rows = []
    for i in range(7):
        d = _today() + timedelta(days=i)
        mark = "✅ " if d in selected else ""
        rows.append(
            [InlineKeyboardButton(f"{mark}{d:%a %d %b}", callback_data=f"day:{d.isoformat()}")]
        )
    rows.append([InlineKeyboardButton("Done ➡️", callback_data="days_done")])
    return InlineKeyboardMarkup(rows)


def _odds_keyboard():
    vals = [2, 3, 5, 10, 20]
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"{v}x", callback_data=f"odds:{v}") for v in vals]]
    )


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    ctx.user_data["days"] = set()
    await update.message.reply_text(
        "Pick the days you want games from (tap to select), then press Done.\n"
        "Commands: /stats for the bot's real results, /history for your last slips.",
        reply_markup=_days_keyboard(set()),
    )


async def on_day(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    sel = ctx.user_data.setdefault("days", set())
    d = datetime.fromisoformat(q.data.split(":", 1)[1]).date()
    sel.symmetric_difference_update({d})
    await q.edit_message_reply_markup(_days_keyboard(sel))


async def on_days_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    sel = ctx.user_data.get("days")
    if not sel:
        await q.answer("Select at least one day", show_alert=True)
        return
    await q.answer()
    ctx.user_data["awaiting_odds"] = True
    await q.edit_message_text(
        "What total odds do you want? Tap one or type a number (e.g. 4.5).",
        reply_markup=_odds_keyboard(),
    )


async def on_odds_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await _run(q.message, q.from_user.id, ctx, float(q.data.split(":", 1)[1]))


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.get("awaiting_odds"):
        await update.message.reply_text("Send /start to begin.")
        return
    try:
        target = float(update.message.text.replace(",", "."))
        assert 1.2 <= target <= 100
    except Exception:
        await update.message.reply_text("Send a number between 1.2 and 100, e.g. 5 or 4.5")
        return
    await _run(update.message, update.effective_user.id, ctx, target)


async def _run(message, user_id: int, ctx, target: float):
    days = sorted(ctx.user_data.get("days", []))
    ctx.user_data["awaiting_odds"] = False
    await message.reply_text("Searching games and analysing, this can take up to a minute...")
    try:
        matches, preds = await get_picks(days)
        if not matches:
            await message.reply_text(
                "No games with verifiable odds found for those days. Try other days with /start."
            )
            return
        result = build_slip(matches, preds, target)
    except Exception as e:
        logging.exception("run failed")
        if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
            await message.reply_text(
                "Gemini quota reached for now. Try again in a few minutes."
            )
        else:
            await message.reply_text("Something went wrong. Try again in a minute.")
        return

    if not result:
        await message.reply_text(
            f"Couldn't build a slip near {target}x from those games. "
            "Try different days or a different target." + DISCLAIMER
        )
        return

    legs, total = result
    slip_id = db.save_slip(user_id, message.chat_id, target, total, legs)
    out = [f"Slip #{slip_id}: target {target}x → {total:.2f}x ({len(legs)} picks)\n"]
    for l in legs:
        m, p = l["match"], l["pred"]
        out.append(
            f"{m['kickoff']:%a %d %b %H:%M} | {m['league']}\n"
            f"{m['home']} vs {m['away']}\n"
            f"Pick: {PICK_LABEL[p.pick]} @ {l['odds']} (confidence {p.confidence}%)\n"
            f"{p.reasoning}\n"
        )
    await message.reply_text("\n".join(out) + DISCLAIMER)


def _rate(won: int, lost: int) -> str:
    done = won + lost
    return f"{won}/{done} ({won / done * 100:.0f}%)" if done else "no settled results yet"


def _stats_block(title: str, s: dict) -> str:
    p, sl = s["picks"], s["slips"]
    return (
        f"{title}\n"
        f"Picks won: {_rate(p.get('won', 0), p.get('lost', 0))}\n"
        f"Slips won: {_rate(sl['won'], sl['lost'])}\n"
        f"Waiting for results: {sl['pending']} slips"
    )


async def stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = _stats_block("All users", db.stats()) + "\n\n" + _stats_block(
        "Your slips", db.stats(update.effective_user.id)
    )
    await update.message.reply_text(
        text + "\n\nResults are checked automatically after games finish. Void picks are excluded."
    )


def _format_slip(slip, legs, status: str) -> str:
    lines = [f"Slip #{slip['id']} ({slip['total_odds']}x): {status.upper()} {STATUS_ICON[status]}"]
    for l in legs:
        score = f" {l['score']}" if l["score"] else ""
        lines.append(
            f"{STATUS_ICON[l['status']]} {l['home']} vs {l['away']}{score} "
            f"({PICK_LABEL[l['pick']]} @ {l['odds']})"
        )
    return "\n".join(lines)


async def history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = db.recent_slips(update.effective_user.id)
    if not rows:
        await update.message.reply_text("No slips yet. Send /start to make one.")
        return
    await update.message.reply_text(
        "\n\n".join(_format_slip(s, legs, db.slip_status(legs)) for s, legs in rows)
    )


async def settle_job(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        await results.settle_pending()
        for slip, legs, status in db.finished_unnotified_slips():
            try:
                await ctx.bot.send_message(
                    slip["chat_id"], "Result update\n\n" + _format_slip(slip, legs, status)
                )
            except Exception:
                logging.exception("could not notify slip %s", slip["id"])
            db.mark_notified(slip["id"])
    except Exception:
        logging.exception("settle job failed")


async def on_startup(app):
    try:
        names = []
        async for m in await client.aio.models.list():
            if "generateContent" in (m.supported_actions or []):
                names.append(m.name.replace("models/", ""))
        logging.info("Gemini models available to this key: %s", ", ".join(names))
        logging.info("Model order used: %s", ", ".join(config.MODELS))
    except Exception:
        logging.exception("could not list Gemini models")

    # Diagnostics: does the key work at all, and does Google Search grounding work?
    from google.genai import types as gt
    from research import generate

    try:
        await generate(contents="Reply with the single word OK")
        logging.info("DIAG plain generate: OK")
    except Exception as e:
        logging.info("DIAG plain generate FAILED: %s", str(e)[:150])
    try:
        await generate(
            contents="Use Google Search: who won the most recent Premier League match?",
            config=gt.GenerateContentConfig(
                tools=[gt.Tool(google_search=gt.GoogleSearch())]
            ),
        )
        logging.info("DIAG search grounding: OK")
    except Exception as e:
        logging.info("DIAG search grounding FAILED: %s", str(e)[:150])


def main():
    app = Application.builder().token(config.TELEGRAM_TOKEN).post_init(on_startup).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("history", history))
    app.add_handler(CallbackQueryHandler(on_day, pattern=r"^day:"))
    app.add_handler(CallbackQueryHandler(on_days_done, pattern=r"^days_done$"))
    app.add_handler(CallbackQueryHandler(on_odds_button, pattern=r"^odds:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.job_queue.run_repeating(
        settle_job, interval=config.SETTLE_EVERY_HOURS * 3600, first=120
    )
    app.run_polling()


if __name__ == "__main__":
    main()
