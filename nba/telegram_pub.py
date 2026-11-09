"""Telegram output in the fleet card style (HTML, escaped values, spoilers for results)."""
import html
import logging
import re

import httpx

log = logging.getLogger(__name__)
LIMIT = 4000
DIVIDER = "━━━━━━━━━━━━━━━━"
SECTION_TITLES = {
    "digest": "🏀", "watch": "📺", "results": "📊", "aar": "🔍", "impact": "⚡", "status": "🩹", "movers": "📈",
    "title": "🏆", "verdict": "🧠", "tonight": "🔮", "record": "🎯", "weekly": "📅", "alert": "🚨", "warn": "⚠️",
    "fail": "❌", "team": "🏀", "compare": "⚔️", "scorecard": "🎯", "player": "👤", "ask": "💬", "game": "🏀",
    "film": "🎬", "plays": "📋", "wrap": "🏁", "news": "📰",
}
DROP_ORDER = ("movers", "record", "impact", "status", "news")      # matchup keys never drop: they split into a 2nd message      # dropped in this order when a digest is too long


def e(v) -> str:
    return html.escape(str(v), quote=False)


def title(kind, text, sub=None):
    return f"{SECTION_TITLES[kind]} <b>{e(text)}</b>" + (f" · {e(sub)}" if sub else "")


class Spoiler:
    def __init__(self, on: bool):
        self.on = on

    def __call__(self, s: str) -> str:          # s is already-escaped HTML
        return f"<tg-spoiler>{s}</tg-spoiler>" if self.on and s else s


def stars(n: int) -> str:
    n = max(1, min(5, int(n)))
    return "★" * n + "☆" * (5 - n)


def pct(p) -> str:
    return "–" if p is None else f"{100 * p:.0f}%"


# ---------- digest blocks ----------

def worth_watching(rows: list[dict]) -> str:
    """rows: [{label: 'PHI @ CHI', stars: int, reasons: [codes]}] ONLY. No scores, winners or margins reach here."""
    lines = [title("watch", "Worth watching")]
    for r in rows:
        why = ", ".join(r["reasons"]) or "routine game"
        lines.append(f"{stars(r['stars'])} <b>{e(r['label'])}</b> · <i>{e(why)}</i>")
    return "\n".join(lines)


def results(games: list[dict], sp: Spoiler, short=False) -> str:
    lines = [title("results", "Results")]
    for g in games:
        mark = {True: "🟢", False: "🔴", None: "⚪"}[g.get("pick_right")]
        head = f"{mark} <b>{e(g['matchup'])}</b> " + sp(e(g["score"]) + (f" ({g['ot']}OT)" if g.get("ot") else ""))
        lines.append(head)
        if not short and g.get("stats_line"):
            lines.append("   " + sp(e(g["stats_line"])))
        if g.get("top_line"):
            lines.append("   👤 " + sp(e(g["top_line"])))
        if g.get("won_line"):
            lines.append("   ⚖️ " + sp(e(g["won_line"])))
    return "\n".join(lines)


def aar_block(items: list[dict], sp: Spoiler, tuning_line: str | None) -> str:
    lines = [title("aar", "After Action Review", "missed picks")]
    for a in items:
        main = a["main"]
        pts = a["causes"].get(main, 0)
        lines.append(f"🔴 <b>{e(a['pick'])}</b> {pct(a['pick_wp'])} · " + sp(
            e(f"lost by {abs(a['actual_margin'])}" if a["actual_margin"] < 0 else f"won by {a['actual_margin']} (off by {abs(a['error'])})")
            + f" · main cause: <i>{e(main)}</i> ({pts:+.1f})"
            + (f" · {e(', '.join(a['scratches'][:2]))} out late" if a.get("scratches") and main == "availability" else "")))
        if a.get("note"):
            lines.append("   " + sp(f"<i>{e(a['note'])}</i>"))
    if tuning_line:
        lines.append(f"🛠 {e(tuning_line)}")
    return "\n".join(lines)


