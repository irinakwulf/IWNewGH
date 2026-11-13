# Taskboard

A local task board for a single project: create tasks, hand them to team members, move
them through three statuses, give them deadlines, discuss them in comments, keep the
project's own details, and read four dashboard counts. It is built as a test project, so
a sample dataset, deliberate empty states and an automated suite are part of the
deliverable rather than extras.

## What this is

A server-rendered Flask application backed by one SQLite file. Every interaction is a
link or an HTML form post; no page ships a script and there is no build step. One Python
process serves the whole product, and a fresh checkout opens on the documented sample
board once the environment, install and seed commands below have run.

The capabilities are the ones the project was asked for, and each has one control and one
place it is tested:

- **Create a task** with a title, an optional description, an assignee, a status and a
  deadline, at `/tasks/new`.
- **Assign a task to a team member**, or clear the assignment, from the task detail page
  or the create and edit forms. An unassigned task reads "Unassigned" everywhere.
- **Update a task's status** through the closed set `pending`, `in_progress` and
  `completed`, from a task list row or the detail page. Every transition is allowed in
  both directions, so a completed task can be reopened.
- **Set a deadline**, optional and date-only, from the create and edit forms. A deadline
  earlier than the project start date is rejected.
- **Add a comment** to a task, attributed to a team member chosen on the form; the
  conversation reads oldest first, with each author and timestamp.
- **Keep the project details** — name, description, start date and end date — on one
  record, viewable and editable at `/project`.
- **Read four dashboard counts** — total, completed, pending and in progress — where each
  card links into the task list filtered to the rows behind it.
- **Filter the task list** by status, by assignee (including unassigned) and by deadline,
  with the three applied together and an explicit Apply control.

The screens are the dashboard at `/`, the task list at `/tasks`, the create and edit forms,
the task detail page, and the project screen at `/project`.

## Requirements

- **Python 3.12.** `pyproject.toml` declares `requires-python = ">=3.12"`; the pinned set
  was resolved against, and the suite runs on, CPython 3.12.3.
- **Flask 3.1.3 and its runtime closure**, pinned exactly in `requirements.txt`:

  | Package | Version | Role |
  |---------|---------|------|
  | Flask | 3.1.3 | Application factory, routing, request handling, Jinja integration, CLI, flash messages |
  | Werkzeug | 3.1.9 | WSGI toolkit and the development server Flask runs on |
  | Jinja2 | 3.1.6 | Template engine, with autoescaping on for every `.html` template |
  | itsdangerous | 2.2.0 | Signs the session cookie that carries flash messages |
  | MarkupSafe | 3.0.4 | The escaping machinery behind Jinja's autoescaping |
  | click | 8.5.0 | Command-line framework behind the `init-db` and `seed-db` commands |
  | blinker | 1.9.0 | Signal support Flask imports at start-up |

- **SQLite 3.45.1** through the standard library's `sqlite3` module. There is no database
  server process and nothing to install for it.
- **Test-time packages**, pinned exactly in `requirements-dev.txt`, which pulls
  `requirements.txt` in by reference: pytest 9.1.1, pytest-cov 7.1.0, coverage 7.17.0,
  iniconfig 2.3.1, packaging 26.3, pluggy 1.7.0 and Pygments 2.21.0.

Nothing else is installed. There is no ORM, no form framework, no CSS framework, no
bundler and nothing to compile for the browser, and the application calls no external
service.

## Install

From the repository root, the five commands of the whole workflow — create the
environment, install, load the sample data, serve the application and run the suite:

```bash
python3.12 -m venv .venv
./.venv/bin/python -m pip install -r requirements-dev.txt
./.venv/bin/python -m flask --app app seed-db
./.venv/bin/python -m flask --app app run --host 127.0.0.1 --port 5000
./.venv/bin/python -m pytest
```

The fourth command serves the application until it is stopped, so the fifth is run from a
second shell while the server is up. The sections below repeat the one command each of
them needs.

Two interpreter notes. `python3.12` names the pinned 3.12 interpreter; on a host where
`python3` is already 3.12, the same two commands work with `python3` in its place. Every
command is spelled `python -m ...`, which runs the virtual environment's interpreter
directly and needs no console scripts on `PATH`.

