"""/all: build a slip from EVERY league on the chosen days (separate from /start).

Pick today / tomorrow / both / a few days / the weekend (or tick days yourself), then the
target odds. Uses its own state keys and button prefix ("all:"), so /start is unaffected."""
import asyncio
import logging
import math
import time
from datetime import datetime, timedelta, timezone

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import allleagues
import config
import db
import markets
import odds
import predictor
from research import LOCAL
from slip import build_slip, max_reachable

CHUNK = 40  # matches per Gemini call
MAX_MATCHES = 120  # most matches considered (clearest favourites first)
GEMINI_WAIT = 75  # seconds before falling back to odds-based picks
_cache: dict = {}
_H: dict = {}  # helpers passed in from bot.py


def _today():
    return datetime.now(LOCAL).date()


def _presets() -> dict:
    t = _today()
    days = [t + timedelta(days=i) for i in range(8)]
    weekend = [d for d in days if d.weekday() >= 5][:2]
    if weekend and weekend[0].weekday() == 6:
        weekend = weekend[:1]
    p = {
        "today": [t],
        "tmrw": [t + timedelta(days=1)],
        "2d": [t, t + timedelta(days=1)],
        "3d": [t + timedelta(days=i) for i in range(3)],
    }
    if weekend:
        p["wknd"] = weekend
    return p


def _days_kb(selected: set):
    rows = [
        [InlineKeyboardButton("Today", callback_data="all:p:today"),
         InlineKeyboardButton("Tomorrow", callback_data="all:p:tmrw")],
        [InlineKeyboardButton("Today + Tomorrow", callback_data="all:p:2d"),
         InlineKeyboardButton("Next 3 days", callback_data="all:p:3d")],
    ]
    if "wknd" in _presets():
        rows.append([InlineKeyboardButton("This weekend", callback_data="all:p:wknd")])
    week = [_today() + timedelta(days=i) for i in range(7)]
    for i in range(0, 7, 2):
        rows.append([
            InlineKeyboardButton(
                f"{'✅ ' if d in selected else ''}{d:%a %d %b}", callback_data=f"all:d:{d.isoformat()}"
            )
            for d in week[i:i + 2]
        ])
    rows.append([InlineKeyboardButton("Done ➡️", callback_data="all:done")])
    return InlineKeyboardMarkup(rows)


def _odds_kb():
    rows = [[2, 3, 5, 10, 20], [50, 100, 200, 500]]
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"{v}x", callback_data=f"all:o:{v}") for v in row] for row in rows]
    )


async def cmd_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["all_days"] = set()
    ctx.user_data["all_awaiting"] = False
    ctx.user_data.pop("awaiting_odds", None)  # a number typed now belongs to /all
    await update.message.reply_text(
        "All leagues mode: I use every league that has games on your days, so a short "
        "stretch (1-2 days) can still reach big odds.\n\n"
        "Tap a quick choice, or tick the days you want and press Done.\n"
        "(/start is the normal mode with the top leagues.)",
        reply_markup=_days_kb(set()),
    )


async def _ask_odds(q, ctx, days):
    ctx.user_data["all_days"] = set(days)
    ctx.user_data["all_awaiting"] = True
    txt = ", ".join(f"{d:%a %d %b}" for d in sorted(days))
    await q.edit_message_text(
        f"Days: {txt}\nWhat total odds do you want? Tap one or type a number (e.g. 4.5).",
        reply_markup=_odds_kb(),
    )


async def on_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = q.data.split(":")
    kind = parts[1]
    sel = ctx.user_data.setdefault("all_days", set())
    if kind == "p":
        days = _presets().get(parts[2])
        await q.answer()
        if days:
            await _ask_odds(q, ctx, days)
    elif kind == "d":
        await q.answer()
        sel.symmetric_difference_update({datetime.fromisoformat(parts[2]).date()})
        await q.edit_message_reply_markup(_days_kb(sel))
    elif kind == "done":
        if not sel:
            await q.answer("Select at least one day", show_alert=True)
            return
        await q.answer()
        await _ask_odds(q, ctx, sel)
    elif kind == "o":
        await q.answer()
        if not ctx.user_data.get("all_days"):
            await q.message.reply_text("Send /all to start again.")
            return
        await _run(q.message, q.from_user.id, ctx, float(parts[2]))


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Runs before /start's text handler; only acts while /all is waiting for odds."""
    if not ctx.user_data.get("all_awaiting"):
        return
    try:
        target = float(update.message.text.replace(",", "."))
        assert 1.2 <= target <= config.MAX_TARGET
    except Exception:
        await update.message.reply_text(
            f"Send a number between 1.2 and {config.MAX_TARGET:g}, e.g. 5, 4.5 or 150"
        )
        raise ApplicationHandlerStop
    await _run(update.message, update.effective_user.id, ctx, target)
    raise ApplicationHandlerStop


async def _picks(days: list):
    """(matches, cands, info) for every league on these days."""
    key = tuple(days)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < config.CACHE_SECONDS:
        return hit[1]

    matches, info = await allleagues.get_matches(days)
    if not matches:
        return [], {}, info
    for m in matches:
        m["selections"] = markets.selections(m["odds"])
    matches = sorted(matches, key=lambda m: -odds.favourite_prob(m["odds"]))[:MAX_MATCHES]

    cands, ai_ok = {}, True
    chunks = [matches[i:i + CHUNK] for i in range(0, len(matches), CHUNK)]
    try:  # all chunks at once, and never wait more than GEMINI_WAIT seconds in total
        parts = await asyncio.wait_for(
            asyncio.gather(*(predictor.predict(c) for c in chunks), return_exceptions=True),
            GEMINI_WAIT,
        )
    except asyncio.TimeoutError:
        logging.warning("Gemini too slow for /all, using odds-based picks")
        parts = []
    for part in parts:
        if isinstance(part, dict) and part:
            cands.update(part)
        else:
            ai_ok = False
    rest = [m for m in matches if m["id"] not in cands]
    if rest:
        ai_ok = False
        cands.update(predictor.market_only(rest))

    result = (matches, cands, info)
    if ai_ok:
        _cache[key] = (time.time(), result)
    return result


