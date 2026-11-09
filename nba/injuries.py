"""ESPN injury list → current state + a log of every status change. First run seeds silently."""
from datetime import datetime, timezone

from . import db

AVAILABLE = "available"


def state(conn) -> dict:
    return {r["player_id"]: dict(r) for r in conn.execute("select * from injury_state")}


def snapshot(conn, rows: list[dict], ts: str | None = None) -> list[dict]:
    ts = ts or datetime.now(timezone.utc).isoformat(timespec="seconds")
    seeded = db.meta_get(conn, "injuries_seeded") == "1"
    old = state(conn)
    new = {r["player_id"]: r for r in rows}
    changes = []
    for pid, r in new.items():
        prev = old.get(pid)
        if prev is None or prev["status"] != r["status"]:
            changes.append({"ts": ts, "player_id": pid, "name": r["name"], "team": r["team"],
                            "old": prev["status"] if prev else AVAILABLE, "new": r["status"], "detail": r["detail"]})
    for pid, prev in old.items():
        if pid not in new:
            changes.append({"ts": ts, "player_id": pid, "name": prev["name"], "team": prev["team"],
                            "old": prev["status"], "new": AVAILABLE, "detail": ""})
    with db.tx(conn):
        conn.execute("delete from injury_state")
        for pid, r in new.items():
            db.upsert(conn, "injury_state", {"player_id": pid, "name": r["name"], "team": r["team"],
                                             "status": r["status"], "detail": r["detail"], "ts": ts})
        if seeded:
            for c in changes:
                db.upsert(conn, "injury_change", c)
    if not seeded:
        db.meta_set(conn, "injuries_seeded", "1")
        return []
    return changes


def pending_changes(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("select * from injury_change where alerted=0 order by id")]


def mark(conn, ids, value):
    with db.LOCK:
        conn.executemany("update injury_change set alerted=? where id=?", [(value, i) for i in ids])


def recent_changes(conn, since_ts: str) -> list[dict]:
    return [dict(r) for r in conn.execute("select * from injury_change where ts >= ? order by id", (since_ts,))]