If the interpreter's `ensurepip` ships without its bundled wheel, `python3 -m venv .venv`
fails at its ensurepip step and leaves `.venv/bin/` holding only `python`, `python3` and
`python3.12`. Install into that environment with the system pip instead:

```bash
python3 -m pip install --root-user-action=ignore --target .venv/lib/python3.12/site-packages -r requirements-dev.txt
```

A `--target` install writes its console scripts into `<target>/bin`, which is not on
`PATH`; that is why every command is run as `python -m`. Where `ensurepip` is intact, the
ordinary `./.venv/bin/python -m pip install -r requirements-dev.txt` form above works
and is shorter.

If no virtual environment can be created at all, the equivalent is the system interpreter
with its own package directory:

```bash
python3 -m pip install --break-system-packages --root-user-action=ignore -r requirements-dev.txt
python3 -m flask --app app run --host 127.0.0.1 --port 5000
```

That form is needed on an image whose Python is marked externally managed, where a plain
`pip install` is refused.

Two environment variables are read, and both have working defaults, so neither is needed
to start:

| Variable | Default | Purpose |
|----------|---------|---------|
| `DATABASE` | `instance/taskboard.sqlite3` | The SQLite file the application creates and reads. A relative value is resolved against the repository root; an absolute one is used as given. |
| `SECRET_KEY` | A development-only fallback declared in `app/config.py` | Signs the session cookie that carries flash messages. Set a real value only where the fallback matters; the shipped one is visibly fake on purpose. |

No `.env` file is shipped and none is needed: both names are read from the environment at
configuration time, and the application reads no other variable.

## Seed the sample data

```bash
./.venv/bin/python -m flask --app app seed-db
```

The command prints exactly one line:

```text
Seeded 1 project, 5 members, 12 tasks, 8 comments.
```

It is idempotent: the four tables are dropped, `app/schema.sql` is re-applied and the
dataset is inserted, so re-running it restores the documented board instead of duplicating
rows. Deadlines are day offsets resolved against the date the command runs, which is what
keeps the overdue, due-today and due-this-week filters populated whenever it is run. There
is no separate step for a schema change, so a change to `app/schema.sql` is followed by
this command.

The same function, `seed_database` in `seed.py`, is called by the test fixtures, so the
board the application starts on and the rows the tests assert against are the same rows.

## Run

```bash
./.venv/bin/python -m flask --app app run --host 127.0.0.1 --port 5000
```

The application is then at <http://127.0.0.1:5000/>. The development server binds the
loopback address, so nothing is served on the network.

The schema is applied automatically at start-up when the `task` table is missing, so a
fresh checkout serves the dashboard, the task list and the project screen — with zero
counts and their empty states — before anyone runs `seed-db`. `flask --app app init-db`
drops the tables and recreates the schema, which resets the board to empty.

On a seeded board the dashboard reads Total 12, Completed 3, Pending 5 and In progress 4.

## Test

```bash
./.venv/bin/python -m pytest
```

Run from the repository root, the command exercises every capability through the
application's own HTTP surface, and reaches past it only where a direct call proves what
no request can:
`tests/test_validation.py` drives the parsers directly, with a `MultiDict` and no client;
`tests/test_seed.py` opens its own `sqlite3` connection and builds no application;
one test in `tests/test_comments.py` calls the comment service directly, because no
route can reach the branch it proves; and `tests/test_config.py` drives `app/config.py`
and the `create_app` factory directly, again with no client. Every test gets a temporary
SQLite database of its own, built from pytest's `tmp_path`, so no test reads or writes
`instance/taskboard.sqlite3`.

Coverage is measured over `app/` with branch measurement — `--cov=app --cov-branch` comes
from `addopts` in `pyproject.toml` — and the run fails below a hard floor of
`fail_under = 85` declared in `[tool.coverage.report]` of the same file. A change that
drops the package under the floor therefore fails the run rather than being reported
afterwards.

