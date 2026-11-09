import sqlite3
import pytest
from nba import db


def test_migrate_idempotent(conn):
    db.migrate(conn)
    db.migrate(conn)
    names = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
    assert {"games", "team_box", "player_box", "plays", "predictions", "job_runs", "injury_change"} <= names


def test_predictions_are_frozen(conn):
    conn.execute("insert into predictions(game_id, version, created_utc, home_wp, margin, total) values ('g',1,'t',0.6,3,220)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("update predictions set home_wp=0.1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("delete from predictions")


def test_job_runs_unique(conn):
    conn.execute("insert into job_runs(job, key, status) values ('daily','2026-11-09','running')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("insert into job_runs(job, key, status) values ('daily','2026-11-09','running')")
