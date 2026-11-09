"""Telegram commands. Owner-only. Heavy work runs in a thread so the bot keeps answering during jobs."""
import asyncio
import json
import logging
from datetime import timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from . import aggregator as agg
from . import analyst
from . import db
from . import espn
from . import jobs
from . import predictions as P
from . import scorecard
from . import simulate
from . import telegram_pub as tp

log = logging.getLogger(__name__)
CHEAP = {"/game today", "/scorecard"}          # the only commands inline buttons may trigger
LIMITED = {"ask", "whatif"}


def allowed(ctx, update: Update) -> bool:
    u = update.effective_user
    return bool(u and u.id in ctx.s.allowed_ids)


def use_quota(ctx, kind) -> bool:
    day = ctx.now_sgt().date().isoformat()
    with db.LOCK:
        n = sum(r[0] for r in ctx.conn.execute("select n from usage where day=? and kind in ('ask','whatif')", (day,)))
        if n >= ctx.s.ask_limit:
            return False
        ctx.conn.execute("insert into usage(day, kind, n) values (?,?,1) on conflict(day, kind) do update set n=n+1", (day, kind))
    return True


def find_player(st, name):
    q = name.strip().lower()
    hits = [(pid, v) for pid, v in st["values"].items() if q in v["name"].lower()]
    return sorted(hits, key=lambda x: -x[1]["value"])[0] if hits else (None, None)


# ---------- command bodies (sync, run in a thread) ----------

def cmd_team(ctx, args):
    t = espn.team_code(" ".join(args))
    if not t:
        return "Usage: <code>/team BOS</code>"
    st = jobs.state(ctx)
    R, sim, mkt = st["ratings"][t], jobs.latest_sim(ctx), jobs.market_title(ctx)
    se = agg.team_season(ctx.conn, ctx.s.espn_season, st["ratings"]).get(t, {})
    v = _latest_team_verdict(ctx, t)
    nxt = ctx.conn.execute("select * from games where (home=? or away=?) and status='scheduled' order by tip_utc limit 5", (t, t)).fetchall()
    lines = [tp.title("team", espn.NAMES[t], se.get("record", "")), "",
             f"📈 Net {R['net']:+.1f} (off {R['off']:+.1f}, def {R['def']:+.1f}) · healthy {R['healthy_net']:+.1f}",
             f"   last 5 {se.get('roll5')} · last 10 {se.get('roll10')} · pyth {se.get('pyth_wins')} W",
             f"🏆 Title {tp.pct(sim.get(t, {}).get('title'))} · finals {tp.pct(sim.get(t, {}).get('finals'))} · market {tp.pct(mkt.get(t))}"]
    if se.get("luck_flags"):
        lines.append("🎲 <i>" + tp.e(", ".join(se["luck_flags"])) + "</i>")
    if v:
        lines += ["", f"🧠 {tp.e(v['verdict'])} <i>({tp.e(v.get('trend', ''))}, {tp.e(v.get('confidence', ''))})</i>"]
        if v.get("strengths"):
            lines.append("🟢 " + tp.e("; ".join(v["strengths"][:3])))
        if v.get("weaknesses"):
            lines.append("🔴 " + tp.e("; ".join(v["weaknesses"][:3])))
    out = [f"{tp.e(g['away'])} @ {tp.e(g['home'])} {jobs.tip_sgt(g['tip_utc'])}" for g in nxt]
    if out:
        lines += ["", "📅 Next: " + "  ·  ".join(out)]
    return "\n".join(lines)


def _latest_team_verdict(ctx, t):
    for r in ctx.conn.execute("select json from verdicts order by date desc limit 20"):
        for x in json.loads(r[0]).get("teams", []):
            if x.get("team") == t:
                return x
    return None