| Test module | What it proves |
|-------------|----------------|
| `tests/conftest.py` | Supplies the fixtures every module uses: `app` (an application on a temporary database), `client` (a test client for an unseeded application) and `seeded_client` (a test client whose database holds the sample dataset). |
| `tests/test_dashboard.py` | The four cards report the counts the rows actually hold, the three status buckets are exhaustive and mutually exclusive, a create or a status change moves exactly the cards it should, and the zero state renders its own words. |
| `tests/test_tasks.py` | The list, the create and edit forms, the detail page and the two quick actions: a write is checked in the page the reviewer lands on and again in the database, a rejection writes nothing, and an unknown id is a 404. |
| `tests/test_comments.py` | The seeded conversation, adding a comment with its author and timestamp, every rejection (blank body, over-long body, missing author) and the 404 for a comment on a task that does not exist. |
| `tests/test_filters.py` | Each filter returns exactly its rows, the three combine with `AND`, the Overdue bucket excludes finished work, and a value outside a control's allowed set is a 400 rather than a silently ignored filter. It also drives the four Jinja filters of `app/templating.py` directly, including the absent, blank, unparseable and object-valued inputs no page can hand them. |
| `tests/test_project.py` | The seeded details render, a valid post updates them, an end date before the start date is rejected with nothing written, and the first save creates the single project row. |
| `tests/test_validation.py` | The parsing layer directly, one case per rule, with no request and no template: typed values and messages asserted as whole mappings and exact strings. |
| `tests/test_seed.py` | The loader is idempotent and the dataset still covers all three statuses, the unassigned case and the no-deadline case. The `init-db` and `seed-db` commands are driven through Flask's own runner, which proves the record `seed-db` writes for the event, the summary line it prints, and the error a missing `seed.py` produces. |
| `tests/test_config.py` | `app/config.py` directly, with no request and no client: the five keys the default mapping holds, the two environment variables it reads, an empty one falling back to its default, the mapping a caller passes in outranking the environment, and a database path resolved against the repository root, so the same value names the same file whatever directory the process runs in. It also covers three behaviours of `create_app`: the start-up record naming the event at the level the documented run command leaves in place, the resolved database path kept to a DEBUG record a caller can ask for, and a `DATABASE` the process cannot use arriving as one line that names the setting, the resolved path and the recovery command, the file left as it was. |

`addopts` already applies coverage, so a single module keeps the gate. While iterating on
one area, skip it for speed:

```bash
./.venv/bin/python -m pytest tests/test_tasks.py -q --no-cov
```

## Routes

There is no JSON API. The application's whole external contract is twelve endpoints that
accept HTML form posts and return HTML pages, and requests are
`application/x-www-form-urlencoded`. Every field arrives as a string and is parsed and
typed in `app/validation.py`. The twelve contract endpoints below are served by
twelve method-and-path shapes over eleven URL rules, one method per rule: `/tasks`
and `/tasks/<id>` each carry a read rule and a write rule beside each other, while
the two form paths `/tasks/new` and `/tasks/<id>/edit` are reads only, so a post to
either is a method mismatch rather than a write path.

| Method and path | Input | Response |
|-----------------|-------|----------|
| `GET /` | none | 200 dashboard: the four metric cards, the create control, and the project panel. With no tasks the cards read 0 and the task area renders its own empty state. |
| `GET /tasks` | query `status`, `assignee`, `deadline`, each optional | 200 task list with the rows the filters select and a result line; 400 when a filter value is outside its control's allowed set. |
| `GET /tasks/new` | none | 200 create form with empty fields and Status set to Pending. |
| `POST /tasks` | form `title`, `description`, `assignee_id`, `status`, `deadline` | 302 to the new task's detail page with the flash "Task created."; invalid input re-renders the form at 200 with inline messages and writes nothing. |
| `GET /tasks/<id>` | none | 200 detail page with the task, its status and assign controls and its comments; 404 for an unknown id. |
| `GET /tasks/<id>/edit` | none | 200 edit form prefilled with the stored values; 404 for an unknown id. |
| `POST /tasks/<id>` | form `title`, `description`, `assignee_id`, `status`, `deadline` | 302 to the detail page with "Task updated."; invalid input re-renders the form at 200 and writes nothing; 404 for an unknown id. |
| `POST /tasks/<id>/status` | form `status`, `origin`, plus the three filter values when `origin=list` | 302 to the detail page, or back to `/tasks` with the same filters applied, with a flash naming the new status; 400 for a value outside the status set; 404 for an unknown id. |
| `POST /tasks/<id>/assign` | form `assignee_id` (empty clears the assignment), `origin`, plus the filter values when `origin=list` | 302 with "Assignee updated." or "Task unassigned."; 400 for an id that is not a member; 404 for an unknown id. |
| `POST /tasks/<id>/comments` | form `author_id`, `body` | 302 to the detail page with "Comment added."; invalid input re-renders the detail page at 200 with inline messages and writes nothing; 404 for an unknown id. |
| `GET /project` | none | 200 project screen with the stored details and the edit form, or empty fields while no project record exists. |
| `POST /project` | form `name`, `description`, `start_date`, `end_date` | 302 to `/project` with "Project details saved."; invalid input, an end date before the start date included, re-renders the form at 200 and writes nothing. |

