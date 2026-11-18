"""Unit tests for oc_reap (no real process, tmux or database involved)."""

import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import oc_reap  # noqa: E402

# Lines copied from a real opencode 1.19.3 log, ids anonymized.
LOG = """\
timestamp=2026-10-20T13:51:26.133Z level=INFO run=aaaa1111 message="creating instance" directory=/home/u/dev/proj
timestamp=2026-10-20T13:51:26.204Z level=INFO run=aaaa1111 message=loading path=/home/u/.config/opencode/opencode.json
timestamp=2026-10-20T14:00:00.000Z level=INFO run=aaaa1111 message=loop session.id=ses_parent1 step=1
timestamp=2026-10-20T15:00:00.500Z level=INFO run=aaaa1111 message=stream providerID=x modelID=y session.id=ses_child1 small=false
timestamp=2026-10-21T19:13:17.000Z level=INFO run=aaaa1111 message="watcher backend" directory=/home/u/dev/proj platform=linux
timestamp=2026-10-12T12:13:05.507Z level=INFO run=bbbb2222 message="creating instance" directory="/home/u/drive/My Docs"
timestamp=2026-10-12T12:20:00.000Z level=INFO run=cccc3333 message="creating instance" directory=/home/u/dev/proj
not a log line
"""


def epoch(text):
    """Parse an ISO UTC timestamp to epoch seconds (test helper)."""
    return oc_reap.parse_log_ts(f"timestamp={text}Z x")


def tui(pid=1, cwd="/home/u/dev/proj", start=None, session=None):
    """Build a Tui with sensible defaults (test helper)."""
    return oc_reap.Tui(pid, cwd, start if start is not None else epoch("2026-10-20T13:51:25.000"),
                       session, "work:0.1")


class FakeRunner:
    """Stand-in for subprocess.run backed by an in-memory session table."""

    def __init__(self, sessions):
        """Keep the rows and record every SQL statement."""
        self.sessions = {s["id"]: dict(s) for s in sessions}
        self.statements = []

    def __call__(self, argv, **kwargs):
        """Answer the few SQL shapes oc_reap emits."""
        sql = argv[-1]
        self.statements.append(sql)
        sid = sql.rsplit("'", 2)[-2]
        if sql.startswith("SELECT"):
            rows = [self.sessions[sid]] if sid in self.sessions else []
            return subprocess.CompletedProcess(argv, 0, json.dumps(rows), "")
        if sql.startswith("UPDATE"):
            self.sessions[sid]["title"] = oc_reap.WIP_PREFIX + self.sessions[sid]["title"]
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        return subprocess.CompletedProcess(argv, 1, "", "unsupported")


SESSIONS = [
    {"id": "ses_parent1", "title": "Migrate module", "parent_id": None,
     "directory": "/home/u/dev/proj", "time_updated": int(epoch("2026-10-20T15:00:01.000") * 1000)},
    {"id": "ses_child1", "title": "Explore", "parent_id": "ses_parent1",
     "directory": "/home/u/dev/proj", "time_updated": 0},
]


class ParseTests(unittest.TestCase):
    """Duration and log parsing."""

    def test_durations(self):
        """Every unit converts; garbage is rejected."""
        self.assertEqual(oc_reap.parse_duration("72h"), 259200)
        self.assertEqual(oc_reap.parse_duration("3d"), 259200)
        self.assertEqual(oc_reap.parse_duration("90m"), 5400)
        self.assertEqual(oc_reap.parse_duration("0s"), 0)
        for bad in ("", "72", "h", "-1h", "1.5h", "3w"):
            with self.assertRaises(oc_reap.ReapError):
                oc_reap.parse_duration(bad)

    def test_parse_log(self):
        """Instances (quoted dirs too) and last session activity per run."""
        instances, activity = oc_reap.parse_log(io.StringIO(LOG))
        self.assertEqual([(i.run, i.directory) for i in instances],
                         [("aaaa1111", "/home/u/dev/proj"),
                          ("bbbb2222", "/home/u/drive/My Docs"),
                          ("cccc3333", "/home/u/dev/proj")])
        self.assertEqual(activity, {"aaaa1111": (epoch("2026-10-20T15:00:00.500"), "ses_child1")})

    def test_maintenance_lines_are_not_activity(self):
        """A 'watcher backend' line a day later does not refresh activity."""
        _, activity = oc_reap.parse_log(io.StringIO(LOG))
        self.assertLess(activity["aaaa1111"][0], epoch("2026-10-21T00:00:00.000"))


