-- Schema for the taskboard application: one project record, the team members
-- tasks are assigned to, the tasks themselves and the comments on them.
--
-- This file carries its own drops, so every caller that executes it -- the
-- init-db command, the start-up check that builds a missing schema and the
-- seed script -- starts from an empty schema rather than a stale one.
-- Executing the whole file twice in a row must therefore succeed and leave
-- the same four tables and four indexes behind.

-- Dropped child first, so these statements work even with foreign keys on.
DROP TABLE IF EXISTS comment;
DROP TABLE IF EXISTS task;
DROP TABLE IF EXISTS member;
DROP TABLE IF EXISTS project;

-- The single project record. CHECK (id = 1) makes "one project" a rule of the
-- schema rather than a convention: a second row is rejected outright. Dates are
-- ISO-8601 text (YYYY-MM-DD); start_date <= end_date is enforced by
-- app/validation.py, which owns the message the reviewer sees.
CREATE TABLE project (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    start_date  TEXT NOT NULL,
    end_date    TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Team members: the people a task is assigned to and a comment is attributed
-- to. email is UNIQUE because the sample data names an assignee or an author
-- by that column, so it is the one column that identifies a member uniquely.
CREATE TABLE member (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    role       TEXT NOT NULL,
    email      TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Tasks: the work itself. project_id and assignee_id are nullable, so a task
-- can exist before the project details are recorded and an unassigned task is
-- NULL rather than a placeholder member. status is a closed set of three
-- values, which is what lets the dashboard counts be exhaustive and mutually
-- exclusive. deadline is a nullable ISO-8601 date; character bounds and the
-- rule that a deadline cannot precede the project start date both live in
-- app/validation.py, not here, so the two layers cannot disagree on wording.
CREATE TABLE task (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER REFERENCES project(id),
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    assignee_id INTEGER REFERENCES member(id),
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'in_progress', 'completed')),
    deadline    TEXT CHECK (deadline IS NULL OR deadline GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Comments on a task, each written by a team member. ON DELETE CASCADE keeps
-- no comment alive after its task is gone.
CREATE TABLE comment (
    id         INTEGER PRIMARY KEY,
    task_id    INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    author_id  INTEGER NOT NULL REFERENCES member(id),
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One index per read path: the status filter and the dashboard aggregate, the
-- assignee filter, the deadline buckets, and a task's comment list.
CREATE INDEX idx_task_status ON task (status);
CREATE INDEX idx_task_assignee ON task (assignee_id);
CREATE INDEX idx_task_deadline ON task (deadline);
CREATE INDEX idx_comment_task ON comment (task_id);