How the responses behave:

- **Every successful write answers 302.** The redirect is followed by a GET, so a refresh
  re-reads the page and cannot resubmit the write.
- **A rejected data-entry post re-renders its own form at 200.** The submitted values stay
  in the fields, a message sits beside each offending field, and no row is written, so one
  field is corrected rather than the whole form retyped. This covers the task create and
  edit posts, the comment post and the project post.
- **A value outside a closed set answers 400, naming the parameter.** That covers the
  three filters and the two quick actions, whose values the interface only ever offers from
  a fixed list, so a value outside it means the request is malformed rather than a field to
  correct. The page reads, for example, `status: Invalid value for filter 'status'.`
- **An unknown task id answers 404**, decided before the posted body is inspected, so an id
  that does not exist is a 404 whatever the payload says.
- **An unexpected failure answers 500** with the application's own page rather than a
  traceback.
- **The two quick actions carry a navigation hint.** They post `origin` as `list` or
  `detail`; when it is `list` they also carry the three filter values as `filter_status`,
  `filter_assignee` and `filter_deadline`. Those are re-validated with the same parser the
  task list uses, before the write, and the redirect is rebuilt from the validated values.
  Any other origin, a missing one included, redirects to the task's detail page. No
  endpoint accepts a `next` or any other redirect target from the request.
- **The create and edit forms post to the paths above**, `POST /tasks` and
  `POST /tasks/<id>`, which are the only write paths for them. `/tasks/new` and
  `/tasks/<id>/edit` are reads that render the form; neither accepts a post.

A successful write flashes one message, rendered under the header with `role="status"`;
a rejected one puts its message beside the field instead, so the two outcomes are never
confused.

## Sample data

`seed.py` holds the dataset and the loader for it: one project, five team members, twelve
tasks and eight comments. Loaded with `flask --app app seed-db`, it produces a dashboard
reading **Total 12, Completed 3, Pending 5 and In progress 4**.

The project is "Website Redesign", described as a marketing site refresh with a new design
system and CMS migration, starting 14 days before seeding and ending 45 days after. The
team is Amina Haddad (Product Owner), Daniel Rivera (Frontend Engineer), Priya Nair
(Backend Engineer), Marcus Bell (Designer) and Sofia Lindqvist (QA Engineer). Deadlines
below are relative to the day the seed command runs:

| # | Task | Assignee | Status | Deadline |
|---|------|----------|--------|----------|
| 1 | Draft homepage wireframes | Marcus Bell | Completed | 9 days ago |
| 2 | Set up CI for the site build | Priya Nair | Completed | 5 days ago |
| 3 | Audit the current content inventory | Amina Haddad | Completed | none |
| 4 | Implement the new hero component | Daniel Rivera | In progress | today |
| 5 | Migrate blog posts to the new CMS | Priya Nair | In progress | in 3 days |
| 6 | Review accessibility of the navigation | Sofia Lindqvist | In progress | in 6 days |
| 7 | Write the deployment runbook | Priya Nair | In progress | none |
| 8 | Collect stakeholder feedback on the copy | Amina Haddad | Pending | 2 days ago |
| 9 | Build the pricing page | Daniel Rivera | Pending | yesterday |
| 10 | Define the redirect map | Priya Nair | Pending | in 4 days |
| 11 | Prepare the launch checklist | Sofia Lindqvist | Pending | none |
| 12 | Optimise the hero images | *(unassigned)* | Pending | in 10 days |