def impact_block(rows: list[dict], sp: Spoiler) -> str:
    lines = [title("impact", "Player impact", "win probability added")]
    for r in rows:
        best = ", ".join(f"{e(n)} {w:+.0%}" for n, _, w in r["best"][:1])
        worst = ", ".join(f"{e(n)} {w:+.0%}" for n, _, w in r["worst"][:1])
        lines.append(f"<b>{e(r['label'])}</b> " + sp(f"🟢 {best}" + (f"  🔴 {worst}" if worst else "")))
    return "\n".join(lines)


def status_block(changes: list[dict]) -> str:
    lines = [title("status", "Player status")]
    for c in changes:
        good = c["new"] in ("available", "probable")
        lines.append(f"{'🟢' if good else '🔴'} <b>{e(c['name'])}</b> ({e(c['team'])}) · {e(c['old'])} → {e(c['new'])}"
                     + (f" · <i>{e(c['detail'])}</i>" if c.get("detail") else ""))
    return "\n".join(lines)


def movers_block(movers: list[tuple], sp: Spoiler) -> str:
    lines = [title("movers", "Net rating movers")]
    for team, d, now in movers:
        lines.append(f"{'🟢' if d > 0 else '🔴'} <b>{e(team)}</b> " + sp(f"{'▲' if d > 0 else '▼'}{abs(d):.1f} → {now:+.1f}"))
    return "\n".join(lines)


def title_block(rows: list[dict], sp: Spoiler) -> str:
    lines = [title("title", "Title race", "model · market")]
    for r in rows:
        d = r.get("delta")
        if d is None or abs(d) < 0.005:
            mv = " ⚪"
        else:
            mv = " " + sp(f"🟢 ▲{100 * d:.0f}" if d > 0 else f"🔴 ▼{100 * -d:.0f}")
        lines.append(f"<b>{e(r['team'])}</b> {pct(r['model'])} · {pct(r.get('market'))}{mv}")
    return "\n".join(lines)


def verdict_block(lines_: list[str], sp: Spoiler) -> str:
    return "\n".join([title("verdict", "Claude verdict")] + [sp(e(x)) for x in lines_])


def tonight_block(preds: list[dict]) -> str:
    lines = [title("tonight", "Tonight's picks")]
    for p in preds:
        fav, wp = (p["home"], p["home_wp"]) if p["home_wp"] >= 0.5 else (p["away"], 1 - p["home_wp"])
        mk = f" · mkt {pct(p['market_wp'] if fav == p['home'] else 1 - p['market_wp'])}" if p.get("market_wp") is not None else ""
        lines.append(f"<b>{e(p['away'])} @ {e(p['home'])}</b> · {e(fav)} {pct(wp)}{mk} · <i>{e(p.get('tip_sgt', ''))}</i>")
        if p.get("flags"):
            lines.append(f"   ⚠️ <i>{e(p['flags'])}</i>")
    return "\n".join(lines)


def news_block(items: list[dict], title_text="News that matters", sub=None) -> str:
    lines = [title("news", title_text, sub)]
    for n in items:
        mark = "🔴" if n.get("status_implied") in ("out", "doubtful") or n["kind"] in ("suspension",) else "🟢" if n["kind"] == "return" else "⚪"
        who = ", ".join(n.get("players") or []) or ", ".join(n.get("teams") or [])
        tag = []
        if n.get("tonight"):
            tag.append("tonight")
        if n.get("impact"):
            tag.append(f"worth {n['impact']:.1f} pts")
        if n.get("ahead"):
            tag.append("ahead of the injury report")
        link = f' · <a href="{e(n["url"])}">{e(n["source"])}</a>' if n.get("url") else f" · {e(n['source'])}"
        lines.append(f"{mark} <b>{e(who)}</b> ({e('/'.join(n.get('teams') or []))}) · {e(n['kind'])}" + (f" · <i>{e(', '.join(tag))}</i>" if tag else ""))
        lines.append(f"   {e(n.get('summary') or n.get('title'))}{link}")
    return "\n".join(lines) if len(lines) > 1 else ""


def matchup_block(preds: list[dict], weak: dict) -> str:
    """Tonight's games: the opponent's top measured weakness and the play that attacks it, for each side."""
    lines = [title("tonight", "Matchup keys", "how each side can win")]
    for p in preds:
        a, h = p["away"], p["home"]
        rows = []
        for att, dfn in ((a, h), (h, a)):
            w = (weak.get(dfn) or [None])[0]
            if w:
                plays = ", ".join(w.get("plays", [])[:2])
                rows.append(f"   🎯 <b>{e(att)}</b>: {e(w['exploit'].lower())}" + (f" ({e(plays)})" if plays else "")
                            + f" · <i>{e(dfn)} {e(w['area'].lower())}</i>")
        if rows:
            lines.append(f"<b>{e(a)} @ {e(h)}</b>")
            lines += rows
    return "\n".join(lines) if len(lines) > 1 else ""


