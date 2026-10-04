"""Persist analyzed runs in Postgres."""
import os

import psycopg
from psycopg.rows import dict_row

from xray.analyze import Report

SCHEMA_SQL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")

# goodput is derived on read, never stored, so it can never disagree with the columns it comes from
_GOODPUT = "CASE WHEN work_ns = 0 THEN 1.0 ELSE used_ns::float8 / work_ns END AS goodput"
_RUN_COLUMNS = f"id, name, created_at, user_requests, calls, work_ns, used_ns, zombie_ns, tail_ns, {_GOODPUT}"
_EDGE_COLUMNS = f"caller, callee, calls, fanout, reach, work_ns, used_ns, zombie_ns, tail_ns, {_GOODPUT}"


def connect(url: str | None = None, timeout_s: int = 5) -> psycopg.Connection:
    # connect_timeout makes an unreachable database fail in seconds instead of hanging a request for a minute
    # (a "localhost" that resolves to ::1 first, with nothing listening there, did exactly that).
    return psycopg.connect(url or os.environ["DATABASE_URL"], row_factory=dict_row, connect_timeout=timeout_s)


def init_schema(conn: psycopg.Connection) -> None:
    with open(SCHEMA_SQL, encoding="utf-8") as handle:
        conn.execute(handle.read())
    conn.commit()


def save_run(conn: psycopg.Connection, name: str, report: Report) -> int:
    """Store one analyzed run and all of its edges in ONE transaction: either both land or neither does."""
    with conn.transaction():
        run_id = conn.execute(
            "INSERT INTO runs (name, user_requests, calls, work_ns, used_ns, zombie_ns, tail_ns) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                name,
                report.user_requests,
                report.dependency_calls,
                report.total_work_ns,
                report.used_work_ns,
                report.zombie_work_ns,
                report.tail_ns,
            ),
        ).fetchone()["id"]
        with conn.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO edges (run_id, caller, callee, calls, fanout, reach, work_ns, used_ns, zombie_ns, tail_ns) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    (run_id, e.caller, e.callee, e.calls, e.fanout, e.reach, e.work_ns, e.used_ns, e.zombie_ns, e.tail_ns)
                    for e in report.edges
                ],
            )
    return run_id


def list_runs(conn: psycopg.Connection, limit: int = 50) -> list[dict]:
    return conn.execute(
        f"SELECT {_RUN_COLUMNS} FROM runs ORDER BY created_at DESC, id DESC LIMIT %s", (limit,)
    ).fetchall()


def get_run(conn: psycopg.Connection, run_id: int) -> dict | None:
    run = conn.execute(f"SELECT {_RUN_COLUMNS} FROM runs WHERE id = %s", (run_id,)).fetchone()
    if run is None:
        return None
    run["edges"] = conn.execute(
        f"SELECT {_EDGE_COLUMNS} FROM edges WHERE run_id = %s ORDER BY caller, callee", (run_id,)
    ).fetchall()
    return run