class TuiDetectionTests(unittest.TestCase):
    """Command-line classification."""

    def test_tui_forms(self):
        """Bare, project path, and --session launches are TUIs."""
        self.assertEqual(oc_reap.tui_session_arg(["opencode"]), (True, None))
        self.assertEqual(oc_reap.tui_session_arg(["/x/bin/opencode", "/p"]), (True, None))
        self.assertEqual(oc_reap.tui_session_arg(
            ["/x/opencode", "--session", "ses_abc123"]), (True, "ses_abc123"))
        self.assertEqual(oc_reap.tui_session_arg(
            ["opencode", "--session=ses_abc123"]), (True, "ses_abc123"))

    def test_non_tui(self):
        """Subcommands and other programs are ignored; bad ids dropped."""
        for argv in (["opencode", "run", "hi"], ["opencode", "serve"],
                     ["opencode", "db", "SELECT 1"], ["notify-send", "opencode"],
                     ["python3", "opencode"], []):
            self.assertFalse(oc_reap.tui_session_arg(argv)[0], argv)
        self.assertEqual(oc_reap.tui_session_arg(["opencode", "-s", "'; DROP"]), (True, None))


class MatchRunTests(unittest.TestCase):
    """PID to log-run matching."""

    def setUp(self):
        """Parse the fixture log."""
        self.instances, _ = oc_reap.parse_log(io.StringIO(LOG))

    def test_unique_match_with_skew(self):
        """An 18 s skew (observed in the field) still matches."""
        run, problem = oc_reap.match_run(tui(start=epoch("2026-10-20T13:51:44.000")), self.instances)
        self.assertEqual((run, problem), ("aaaa1111", None))

    def test_no_match(self):
        """Wrong directory or far-away start: skipped, with a reason."""
        run, problem = oc_reap.match_run(tui(cwd="/elsewhere"), self.instances)
        self.assertIsNone(run)
        self.assertIn("no matching", problem)

    def test_ambiguous(self):
        """Two starts in the same directory inside the window: refused."""
        extra = self.instances + [oc_reap.Instance(epoch("2026-10-20T13:52:00.000"), "dddd4444",
                                                   "/home/u/dev/proj")]
        run, problem = oc_reap.match_run(tui(), extra)
        self.assertIsNone(run)
        self.assertIn("ambiguous", problem)


class PlanTests(unittest.TestCase):
    """Decisions and the [WIP] rename."""

    def setUp(self):
        """Parse the log and build a fake database."""
        self.instances, self.activity = oc_reap.parse_log(io.StringIO(LOG))
        self.runner = FakeRunner(SESSIONS)
        self.db = oc_reap.OpencodeDb("opencode", runner=self.runner)

    def test_idle_tui_is_closed_and_root_session_used(self):
        """Sub-agent activity resolves to the parent session."""
        now = epoch("2026-10-23T16:00:00.000")
        [decision] = oc_reap.plan([tui()], self.instances, self.activity, self.db,
                                  now, oc_reap.parse_duration("72h"))
        self.assertEqual(decision.action, "close")
        self.assertEqual(decision.session["id"], "ses_parent1")
        self.assertEqual(decision.last_activity, epoch("2026-10-20T15:00:01.000"))

    def test_recent_tui_is_kept(self):
        """71 h of idleness is below the 72 h threshold."""
        now = epoch("2026-10-23T14:00:00.000")
        [decision] = oc_reap.plan([tui()], self.instances, self.activity, self.db,
                                  now, oc_reap.parse_duration("72h"))
        self.assertEqual(decision.action, "keep")

    def test_unmatched_tui_is_skipped(self):
        """Never close a TUI that cannot be identified."""
        [decision] = oc_reap.plan([tui(cwd="/nowhere")], self.instances, self.activity,
                                  self.db, epoch("2027-01-01T00:00:00.000"), 0)
        self.assertEqual(decision.action, "skip")

    def test_session_arg_used_without_activity(self):
        """A --session TUI with no logged activity uses its argument."""
        [decision] = oc_reap.plan([tui(cwd="/home/u/drive/My Docs",
                                       start=epoch("2026-10-12T12:13:04.000"),
                                       session="ses_parent1")],
                                  self.instances, self.activity, self.db,
                                  epoch("2027-01-01T00:00:00.000"), 0)
        self.assertEqual(decision.session["id"], "ses_parent1")

    def test_mark_wip_is_idempotent(self):
        """The prefix is added once, and only the title changes."""
        self.assertEqual(self.db.mark_wip("ses_parent1"), "[WIP] Migrate module")
        self.assertEqual(self.db.mark_wip("ses_parent1"), "[WIP] Migrate module")
        updates = [s for s in self.runner.statements if s.startswith("UPDATE")]
        self.assertEqual(len(updates), 1)
        self.assertNotIn("time_updated", updates[0])

    def test_invalid_session_id_rejected(self):
        """Ids are validated before reaching SQL."""
        with self.assertRaises(oc_reap.ReapError):
            self.db.session("ses_x' OR '1'='1")

    def test_db_failure_raises(self):
        """A failing opencode db is a hard error, not an empty result."""
        db = oc_reap.OpencodeDb("opencode", runner=lambda argv, **kw:
                                subprocess.CompletedProcess(argv, 1, "", "boom"))
        with self.assertRaises(oc_reap.ReapError):
            db.session("ses_parent1")


