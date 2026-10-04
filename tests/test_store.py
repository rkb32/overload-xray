"""These talk to a real Postgres. Without DATABASE_URL they are skipped:

    docker compose up -d db
    set DATABASE_URL=postgresql://postgres:xray@127.0.0.1:15432/xray

Use 127.0.0.1, not "localhost": the database port is published on IPv4 only, and "localhost" may try ::1 first.
"""
import os
import time
import uuid

import psycopg
import pytest

from xray import store
from xray.analyze import EdgeStats, Report

S = 1_000_000_000


@pytest.fixture
def conn():
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set")
    schema = f"xray_test_{uuid.uuid4().hex[:8]}"  # every test gets its own throwaway schema
    connection = store.connect(url)
    connection.execute(f"CREATE SCHEMA {schema}")
    connection.execute(f"SET search_path TO {schema}")
    connection.commit()
    store.init_schema(connection)
    yield connection
    connection.rollback()
    connection.execute(f"DROP SCHEMA {schema} CASCADE")
    connection.commit()
    connection.close()


def test_an_unreachable_database_fails_fast_instead_of_hanging():
    started = time.time()

    with pytest.raises(psycopg.OperationalError):
        store.connect("postgresql://postgres:x@127.0.0.1:1/xray", timeout_s=3)  # nothing listens on port 1

    assert time.time() - started < 10


def example_report(**edge_overrides) -> Report:
    edge = dict(caller="A", callee="B", calls=2, fanout=2.0, reach=2.0,
                work_ns=10 * S, used_ns=5 * S, zombie_ns=5 * S, tail_ns=4 * S)
    edge.update(edge_overrides)
    return Report(user_requests=1, edges=[EdgeStats(**edge)])


def test_a_saved_run_comes_back_with_its_edges_and_derived_goodput(conn):
    run_id = store.save_run(conn, "readme-example", example_report())

    run = store.get_run(conn, run_id)

    assert (run["name"], run["calls"], run["tail_ns"]) == ("readme-example", 2, 4 * S)
    assert run["goodput"] == 0.5
    assert [(e["caller"], e["callee"], e["goodput"]) for e in run["edges"]] == [("A", "B", 0.5)]


def test_runs_are_listed_newest_first(conn):
    first = store.save_run(conn, "first", example_report())
    second = store.save_run(conn, "second", example_report())

    assert [r["id"] for r in store.list_runs(conn)] == [second, first]


def test_a_run_without_work_has_full_goodput_instead_of_a_division_by_zero(conn):
    run_id = store.save_run(conn, "idle", Report(user_requests=0, edges=[]))

    assert store.get_run(conn, run_id)["goodput"] == 1.0


def test_a_missing_run_is_none(conn):
    assert store.get_run(conn, 424242) is None


def test_a_bad_edge_rolls_back_the_whole_run(conn):
    impossible = example_report(used_ns=11 * S)  # used work cannot exceed total work

    with pytest.raises(psycopg.errors.CheckViolation):
        store.save_run(conn, "broken", impossible)
    conn.rollback()

    assert store.list_runs(conn) == []  # no half-saved run left behind