def cmd_compare(ctx, args):
    if len(args) < 2 or not espn.team_code(args[0]) or not espn.team_code(args[1]):
        return "Usage: <code>/compare BOS NY</code>"
    a, b = espn.team_code(args[0]), espn.team_code(args[1])
    st = jobs.state(ctx)
    R = st["ratings"]
    se = agg.team_season(ctx.conn, ctx.s.espn_season, R)
    p = st["params"]
    hi, lo = (a, b) if R[a]["healthy_net"] >= R[b]["healthy_net"] else (b, a)
    ps = simulate.series_prob(st["s_po"][hi], st["s_po"][lo], p.hca, p.sigma)

    def traits(t):
        vals = sorted((v["value"] for v in st["values"].values() if v["team"] == t), reverse=True)
        top7 = sum(vals[:7])
        return f"top-7 value {top7:.1f} · star share {vals[0] / top7:.0%}" if top7 else "no player data"
    lines = [tp.title("compare", f"{a} vs {b}"), ""]
    for t in (a, b):
        f = se.get(t, {}).get("four_factors", {})
        lines += [f"<b>{t}</b> {se.get(t, {}).get('record', '')} · net {R[t]['net']:+.1f} · healthy {R[t]['healthy_net']:+.1f}",
                  f"   eFG {f.get('efg', 0):.1%} · TOV {f.get('tov', 0):.1%} · ORB {f.get('orb', 0):.1%} · FTr {f.get('ftr', 0):.2f}",
                  f"   {traits(t)}"]
    lines += ["", f"⚔️ 7-game series ({hi} with home court): <b>{hi} {ps:.0%}</b> · {lo} {1 - ps:.0%}",
              "<i>Play-type matchup penalties arrive in Phase 2.</i>"]
    return "\n".join(lines)


def cmd_whatif(ctx, args):
    if len(args) < 2 or not espn.team_code(args[0]) or args[1][0] not in "+-":
        return "Usage: <code>/whatif BOS -Tatum</code> or <code>/whatif BOS +Tatum</code>"
    t = espn.team_code(args[0])
    st = jobs.state(ctx)
    pid, v = find_player(st, " ".join(args[1:])[1:])
    if not pid:
        return "Player not found in this season's box scores."
    if not use_quota(ctx, "whatif"):
        return f"Daily limit reached ({ctx.s.ask_limit} /ask + /whatif)."
    inj = dict(st["injuries"])
    if args[1][0] == "-":
        inj[pid] = {"status": "out", "team": v["team"], "name": v["name"], "detail": "season (what-if)"}
    else:
        inj.pop(pid, None)
    seed = 99
    base = jobs.run_sim(ctx, st, n=5000, seed=seed)
    alt = jobs.run_sim(ctx, st, n=5000, seed=seed, injuries_override=inj)
    d = alt[t]["title"] - base[t]["title"]
    return "\n".join([tp.title("compare", f"What if {v['name']} {'sits' if args[1][0] == '-' else 'plays'}", t), "",
                      f"🏆 Title {base[t]['title']:.1%} → {alt[t]['title']:.1%} {'🟢 ▲' if d > 0 else '🔴 ▼'}{abs(100 * d):.1f}",
                      f"🏁 Finals {base[t]['finals']:.1%} → {alt[t]['finals']:.1%}",
                      f"<i>Value {v['value']:.1f} pts/game · 5,000 runs, same seed</i>"])


def cmd_player(ctx, args):
    if not args:
        return "Usage: <code>/player Tatum</code>"
    st = jobs.state(ctx)
    pid, v = find_player(st, " ".join(args))
    if not pid:
        return "Player not found in this season's box scores."
    rows = ctx.conn.execute("""select p.*, g.date, g.home, g.away from player_box p join games g on g.game_id=p.game_id
                               where p.player_id=? and p.dnp=0 order by g.date desc""", (pid,)).fetchall()
    n = len(rows)
    avg = lambda k: sum(r[k] for r in rows) / max(1, n)
    fga, fgm, f3a, f3m, fta, ftm = (sum(r[k] for r in rows) for k in ("fga", "fgm", "fg3a", "fg3m", "fta", "ftm"))
    ts = sum(r["pts"] for r in rows) / max(1, 2 * (fga + 0.44 * fta))
    inj = st["injuries"].get(pid)
    lines = [tp.title("player", v["name"], v["team"]), "",
             f"{'🔴 ' + tp.e(inj['status']) + (' · ' + tp.e(inj['detail']) if inj.get('detail') else '') if inj else '🟢 available'}",
             f"📊 {n} games · {avg('minutes'):.1f} min · {avg('pts'):.1f} pts · {avg('reb'):.1f} reb · {avg('ast'):.1f} ast",
             f"   FG {fgm}-{fga} · 3P {f3m}-{f3a} · FT {ftm}-{fta} · TS {ts:.1%}",
             f"💰 Value {v['value']:.1f} pts/game of margin (box-score proxy)"]
    last5 = rows[:5]
    if last5:
        recent = sum(r["minutes"] for r in last5) / len(last5)
        lines.append(f"⏱ Last 5: {recent:.1f} min vs season {avg('minutes'):.1f}")
        for r in last5:
            w = ctx.conn.execute("select wpa from player_wpa where game_id=? and key=?", (r["game_id"], pid)).fetchone()
            opp = r["away"] if r["team"] == r["home"] else r["home"]
            lines.append(f"   {r['date'][5:]} vs {opp}: " + tp.Spoiler(ctx.s.spoiler)(
                tp.e(f"{r['pts']}p {r['reb']}r {r['ast']}a, {int(r['minutes'])} min") + (f" · WPA {w['wpa']:+.0%}" if w else "")))
    lines.append("<i>Scouting report, archetype and similar seasons arrive in Phase 2.</i>")
    return "\n".join(lines)