Each row is there to prove a rule, and the board can be read against these claims:

- **Two tasks are overdue**: "Collect stakeholder feedback on the copy" and "Build the
  pricing page". They are the only rows the Overdue filter returns.
- **One task is due today**: "Implement the new hero component".
- **Four tasks fall inside the coming week**: today's task plus "Migrate blog posts to the
  new CMS", "Review accessibility of the navigation" and "Define the redirect map".
- **Three tasks carry no deadline**: "Audit the current content inventory", "Write the
  deployment runbook" and "Prepare the launch checklist". They are the only rows the No
  deadline filter returns.
- **One task is unassigned**: "Optimise the hero images", which is also the only row due
  beyond the coming week, so its deadline shows as a plain date with no relative word.
- **Two completed tasks have deadlines that have passed**: "Draft homepage wireframes" and
  "Set up CI for the site build". They must not appear under the Overdue filter — finished
  work is not late, and the pair is the proof that the filter excludes it.

The four dashboard numbers must reconcile, because `task.status` is closed by the schema's
`CHECK (status IN ('pending', 'in_progress', 'completed'))`: the three status buckets are
exhaustive and mutually exclusive, so **Total always equals Pending + In progress +
Completed**. The counts are derived on every request from one grouped aggregate plus one
`COUNT(*)`, and nothing is stored, so creating a task, assigning one or changing a status
cannot leave a stale figure on screen.

The eight comments are spread across six tasks, two on "Implement the new hero component"
and two on "Collect stakeholder feedback on the copy", with varied authors and distinct
timestamps. That gives the comment list, its oldest-first ordering and the Comments column
real content, and their timestamps are distinct by construction so the order never depends
on insertion timing.

`seed.py` sits at the repository root rather than inside `app/` because the `seed-db`
command and `tests/test_seed.py` both import it as a top-level module, which keeps the
dataset independent of the application's own wiring.

## Interface conventions

Templates read named values and preformatted text, never raw rows and never a rule of
their own, so these conventions hold across every screen:

- **Dates** are stored as ISO-8601 `YYYY-MM-DD` text and shown as `26 Oct 2026`, with no
  leading zero on the day, so the fifth of January reads `5 Jan 2026`.
- **Timestamps** are stored as UTC ISO-8601 text written by SQLite and shown as
  `26 Oct 2026, 14:03`: 24-hour clock, seconds dropped, and the stored wall-clock value
  displayed as written, with no time-zone conversion.
- **Statuses** are `pending`, `in_progress` and `completed` in the database and the query
  string, and `Pending`, `In progress` and `Completed` on screen as a coloured badge whose
  wording carries the meaning on its own. The set is closed, every transition is allowed
  in both directions, and each change leaves the dashboard correct because the counts are
  recomputed per request.
- **A deadline** always shows its absolute date. Unfinished work adds one relative word —
  `Overdue`, `Due today` or `Due in N days` — and a completed task adds none, because
  finished work is not late. `Due in N days` covers the same seven-day window as the Due
  this week filter, today through six days ahead, so N runs from 1 to 6 and keeps the
  plural at one day (`Due in 1 days`), which is the wording the interface specifies. The
  word beside a deadline and the filter can therefore never disagree about which work is
  imminent, and the date, the middle dot between them and the word are produced together
  by `app/templating.py`'s `format_deadline`, which both the list and the detail page
  render, so one deadline reads identically on both screens. A stored value no date can
  be read from — reachable only by editing or restoring the database by hand, because
  the form refuses it — is shown as it is stored, and a task with no deadline reads
  "No deadline", so the column never renders a blank cell.
- **Filters** are three, applied together with `AND` in one `WHERE` clause with bound
  parameters and carried in the query string, so a filtered list can be linked and
  survives a row's status change:

  | Filter | Control values | Matches on |
  |--------|----------------|------------|
  | Status | All, Pending, In progress, Completed | `task.status` equals the chosen value |
  | Assignee | All, Unassigned, each member by name | `task.assignee_id` equals the member's id, or `IS NULL` for Unassigned |
  | Deadline | All, Overdue, Due today, Due this week, No deadline | `deadline < today AND status <> 'completed'`; `deadline = today`; `today <= deadline <= today + 6 days`; `deadline IS NULL` |

  `today` is evaluated once per request. A task due today matches both Due today and Due
  this week, because the control offers alternative buckets rather than additive
  categories, and the Overdue bucket and the Overdue indicator both exclude completed
  tasks.