async def _run(message, user_id: int, ctx, target: float):
    days = sorted(ctx.user_data.get("all_days", []))
    ctx.user_data["all_awaiting"] = False
    await message.reply_text("Collecting games from every league, one moment...")
    try:
        matches, cands, info = await _picks(days)
        if not matches:
            await message.reply_text(
                "No games with listed odds on those days in any league. Try other days with /all."
            )
            return
        result = await asyncio.to_thread(build_slip, matches, cands, target)
    except Exception as e:
        logging.exception("/all run failed")
        if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
            await message.reply_text("Gemini quota reached for now. Try again in a few minutes.")
        else:
            await message.reply_text("Something went wrong. Try again in a minute.")
        return

    if not result:
        reach = max_reachable(matches, cands)
        hint = (
            f"The most I can reach with these days is about {reach:,.0f}x. "
            "Add another day or lower the target."
            if reach and reach < target
            else "Try different days or a slightly different target."
        )
        await message.reply_text(f"Couldn't build a slip near {target:g}x. {hint}")
        return

    legs, total = result
    slip_id = db.save_slip(user_id, message.chat_id, target, total, legs)
    chance = math.prod(l["cand"]["confidence"] / 100 for l in legs)
    leagues = len({l["match"]["league"] for l in legs})
    out = [
        f"All-leagues slip #{slip_id}: target {target:g}x → {total:.2f}x ({len(legs)} picks, "
        f"{leagues} leagues, chosen from {len(matches)} games)\n"
        f"Chance that every pick wins (model estimate): {chance * 100:.1f}%\n"
    ]
    for l in legs:
        m, c = l["match"], l["cand"]
        tag = "" if c["real"] else " (est.)"
        out.append(
            f"{m['kickoff']:%a %d %b %H:%M} | {m['league']}\n"
            f"{m['home']} vs {m['away']}\n"
            f"Pick: {c['label']} @ {l['odds']}{tag} (confidence {c['confidence']}%)\n"
            f"{c['reasoning']}\n"
        )
    if info.get("skipped"):
        out.append(f"({info['skipped']} small leagues skipped to save odds-data credits.)\n")
    note = _H["est_note"] if any(not l["cand"]["real"] for l in legs) else ""
    await _H["send_long"](message, "\n".join(out) + note + _H["disclaimer"])


async def _settle_extras() -> int:
    """Settle picks from leagues outside odds.LEAGUES (the normal settle job only knows those)."""
    from results import FINISHED_AFTER

    cutoff = (datetime.now(timezone.utc) - FINISHED_AFTER).isoformat(timespec="seconds")
    rows = db.matches_to_settle(cutoff)
    names = allleagues.saved_names()
    extra = [r for r in rows if r["league"] in names and names[r["league"]] not in odds.LEAGUES]
    if not extra:
        return 0
    found = {}
    async with httpx.AsyncClient(timeout=20) as c:
        for key in {names[r["league"]] for r in extra}:
            r = await c.get(
                f"{odds.BASE}/sports/{key}/scores/",
                params={"apiKey": config.ODDS_API_KEY, "daysFrom": 3, "dateFormat": "iso"},
            )
            if r.status_code in (404, 422):
                continue
            r.raise_for_status()
            odds.note_credits(r.headers)
            for ev in r.json():
                if not ev.get("completed") or not ev.get("scores"):
                    continue
                try:
                    sc = {s["name"]: int(s["score"]) for s in ev["scores"]}
                    found[(ev["home_team"], ev["away_team"])] = (
                        sc[ev["home_team"]], sc[ev["away_team"]]
                    )
                except (KeyError, ValueError, TypeError):
                    continue
    n = 0
    for r in extra:
        goals = found.get((r["home"], r["away"]))
        if goals:
            db.settle_match(r["home"], r["away"], r["kickoff_utc"], goals[0], goals[1])
            n += 1
    logging.info("all-leagues settlement: %s matches settled", n)
    return n


async def settle_job(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        if config.ODDS_API_KEY and await _settle_extras():
            for slip, legs, status in db.finished_unnotified_slips():
                try:
                    await ctx.bot.send_message(
                        slip["chat_id"], "Result update\n\n" + _H["format_slip"](slip, legs, status)
                    )
                except Exception:
                    logging.exception("could not notify slip %s", slip["id"])
                db.mark_notified(slip["id"])
    except Exception:
        logging.exception("all-leagues settle job failed")


def register(app, send_long, format_slip, est_note: str, disclaimer: str):
    """Adds /all. Nothing here changes the existing handlers."""
    _H.update(send_long=send_long, format_slip=format_slip, est_note=est_note, disclaimer=disclaimer)
    if not config.ODDS_API_KEY:
        logging.info("/all disabled: it needs ODDS_API_KEY")
        return
    app.add_handler(CommandHandler("all", cmd_all))
    app.add_handler(CallbackQueryHandler(on_cb, pattern=r"^all:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text), group=-1)
    app.job_queue.run_repeating(
        settle_job, interval=config.SETTLE_EVERY_HOURS * 3600, first=3600
    )
