"""Integration test against a real opencode TUI and database.

Opt-in (it writes to the real opencode database): ``OC_REAP_INTEGRATION=1``.

A throwaway session is imported from ``fixtures/session.json`` with fresh
ids (no LLM call), opened in a TUI inside a throwaway tmux server
(``-L <socket> -f /dev/null``, always killed), and deleted at the end.
Only the TUI started here is targeted (``--pid``), so real TUIs are never
touched.
"""

import io
import json
import os
import re
import secrets
import shutil
import string
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import oc_reap  # noqa: E402

ENABLED = os.environ.get("OC_REAP_INTEGRATION") == "1"
FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "session.json")
SOCKET = f"oc-reap-test-{os.getpid()}"
TITLE = "oc-reap fixture"
ID_RE = re.compile(r"\b(?:ses|msg|prt)_[A-Za-z0-9]+")
ID_ALPHABET = string.ascii_letters + string.digits


def fresh_fixture(path):
    """Write the fixture to ``path`` with every ses/msg/prt id renewed.

    :returns: the new session id.
    """
    with open(FIXTURE, encoding="utf-8") as fh:
        text = fh.read()
    mapping = {old: old[:4] + "".join(secrets.choice(ID_ALPHABET)
                                      for _ in range(len(old) - 4))
               for old in set(ID_RE.findall(text))}
    for old, new in mapping.items():
        text = text.replace(old, new)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return json.loads(text)["info"]["id"]


def wait_for(predicate, timeout, what):
    """Poll ``predicate`` until truthy; fail the test on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.5)
    raise AssertionError(f"timeout waiting for {what}")


@unittest.skipUnless(ENABLED, "set OC_REAP_INTEGRATION=1 to run")
class RealTuiTests(unittest.TestCase):
    """Close a real TUI, tag its session, list it, resume it."""

    @classmethod
    def setUpClass(cls):
        """Import a throwaway session (no LLM call involved)."""
        cls.opencode = oc_reap.find_opencode(None)
        cls.tmp = tempfile.mkdtemp(prefix="oc-reap-it-")
        # `opencode import` stamps the session with its working directory.
        cls.project = os.path.join(cls.tmp, "project")
        # The TUI runs from another directory so that the import process
        # never makes the run matching ambiguous.
        cls.tui_dir = os.path.join(cls.tmp, "tui")
        cls.state = os.path.join(cls.tmp, "state")
        for path in (cls.project, cls.tui_dir):
            os.makedirs(path)
        cls.addClassCleanup(shutil.rmtree, cls.tmp, ignore_errors=True)
        fixture = os.path.join(cls.tmp, "session.json")
        cls.session_id = fresh_fixture(fixture)
        subprocess.run([cls.opencode, "import", fixture], cwd=cls.project,
                       stdin=subprocess.DEVNULL, capture_output=True, check=True,
                       timeout=120)
        cls.addClassCleanup(subprocess.run, [cls.opencode, "session", "delete",
                                             cls.session_id],
                            capture_output=True, check=False, timeout=60)
        cls.db = oc_reap.OpencodeDb(cls.opencode)
        row = cls.db.session(cls.session_id)
        assert row and row["title"] == TITLE and row["directory"] == cls.project, row

    def tmux(self, *args):
        """Run tmux against the isolated server with a pinned environment."""
        env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        env["PATH"] = os.path.dirname(self.opencode) + ":/usr/bin:/bin"
        return subprocess.run(["tmux", "-L", SOCKET, "-f", "/dev/null", *args],
                              env=env, capture_output=True, text=True, check=False)

    def start_tui(self):
        """Start a TUI on the throwaway session; return its pid."""
        self.tmux("kill-server")
        self.tmux("new-session", "-d", "-s", "it", "-x", "200", "-y", "50",
                  "-c", self.tui_dir, f"{self.opencode} --session {self.session_id}")
        self.addCleanup(self.cleanup_tmux)

        def find():
            for t in oc_reap.find_tuis():
                if t.session_arg == self.session_id and t.cwd == self.tui_dir:
                    return t.pid
            return None
        pid = wait_for(find, 30, "the TUI process")
        # Wait for the "creating instance" log line of this TUI.
        wait_for(lambda: self.run_reap("--dry-run", pid=pid)[1].count("skip") == 0,
                 30, "the TUI instance in the log")
        return pid

    def cleanup_tmux(self):
        """Kill the isolated server and remove its socket."""
        self.tmux("kill-server")
        path = f"/tmp/tmux-{os.getuid()}/{SOCKET}"
        if os.path.exists(path):
            os.unlink(path)

    def run_reap(self, *extra, pid):
        """Run ``oc-reap reap --older-than 0s --pid PID``; return (rc, output)."""
        out = io.StringIO()
        rc = oc_reap.main(["reap", "--older-than", "0s", "--pid", str(pid),
                           "--state-dir", self.state, *extra], out=out)
        return rc, out.getvalue()

    def title(self):
        """Return the current title of the throwaway session."""
        return self.db.session(self.session_id)["title"]

    def test_close_tag_list_resume(self):
        """Dry-run changes nothing; reap closes, tags once, lists, resumes."""
        pid = self.start_tui()

        rc, output = self.run_reap("--dry-run", pid=pid)
        self.assertEqual(rc, 0, output)
        self.assertIn("close (dry-run)", output)
        self.assertIn("it:0.0", output)  # tmux target resolved
        os.kill(pid, 0)  # still alive
        self.assertEqual(self.title(), TITLE)

        rc, output = self.run_reap(pid=pid)
        self.assertEqual(rc, 0, output)
        self.assertIn("closed", output)
        wait_for(lambda: not os.path.exists(f"/proc/{pid}"), 5, "TUI exit")
        self.assertEqual(self.title(), oc_reap.WIP_PREFIX + TITLE)

        [record] = oc_reap.read_journal(self.state)
        self.assertEqual(record["session_id"], self.session_id)
        self.assertEqual(record["resume"],
                         f"cd {self.project} && opencode --session {self.session_id}")

        out = io.StringIO()
        oc_reap.main(["list", "--state-dir", self.state, "--json"], out=out)
        [row] = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(row["title"], oc_reap.WIP_PREFIX + TITLE)

        # Resume: the session reopens with its history, and a second reap
        # does not stack the prefix.
        pid = self.start_tui()
        info = self.db.session(self.session_id)
        self.assertIsNotNone(info)
        rc, _ = self.run_reap(pid=pid)
        self.assertEqual(rc, 0)
        self.assertEqual(self.title(), oc_reap.WIP_PREFIX + TITLE)
        messages = self.db.query("SELECT count(*) AS n FROM message WHERE "
                                 f"session_id = '{self.session_id}'")
        self.assertGreaterEqual(messages[0]["n"], 2)


if __name__ == "__main__":
    unittest.main()