- **Empty states say which situation they are in.** A board with no tasks reads "No tasks
  yet." with a single create control; a filter that matched nothing reads "No tasks match
  these filters." with a Clear filters link; the project panel reads "No project details
  yet." independently of the task area; a task with no comments reads "No comments yet."
- **Feedback matches the outcome.** A successful write flashes one message under the header
  as a status announcement, and a rejected one puts its message beside the field with the
  field marked invalid, so the two are never mistaken for each other. The 400, 404 and 500
  pages keep the page shell, state what happened and link back.
- **Accessibility is structural.** Every control has a visible `<label for>`, an inline
  error is linked to its field with `aria-describedby` and `aria-invalid`, the current
  navigation link carries `aria-current="page"`, and the task table has a caption and
  column headers.
- **One stylesheet, one token set.** `app/static/css/app.css` declares every colour,
  spacing, type, radius and elevation value as a custom property on `:root` and consumes it
  through `var()`, with a single 1080 px content column. Below its one breakpoint of 640 px
  the metric cards stack, the three filter selects take a line each and the task table keeps
  scrolling inside its wrapper. No page ships a script, and no select submits itself when
  its value changes, so the Apply button is always the control.

## Manual UI checklist

Start the application on a freshly seeded board and work through the list; it takes about
five minutes and exercises every control the project was asked to make work.

1. Open `/`. The four cards read Total tasks 12, Completed 3, Pending 5 and In progress 4.
2. Click Completed. The list reads "3 of 12 tasks match these filters" and shows the three
   completed rows.
3. From the list open "Build the pricing page". It is Pending with yesterday's deadline,
   shown as Overdue. Choose a team member in the Assignee select and press Assign: the
   assignee line updates and "Assignee updated." appears under the header.
4. Press Back to tasks, set that row's Status to Completed and press Update. The list
   returns with "Status updated to Completed." and the row's badge reads Completed.
5. Set the Deadline filter to Overdue and press Apply filters. Only "Collect stakeholder
   feedback on the copy" is listed: the task just completed does not appear even though its
   deadline has passed.
6. Press Clear filters, open "Collect stakeholder feedback on the copy", press Edit task,
   set its Deadline to tomorrow and save. The detail page shows tomorrow's date with
   "Due in 1 days".
7. Back on the list, set the Deadline filter to Overdue and press Apply filters: no task
   matches, and the list offers "No tasks match these filters." with a Clear filters link.
   Pressing it brings all twelve tasks back.
8. Open `/`. Total is still 12, Completed has risen to 4 and Pending has fallen to 4.
9. Open the task completed in step 4 and add a comment: choose an Author, type a body and
   press Add comment. It appears last, with the author's name and the timestamp, and the
   Comments count for that row on the list has risen by one.
10. Open `/project`, change the description and press Save project details: "Project details
    saved." appears and the dashboard's project panel shows the new text.
11. Submit the task form with an empty title, then the comment form with no author. Each is
    rejected in place, with the message beside the field, the other values still in the form
    and nothing written.
12. Visit `/tasks/9999` and see the 404 page. Visit `/tasks?status=unexpected` and see the
    400 page, naming `status` as the parameter it rejected.
13. Narrow the window to 375 px. The four metric cards stack, the three filter selects take
    a line each, and the task table scrolls sideways inside its wrapper while the page itself
    does not scroll sideways.

## Project layout

