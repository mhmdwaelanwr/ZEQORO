"""CI integration smoke test against ephemeral PostgreSQL and Redis containers.

Execute separately from pytest; never points at a user's production database.
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['APP_ENV'] = 'development'
os.environ['DATABASE_URL'] = 'postgresql+psycopg://zeqoro:ci_test_password@localhost:5432/zeqoro_test'
os.environ['REDIS_URL'] = 'redis://localhost:6379/0'
os.environ['APP_SECRET'] = 'ci-only-app-secret-longer-than-32-bytes'

from fastapi.testclient import TestClient
from app import SessionLocal, SchedulingLock, app, write_serialize, limiter

with TestClient(app) as client:
    assert client.get('/health').status_code == 200
    assert client.get('/ready').json() == {'ready': True}
    assert client.get('/').status_code == 200
    # Real Redis Lua: enforce an actual atomic cross-request rate limit.
    limiter.clear('ci-pg', 'integration-test')
    assert limiter.check('ci-pg', 'integration-test', 2, 20)
    assert limiter.check('ci-pg', 'integration-test', 2, 20)
    assert not limiter.check('ci-pg', 'integration-test', 2, 20)
    # Real PostgreSQL: SELECT FOR UPDATE on singleton row blocks a second session.
    started = threading.Event()
    acquired = threading.Event()
    failed = []
    def contender():
        try:
            with SessionLocal() as second:
                started.set()
                write_serialize(second)
                acquired.set()
                second.commit()
        except Exception as exc:
            failed.append(str(exc))
    with SessionLocal() as first:
        assert first.get(SchedulingLock, 1)
        write_serialize(first)
        thread = threading.Thread(target=contender, daemon=True)
        thread.start()
        assert started.wait(timeout=4)
        assert not acquired.wait(timeout=0.4), 'Postgres lock should block contender'
        first.commit()
    thread.join(timeout=5)
    assert not failed, failed
    assert acquired.is_set(), 'Postgres lock was never released'
print('PostgreSQL locking and Redis rate-limiter integration checks passed.')
