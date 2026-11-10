"""Thompson-sampling bandit over creative choices (thumbnail style, title pattern, category...).

Arms start at Beta(1,1). Agent 9 (analytics) updates alpha/beta from real performance.
With no data every arm is equally likely, so early videos explore all options.
"""
from __future__ import annotations

import sqlite3

import numpy as np

_rng = np.random.default_rng()


def ensure(conn: sqlite3.Connection, dimension: str, values: list[str]) -> None:
    conn.executemany("INSERT OR IGNORE INTO strategy_arms(dimension, value) VALUES (?,?)",
                     [(dimension, v) for v in values])


def choose(conn: sqlite3.Connection, dimension: str, values: list[str]) -> str:
    ensure(conn, dimension, values)
    rows = {r["value"]: r for r in conn.execute(
        "SELECT value, alpha, beta FROM strategy_arms WHERE dimension=?", (dimension,))}
    draws = {v: _rng.beta(rows[v]["alpha"], rows[v]["beta"]) for v in values}
    return max(draws, key=draws.get)


def update(conn: sqlite3.Connection, dimension: str, value: str, reward: float) -> None:
    """reward in [0,1] (e.g. performance percentile); treated as a fractional Bernoulli outcome."""
    reward = min(max(reward, 0.0), 1.0)
    conn.execute("UPDATE strategy_arms SET alpha=alpha+?, beta=beta+?, n_videos=n_videos+1, "
                 "updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE dimension=? AND value=?",
                 (reward, 1 - reward, dimension, value))