```text
/
├── app/                        # The Flask application package
│   ├── __init__.py             # create_app(): configuration, instance folder, schema bootstrap,
│   │                           #   blueprint registration, CLI commands, 400/404/500 handlers
│   ├── config.py               # Default configuration, the DATABASE and SECRET_KEY lookups, path resolution
│   ├── db.py                   # One connection per request, teardown, schema application, init-db and seed-db
│   ├── schema.sql              # DDL for project, member, task and comment, plus four indexes
│   ├── models.py               # Project, Member, Task and Comment dataclasses; TASK_STATUSES and STATUS_LABELS
│   ├── validation.py           # Form and filter parsing; every rule and every message
│   ├── templating.py           # Jinja filters: format_date, format_datetime, status_label, deadline_state;
│   │                           # the format_deadline global that renders a whole deadline indicator;
│   │                           # the app_path global the shell and the error pages build their links with
│   ├── services/               # Business rules: no HTTP, no request object, and their own
│   │                           #   parameterized statements through app/db.get_db()
│   │   ├── projects.py         #   get_project, save_project
│   │   ├── members.py          #   list_members, get_member
│   │   ├── tasks.py            #   list_tasks, get_task, create_task, update_task, set_status, set_assignee
│   │   ├── comments.py         #   list_comments, add_comment
│   │   └── metrics.py          #   dashboard_metrics, total_task_count
│   ├── blueprints/             # The blueprints behind the twelve contract endpoints:
│   │                           #   parse, call one service, render or redirect
│   │   ├── dashboard.py        #   GET /
│   │   ├── tasks.py            #   the list, create, detail, edit, status, assign and comment routes
│   │   └── project.py          #   GET and POST /project
│   ├── templates/
│   │   ├── base.html           #   Page shell: header, navigation, flash region, content block
│   │   ├── dashboard.html      #   Metric grid and project panel
│   │   ├── project.html        #   Project details and the edit form
│   │   ├── tasks/              #   list.html, form.html, detail.html
│   │   ├── partials/           #   _metric_card, _filter_bar, _task_table, _flash, _field_errors
│   │   └── errors/             #   400.html, 404.html, 500.html
│   └── static/css/app.css      # Design tokens on :root and every component style
├── seed.py                     # SAMPLE_DATA and seed_database(), shared by the seed-db command and the tests
├── tests/                      # conftest.py and the eight test modules listed above
├── requirements.txt            # Runtime pins at exact versions
├── requirements-dev.txt        # Test pins, including requirements.txt by reference
├── pyproject.toml              # Project metadata, pytest options and the 85% coverage gate
├── README.md                   # This document
└── .gitignore                  # Ignores the environment, the database, the build and test output
```

`instance/` is created at run time by `create_app()` and holds the SQLite file; it is
never committed. `.gitignore` covers `.venv/`, `instance/`, the bytecode caches
(`__pycache__/` and `*.py[cod]`), `.coverage` and its siblings, `htmlcov/`,
`.pytest_cache/`, and the `build/`, `dist/` and `*.egg-info/` output that building or
installing this project leaves behind. The `instance/` entry is the one that
matters most: the database it holds is disposable and is rebuilt by
`flask --app app seed-db`.

## Security notes

The controls the delivered code needs are in place and hold across every layer:

- **SQL is always parameterized.** Every statement binds its values through `sqlite3`, so no
  submitted text — a task title included — can alter a query, and `app/db.py` owns every
  connection the application opens; the one other connection is `seed.py`'s own, to the file
  it is handed.
- **Jinja autoescaping is never bypassed.** Every template is `.html`, so autoescaping is on,
  and no template applies `|safe` or `Markup` to a value that came from a form. Task titles,
  descriptions and comment bodies therefore render as text.
- **The session cookie is `HttpOnly` and `SameSite=Lax`**, set in `app/config.py`. There is no
  credential in the cookie — only flash messages — and `Lax` keeps a cross-site form post
  from carrying it.
- **The application is served on `127.0.0.1`.** The run command binds the loopback address,
  so nothing is reachable from the network.
- **No route takes a redirect target from the request.** The two quick actions rebuild their
  redirect with `url_for` from values that have already been validated, and there is no
  `next` parameter anywhere in the contract, so the open-redirect pattern is absent rather
  than mitigated.

Two deliberate non-controls, recorded as decisions rather than left as omissions: there are
**no accounts, no sign-in and no authorization layer**, and therefore **no CSRF tokens**,
because this is a loopback-only test project that holds no credential to forge, and a session
exists only as the signed cookie that carries flash messages. Their absence shapes two parts
of the contract: a comment's author and a task's assignee are explicit form fields, both
validated against the member table rather than trusted from the request.