def cmd_status(ctx, args):
    t = espn.team_code(" ".join(args))
    if not t:
        return "Usage: <code>/status BOS</code>"
    st = jobs.state(ctx)
    lines = [tp.title("status", f"{t} roster status"), ""]
    q = """select p.player_id, p.name, count(*) gp, avg(p.minutes) mpg, sum(p.starter) gs from player_box p
           join games g on g.game_id=p.game_id where p.team=? and g.season=? and p.dnp=0
           group by p.player_id order by sum(p.minutes) desc limit 15"""
    for r in ctx.conn.execute(q, (t, ctx.s.espn_season)):
        last5 = ctx.conn.execute("""select avg(minutes) from (select p.minutes from player_box p join games g on g.game_id=p.game_id
                                    where p.player_id=? order by g.date desc limit 5)""", (r["player_id"],)).fetchone()[0] or 0
        inj = st["injuries"].get(r["player_id"])
        trend = "▲" if last5 > 1.2 * r["mpg"] else "▼" if last5 < 0.8 * r["mpg"] else ""
        lines.append(f"{'🔴' if inj else '🟢'} <b>{tp.e(r['name'])}</b> {r['mpg']:.0f} min{trend} · "
                     f"{'starter' if r['gs'] >= r['gp'] / 2 else 'bench'}" + (f" · <i>{tp.e(inj['status'])}</i>" if inj else ""))
    return "\n".join(lines)


def cmd_game(ctx, args):
    if not args or args[0].lower() == "today":
        today = ctx.now_sgt().date().isoformat()
        slate = jobs.slate_view(ctx, today)
        return tp.tonight_block(slate) if slate else "No games tonight."
    if len(args) < 2 or not espn.team_code(args[0]) or not espn.team_code(args[1]):
        return "Usage: <code>/game today</code> or <code>/game LAL BOS</code>"
    a, b = espn.team_code(args[0]), espn.team_code(args[1])
    g = ctx.conn.execute("""select * from games where status='final' and source is not null and
                            ((home=? and away=?) or (home=? and away=?)) order by date desc limit 1""", (a, b, b, a)).fetchone()
    if not g:
        return "No finished game between them this season."
    br = agg.game_brief(ctx.conn, g["game_id"])
    sp = tp.Spoiler(ctx.s.spoiler)
    top = br["top_players"][0] if br["top_players"] else None
    ex = br.get("excitement") or {}
    return "\n".join([tp.title("game", br["matchup"], g["date"]), "",
                      f"📺 {tp.stars(ex.get('stars', 1))} · <i>{tp.e(ex.get('reasons') or 'routine game')}</i>",
                      "📊 " + sp(tp.e(br["score"]) + (f" ({g['ot']}OT)" if g["ot"] else "")),
                      ("👤 " + sp(tp.e(f"{top['name']} {top['line']}"))) if top else ""])


def cmd_scorecard(ctx, args):
    sc = scorecard.grade(ctx.conn)
    if not sc.get("n"):
        return tp.title("scorecard", "Scorecard") + "\n\n<i>No graded picks yet.</i>"
    lines = [tp.title("scorecard", "Scorecard", f"{sc['wins']}-{sc['n'] - sc['wins']} ({sc['acc']:.0%})"), "",
             f"Brier {sc['brier']:.3f} · log loss {sc['logloss']:.3f} · margin error {sc['mae_margin']:.1f}"]
    if "elo_brier" in sc:
        lines.append(f"Elo alone: Brier {sc['elo_brier']:.3f} · {sc['elo_acc']:.0%}")
    if "mkt_brier" in sc:
        lines.append(f"Market ({sc['mkt_n']} games): Brier {sc['mkt_brier']:.3f} vs model {sc['model_brier_on_mkt']:.3f}")
    lines += ["", "<b>Calibration</b>"] + [f"{c['bucket']}: {c['n']} · said {c['predicted']:.0%}, hit {c['actual']:.0%}"
                                           for c in sc["calibration"]]
    if sc["most_missed"]:
        lines.append("Most missed: " + ", ".join(f"{t} {n}" for t, n in sc["most_missed"]))
    return "\n".join(lines)