class FakeApiRunner:
    """Stand-in for subprocess.run that applies v2 title updates to SQLite."""

    def __init__(self, path):
        """Keep the DB path and record every API call."""
        self.path = path
        self.calls = []

    def __call__(self, argv, **kwargs):
        """Apply ``api patch`` to the test database; accept other commands."""
        self.calls.append(argv)
        if len(argv) > 5 and argv[1:3] == ["api", "patch"]:
            session_id = argv[3].rsplit("/", 1)[-1]
            title = json.loads(argv[5])["title"]
            con = sqlite3.connect(self.path)
            con.execute("UPDATE session_v2 SET title = ? WHERE id = ?",
                        (title, session_id))
            con.commit()
            con.close()
        return subprocess.CompletedProcess(argv, 0, "", "")


def v2_store(tmp, rows):
    """Create a minimal ``session_v2`` database and a V2SessionStore."""
    path = os.path.join(tmp, "opencode.db")
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE session_v2 (
        id TEXT PRIMARY KEY, title TEXT, parent_id TEXT, directory TEXT,
        time_updated INTEGER, time_busy INTEGER, time_viewed INTEGER,
        time_idle INTEGER)""")
    for row in rows:
        con.execute(
            "INSERT INTO session_v2 VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (row["id"], row["title"], row.get("parent_id"), row["directory"],
             row.get("time_updated"), row.get("time_busy"),
             row.get("time_viewed"), row.get("time_idle")))
    con.commit()
    con.close()
    return oc_reap.V2SessionStore("opencode", runner=FakeApiRunner(path),
                                  db_path=path)


class V2PlanTests(unittest.TestCase):
    """OpenCode 2 session matching and activity."""

    def setUp(self):
        """Build a parent/child session pair with old activity."""
        self.tmp = tempfile.mkdtemp(prefix="oc-reap-v2-")
        self.addCleanup(self._cleanup)
        old = int(epoch("2026-10-20T15:00:01.000") * 1000)
        self.store = v2_store(self.tmp, [
            {"id": "ses_parent1", "title": "Migrate module", "directory": "/home/u/dev/proj",
             "time_updated": old},
            {"id": "ses_child1", "title": "Explore", "parent_id": "ses_parent1",
             "directory": "/home/u/dev/proj", "time_updated": 0},
        ])

    def _cleanup(self):
        """Remove the temporary database directory."""
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_session_arg_resolves_to_root_and_closes(self):
        """An explicit --session is enough to match the TUI."""
        tui = oc_reap.Tui(1, "/home/u/dev/proj",
                          epoch("2026-10-20T13:51:25.000"), "ses_child1", "work:0.1")
        [decision] = oc_reap.plan([tui], [], {}, self.store,
                                  epoch("2027-01-01T00:00:00.000"), 0)
        self.assertEqual(decision.action, "close")
        self.assertEqual(decision.session["id"], "ses_parent1")
        self.assertEqual(decision.last_activity, epoch("2026-10-20T15:00:01.000"))

    def test_no_mapping_is_skipped(self):
        """No pane option, no --session and no env session: skip."""
        tui = oc_reap.Tui(1, "/home/u/dev/proj",
                          epoch("2026-10-20T13:51:25.000"), None, "work:0.1")
        [decision] = oc_reap.plan([tui], [], {}, self.store,
                                  epoch("2027-01-01T00:00:00.000"), 0)
        self.assertEqual(decision.action, "skip")
        self.assertIn("no session mapping", decision.reason)

    def test_inherited_env_session_is_ignored(self):
        """An env session inherited from oc-reap's own parent is not trusted."""
        tui = oc_reap.Tui(1, "/home/u/dev/proj",
                          epoch("2026-10-20T13:51:25.000"), None, "work:0.1",
                          env_session="ses_parent1")
        with unittest.mock.patch.dict(os.environ, {"OPENCODE_SESSION_ID": "ses_parent1"}):
            [decision] = oc_reap.plan([tui], [], {}, self.store,
                                      epoch("2027-01-01T00:00:00.000"), 0)
        self.assertEqual(decision.action, "skip")

    def test_v2_mark_wip_uses_api_once(self):
        """The title is updated through the API and stays idempotent."""
        self.assertEqual(self.store.mark_wip("ses_parent1"), "[WIP] Migrate module")
        self.assertEqual(self.store.mark_wip("ses_parent1"), "[WIP] Migrate module")
        self.assertEqual(sum(1 for call in self.store.runner.calls
                             if call[1:3] == ["api", "patch"]), 1)