def record_block(sc: dict) -> str:
    if not sc.get("n"):
        return title("record", "Record", "no graded picks yet")
    return title("record", "Record", f"{sc['wins']}-{sc['n'] - sc['wins']} ({100 * sc['acc']:.0f}%) · Brier {sc['brier']:.3f}")


def fit(blocks: list[tuple[str, str]], header: str, limit=LIMIT) -> list[str]:
    """blocks: [(key, html)]. Drop optional sections by DROP_ORDER until it fits; then split between blocks."""
    def joined(bs):
        return header + "\n\n" + f"\n{DIVIDER}\n".join(h for _, h in bs)
    bs = [b for b in blocks if b[1]]
    for key in DROP_ORDER:
        if len(joined(bs)) <= limit:
            break
        bs = [b for b in bs if b[0] != key]
    if len(joined(bs)) <= limit:
        return [joined(bs)]
    msgs, cur = [], header
    for _, h in bs:
        for part in _split_block(h, limit - 50):
            if len(cur) + len(part) + len(DIVIDER) + 2 > limit:
                msgs.append(cur)
                cur = part
            else:
                cur = cur + ("\n\n" if cur == header else f"\n{DIVIDER}\n") + part
    msgs.append(cur)
    return msgs


def _split_block(h: str, limit: int) -> list[str]:
    """A single block longer than the limit is split between lines (each line is self-contained HTML)."""
    if len(h) <= limit:
        return [h]
    out, cur = [], ""
    for line in h.split("\n"):
        if len(cur) + len(line) + 1 > limit:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    return out + [cur]


# ---------- sending ----------

class Telegram:
    def __init__(self, token, chat_id, thread_id=None, http=None):
        self.token, self.chat, self.thread = token, chat_id, thread_id
        self.http = http or httpx.Client(timeout=30)

    def _post(self, method, **data):
        if self.thread:
            data["message_thread_id"] = self.thread
        return self.http.post(f"https://api.telegram.org/bot{self.token}/{method}", data=data)

    def send(self, text: str, buttons: list[tuple[str, str]] | None = None) -> int | None:
        payload = {"chat_id": self.chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}
        if buttons:
            import json
            payload["reply_markup"] = json.dumps({"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in buttons]]})
        r = self._post("sendMessage", **payload)
        if r.status_code == 400 and "parse" in r.text.lower():
            log.warning("Telegram rejected HTML, resending as plain text: %s", r.text[:200])
            payload.pop("parse_mode")
            payload["text"] = plain(text)
            r = self._post("sendMessage", **payload)
        if r.status_code != 200:
            log.error("Telegram send failed %s: %s", r.status_code, r.text[:300])
            return None
        return r.json()["result"]["message_id"]

    def send_many(self, msgs: list[str], buttons=None) -> list:
        return [self.send(m, buttons if i == len(msgs) - 1 else None) for i, m in enumerate(msgs)]

    def send_photo(self, path, caption_html: str) -> int | None:
        data = {"chat_id": self.chat, "caption": caption_html, "parse_mode": "HTML"}
        if self.thread:
            data["message_thread_id"] = self.thread
        with open(path, "rb") as f:
            r = self.http.post(f"https://api.telegram.org/bot{self.token}/sendPhoto", data=data, files={"photo": f})
        return r.json()["result"]["message_id"] if r.status_code == 200 else None


def plain(h: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", h))


class NullTelegram:
    """Used when no token is configured (and in tests): records what would be sent."""
    def __init__(self):
        self.sent = []

    def send(self, text, buttons=None):
        self.sent.append(text)
        return len(self.sent)

    def send_many(self, msgs, buttons=None):
        return [self.send(m) for m in msgs]

    def send_photo(self, path, caption_html):
        self.sent.append(f"[photo] {caption_html}")
        return len(self.sent)
