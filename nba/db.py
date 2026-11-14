"""SQLite store. One shared connection (WAL) guarded by LOCK: the bot and the scheduler share it."""
import sqlite3
import threading
from contextlib import contextmanager

LOCK = threading.RLock()

SCHEMA = """
create table if not exists games(
  game_id text primary key, date text, season int, season_type int, home text, away text,
  tip_utc text, status text, home_pts int, away_pts int, ot int default 0, source text,
  neutral int default 0, note text);
create index if not exists games_date on games(date);

create table if not exists team_box(
  game_id text, team text, opp text, is_home int, pts int, fgm int, fga int, fg3m int, fg3a int,
  ftm int, fta int, oreb int, dreb int, ast int, stl int, blk int, tov int, pf int,
  paint int, fastbreak int, pts_off_to int, largest_lead int, lead_changes int,
  poss real, clean_poss real, clean_pts int, clean_opp_pts int,
  primary key(game_id, team));

create table if not exists player_box(
  game_id text, team text, player_id text, name text, position text, starter int, minutes real,
  pts int, reb int, oreb int, dreb int, ast int, stl int, blk int, tov int, pf int,
  fgm int, fga int, fg3m int, fg3a int, ftm int, fta int, plus_minus int, dnp int, reason text,
  primary key(game_id, player_id));
create index if not exists player_box_pid on player_box(player_id);

create table if not exists plays(
  game_id text, seq int, period int, clock_s real, type text, text text, team text, player_id text,
  home_score int, away_score int, home_wp real, scoring int, shooting int, x real, y real,
  primary key(game_id, seq));

create table if not exists game_excitement(game_id text primary key, score real, stars int, reasons text);
create table if not exists player_wpa(game_id text, key text, team text, wpa real, primary key(game_id, key));

create table if not exists injury_state(player_id text primary key, name text, team text, status text, detail text, ts text);
create table if not exists injury_change(
  id integer primary key autoincrement, ts text, player_id text, name text, team text,
  old text, new text, detail text, alerted int default 0);

create table if not exists ratings(
  date text, team text, off real, def real, net real, prior_net real, blend_w real, healthy_net real,
  games int, primary key(date, team));
create table if not exists elo(date text, team text, elo real, primary key(date, team));
create table if not exists sims(
  date text, kind text, team text, playoffs real, conf_finals real, finals real, title real,
  primary key(date, kind, team));

create table if not exists predictions(
  id integer primary key autoincrement, game_id text, version int, created_utc text,
  home_wp real, margin real, total real, elo_wp real, market_wp real, reason text,
  unique(game_id, version));
create trigger if not exists predictions_no_update before update on predictions
  begin select raise(abort, 'predictions are frozen'); end;
create trigger if not exists predictions_no_delete before delete on predictions
  begin select raise(abort, 'predictions are frozen'); end;

create table if not exists aar(game_id text primary key, date text, json text);
create table if not exists model_params(
  version integer primary key autoincrement, created_utc text, json text, ll_old real, ll_new real, reason text);
create trigger if not exists model_params_no_update before update on model_params
  begin select raise(abort, 'model params are append-only'); end;

create table if not exists film_items(
  id integer primary key autoincrement, game_id text, date text, team text, kind text, label text, who text,
  summary text, url text, title text, channel text, created text, unique(game_id, team, label, url));
create table if not exists film_videos(url text primary key, game_id text, status text, checked text);
create table if not exists play_videos(label text primary key, url text, title text, channel text, duration int, checked text);
create table if not exists film_games(game_id text primary key, status text, checked text);
create table if not exists game_breakdowns(game_id text, team text, hash text, created text, body text,
  primary key(game_id, team));

create table if not exists game_officials(game_id text, name text, primary key(game_id, name));
create table if not exists odds_snap(game_id text, ts text, home_ml_open real, home_ml real, away_ml_open real, away_ml real,
  spread_open real, spread real, total real, primary key(game_id, ts));

create table if not exists futures(date text, team text, american real, implied real, primary key(date, team));
create table if not exists verdicts(date text, kind text, json text, primary key(date, kind));
create table if not exists job_runs(
  job text, key text, status text, steps text default '', started text, finished text, error text,
  primary key(job, key));
create table if not exists usage(day text, kind text, n int, primary key(day, kind));
create table if not exists meta(k text primary key, v text);
"""


def connect(path) -> sqlite3.Connection:
    c = sqlite3.connect(str(path), check_same_thread=False, timeout=30, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("pragma journal_mode=wal")
    c.execute("pragma foreign_keys=on")
    return c


def migrate(c):
    with LOCK:
        c.executescript(SCHEMA)
        cols = {r[1] for r in c.execute("pragma table_info(film_items)")}
        if "ato" not in cols:
            c.execute("alter table film_items add column ato int default 0")
        for col in ("impact", "effect"):              # did the set work, and what it did to the game
            if col not in cols:
                c.execute(f"alter table film_items add column {col} text default ''")


@contextmanager
def tx(c):
    """Serialised write transaction: commit on success, roll back on error."""
    with LOCK:
        c.execute("begin immediate")
        try:
            yield c
        except BaseException:
            c.execute("rollback")
            raise
        c.execute("commit")


def upsert(c, table, row: dict):
    cols = ",".join(row)
    q = ",".join("?" * len(row))
    c.execute(f"insert or replace into {table}({cols}) values ({q})", list(row.values()))


def meta_get(c, k, default=None):
    with LOCK:
        r = c.execute("select v from meta where k=?", (k,)).fetchone()
    return r[0] if r else default


def meta_set(c, k, v):
    with LOCK:
        c.execute("insert or replace into meta(k, v) values (?, ?)", (k, str(v)))