def cmd_ask(ctx, args):
    q = " ".join(args).strip()
    if not q:
        return "Usage: <code>/ask who has the best defense?</code>"
    if not use_quota(ctx, "ask"):
        return f"Daily limit reached ({ctx.s.ask_limit} /ask + /whatif)."
    st = jobs.state(ctx)
    R = st["ratings"]
    se = agg.team_season(ctx.conn, ctx.s.espn_season, R)
    sim = jobs.latest_sim(ctx)
    data = {"date": st["asof"],
            "teams": {t: {"net": round(R[t]["net"], 1), "healthy_net": round(R[t]["healthy_net"], 1),
                          **{k: se.get(t, {}).get(k) for k in ("record", "roll10", "pyth_wins", "luck_flags", "four_factors")},
                          "title": round(sim.get(t, {}).get("title", 0), 3)} for t in R if not t.startswith("_")},
            "injuries": [f"{v['name']} {v['team']} {v['status']}" for v in st["injuries"].values()][:80],
            "tonight": jobs.slate_view(ctx, ctx.now_sgt().date().isoformat()),
            "scorecard": {k: scorecard.grade(ctx.conn).get(k) for k in ("n", "acc", "brier")}}
    ans = analyst.ask(q, agg.compact(data), ctx.s.model_ask, **({"runner": ctx.runner} if ctx.runner else {}))
    return tp.title("ask", "Answer") + "\n\n" + tp.e(ans.strip()[:3500])


def cmd_plays(ctx, args):
    return tp.title("plays", "Play types and schemes") + "\n\n<i>Phase 2: inferred offensive actions and defensive schemes " \
        "from play-by-play (NBA.com play-type data is blocked from the home network).</i>"


def cmd_film(ctx, args):
    return tp.title("film", "Film room") + "\n\n<i>Phase 3: reported sets and coverages from breakdown videos.</i>"


def cmd_help(ctx, args):
    return "\n".join([tp.title("digest", "NBA tracker", "commands"), "",
                      "<code>/team BOS</code> · <code>/compare BOS NY</code> · <code>/whatif BOS -Tatum</code>",
                      "<code>/player Tatum</code> · <code>/status BOS</code> · <code>/game today</code> · <code>/game LAL BOS</code>",
                      "<code>/scorecard</code> · <code>/ask …</code> · <code>/plays BOS</code> · <code>/film BOS</code>",
                      f"<i>/ask and /whatif: {ctx.s.ask_limit} a day combined.</i>"])


COMMANDS = {"team": cmd_team, "compare": cmd_compare, "whatif": cmd_whatif, "player": cmd_player, "status": cmd_status,
            "game": cmd_game, "scorecard": cmd_scorecard, "ask": cmd_ask, "plays": cmd_plays, "film": cmd_film,
            "help": cmd_help, "start": cmd_help}


def dispatch(ctx, name, args) -> str | None:
    """Pure entry point (tests call this). None = not a command we serve."""
    fn = COMMANDS.get(name)
    if not fn:
        return None
    try:
        return fn(ctx, args)
    except Exception as e:
        log.exception("command %s failed", name)
        return f"{tp.SECTION_TITLES['fail']} <code>{tp.e(type(e).__name__)}: {tp.e(str(e)[:200])}</code>"


async def _reply(msg, text):
    try:
        await msg.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except BadRequest:
        await msg.reply_text(tp.plain(text), disable_web_page_preview=True)


def build_app(ctx) -> Application:
    app = Application.builder().token(ctx.s.tg_token).build()

    async def on_command(update: Update, _: ContextTypes.DEFAULT_TYPE):
        if not allowed(ctx, update) or not update.effective_message or not update.effective_message.text:
            return
        parts = update.effective_message.text.split()
        name = parts[0][1:].split("@")[0].lower()
        text = await asyncio.to_thread(dispatch, ctx, name, parts[1:])
        if text:
            await _reply(update.effective_message, text)

    async def on_button(update: Update, _: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()                                    # always stop the spinner
        if not allowed(ctx, update) or q.data not in CHEAP or str(q.message.chat.id) != str(ctx.s.tg_chat_id):
            return
        parts = q.data.split()
        text = await asyncio.to_thread(dispatch, ctx, parts[0][1:], parts[1:])
        if text:
            await _reply(q.message, text)

    app.add_handler(CommandHandler(list(COMMANDS), on_command))
    app.add_handler(CallbackQueryHandler(on_button))
    return app