class JournalTests(unittest.TestCase):
    """Journal writing and the list command."""

    def test_record_and_list(self):
        """Closed sessions are listed while their title keeps [WIP]."""
        runner = FakeRunner(SESSIONS)
        db = oc_reap.OpencodeDb("opencode", runner=runner)
        decision = oc_reap.Decision(tui(), "close", "idle", "aaaa1111",
                                    db.session("ses_parent1"), 1792000000.0)
        with tempfile.TemporaryDirectory() as state:
            record = oc_reap.journal_record(decision, db.mark_wip("ses_parent1"), 1792100000.0)
            self.assertEqual(record["resume"],
                             "cd /home/u/dev/proj && opencode --session ses_parent1")
            oc_reap.append_journal(state, record)
            args = oc_reap.build_parser().parse_args(["list", "--state-dir", state])
            out = io.StringIO()
            oc_reap.cmd_list(args, out, db=db)
            self.assertIn("[WIP] Migrate module", out.getvalue())
            self.assertIn("opencode --session ses_parent1", out.getvalue())
            runner.sessions["ses_parent1"]["title"] = "Migrate module"  # user marks done
            out = io.StringIO()
            oc_reap.cmd_list(args, out, db=db)
            self.assertIn("no closed WIP session", out.getvalue())
            args = oc_reap.build_parser().parse_args(["list", "--all", "--state-dir", state])
            out = io.StringIO()
            oc_reap.cmd_list(args, out, db=db)
            self.assertIn("Migrate module", out.getvalue())

    def test_corrupt_journal(self):
        """A corrupt line fails loudly."""
        with tempfile.TemporaryDirectory() as state:
            with open(os.path.join(state, oc_reap.JOURNAL_NAME), "w") as fh:
                fh.write("{not json\n")
            with self.assertRaises(oc_reap.ReapError):
                oc_reap.read_journal(state)


class FormatTests(unittest.TestCase):
    """Small formatting helpers and the CLI surface."""

    def test_format_age(self):
        """Compact ages."""
        self.assertEqual(oc_reap.format_age(40 * 60), "40m")
        self.assertEqual(oc_reap.format_age(5 * 3600 + 12 * 60), "5h12m")
        self.assertEqual(oc_reap.format_age(3 * 86400 + 4 * 3600), "3d4h")

    def test_resume_command_quotes(self):
        """Directories with spaces are shell-quoted."""
        self.assertEqual(oc_reap.resume_command("/a b", "ses_x"),
                         "cd '/a b' && opencode --session ses_x")

    def test_cli_usage_errors(self):
        """Missing subcommand exits 2; a bad duration exits 1."""
        with self.assertRaises(SystemExit) as ctx:
            oc_reap.build_parser().parse_args([])
        self.assertEqual(ctx.exception.code, 2)
        err = io.StringIO()
        sys.stderr, saved = err, sys.stderr
        try:
            self.assertEqual(oc_reap.main(["reap", "--older-than", "soon"]), 1)
        finally:
            sys.stderr = saved
        self.assertIn("invalid duration", err.getvalue())


if __name__ == "__main__":
    unittest.main()
