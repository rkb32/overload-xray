-- One row per analyzed run (a folder of spans), plus one row per caller -> callee edge inside it.
-- Durations are nanoseconds in bigint: a week of nanoseconds still fits comfortably in 64 bits.
CREATE TABLE IF NOT EXISTS runs (
    id             integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name           text        NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    user_requests  integer     NOT NULL CHECK (user_requests >= 0),
    calls          integer     NOT NULL CHECK (calls >= 0),
    work_ns        bigint      NOT NULL CHECK (work_ns >= 0),
    used_ns        bigint      NOT NULL,
    zombie_ns      bigint      NOT NULL,
    tail_ns        bigint      NOT NULL,
    -- the numbers must make sense together: used and zombie work are parts of the total, the tail is part of zombie
    CONSTRAINT runs_used_within_work   CHECK (used_ns   BETWEEN 0 AND work_ns),
    CONSTRAINT runs_zombie_within_work CHECK (zombie_ns BETWEEN 0 AND work_ns),
    CONSTRAINT runs_tail_within_zombie CHECK (tail_ns   BETWEEN 0 AND zombie_ns)
);

CREATE TABLE IF NOT EXISTS edges (
    id         integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id     integer          NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    caller     text             NOT NULL,
    callee     text             NOT NULL,
    calls      integer          NOT NULL CHECK (calls >= 0),
    fanout     double precision NOT NULL,
    reach      double precision NOT NULL,
    work_ns    bigint           NOT NULL CHECK (work_ns >= 0),
    used_ns    bigint           NOT NULL,
    zombie_ns  bigint           NOT NULL,
    tail_ns    bigint           NOT NULL,
    CONSTRAINT edges_used_within_work   CHECK (used_ns   BETWEEN 0 AND work_ns),
    CONSTRAINT edges_zombie_within_work CHECK (zombie_ns BETWEEN 0 AND work_ns),
    CONSTRAINT edges_tail_within_zombie CHECK (tail_ns   BETWEEN 0 AND zombie_ns),
    -- one row per edge per run; the unique index also serves "all edges of one run" lookups
    UNIQUE (run_id, caller, callee)
);

-- the dashboard lists runs newest first
CREATE INDEX IF NOT EXISTS runs_created_at_idx ON runs (created_at DESC);
