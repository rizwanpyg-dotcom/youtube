import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("YT_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("YT_WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("YT_OUTPUT_DIR", str(tmp_path / "out"))
    from ytengine import config, db
    config.load()
    c = db.connect()
    db.migrate(c)
    yield c
    c.close()


@pytest.fixture()
def topic_video(conn):
    import json
    conn.execute("INSERT INTO topics(slug, fingerprint, title, category, evidence_json) VALUES "
                 "('t1','x','Test topic','bubble',?)", (json.dumps({"target_words": [10, 400]}),))
    conn.execute("INSERT INTO videos(topic_id, format) VALUES (1,'long')")
    return 1
