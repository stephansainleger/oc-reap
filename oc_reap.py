#!/usr/bin/env python3
"""Close idle opencode TUIs and tag their sessions as work in progress.

A long-lived opencode TUI keeps hundreds of MB of RAM, gigabytes of swap
and tens of thousands of inotify watches. ``oc-reap reap`` finds TUIs
whose session has seen no activity for longer than a threshold, stops
them with SIGTERM (sessions are kept, never deleted), and prefixes the
session title with ``[WIP] `` so unfinished work is easy to find again.
``oc-reap list`` shows the closed sessions and how to resume them.

Relationships: ``ai-audit session delete`` deletes sessions and
``ai-audit session nudge`` resumes them; this tool does neither. It
reads the same ``opencode.log`` that ``ai-audit permissions`` reads, and
writes only the ``title`` column through ``opencode db``.
"""

import argparse
import dataclasses
import datetime
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time

VERSION = "0.1.0"
WIP_PREFIX = "[WIP] "
DEFAULT_OLDER_THAN = "72h"
# Max gap between a TUI's computed start time and its "creating instance"
# log line. Observed skew reaches ~18 s on processes alive for days
# (wall clock adjustments); 120 s keeps a wide margin.
MATCH_WINDOW_S = 120
TERM_TIMEOUT_S = 15
EXIT_OK, EXIT_ERROR, EXIT_USAGE = 0, 1, 2

DEFAULT_LOG = os.path.expanduser("~/.local/share/opencode/log/opencode.log")
DEFAULT_STATE_DIR = os.path.join(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")),
    "oc-reap")
JOURNAL_NAME = "closed.jsonl"
LOCK_NAME = "lock"

# opencode subcommands that are NOT an interactive TUI.
NON_TUI_SUBCOMMANDS = frozenset({
    "completion", "acp", "mcp", "attach", "run", "debug", "providers",
    "auth", "agent", "upgrade", "uninstall", "serve", "web", "models",
    "stats", "export", "import", "github", "pr", "session", "plugin",
    "plug", "db"})
SESSION_ID_RE = re.compile(r"^ses_[A-Za-z0-9]+$")
LOG_TS_RE = re.compile(r"^timestamp=(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?)Z ")
LOG_RUN_RE = re.compile(r" run=(\S+) ")
LOG_SESSION_RE = re.compile(r" session\.id=(ses_[A-Za-z0-9]+)")
LOG_DIR_RE = re.compile(r' directory=(?:"([^"]*)"|(\S+))')
DURATION_RE = re.compile(r"^(\d+)([smhd])$")
DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


class ReapError(Exception):
    """A fatal, user-facing error (bad input, missing dependency)."""


@dataclasses.dataclass(frozen=True)
class Tui:
    """A running opencode TUI process.

    ``start`` is a wall-clock epoch (seconds); ``session_arg`` is the
    value of ``--session``/``-s`` if the TUI was launched with one.
    """

    pid: int
    cwd: str
    start: float
    session_arg: str | None
    tmux_target: str | None


@dataclasses.dataclass(frozen=True)
class Instance:
    """A ``creating instance`` log line: one opencode process start."""

    ts: float
    run: str
    directory: str


@dataclasses.dataclass(frozen=True)
class Decision:
    """What ``reap`` will do with one TUI, and why."""

    tui: Tui
    action: str  # "close", "keep" or "skip"
    reason: str
    run: str | None = None
    session: dict | None = None
    last_activity: float | None = None


def parse_duration(text):
    """Convert a duration such as ``72h``, ``3d``, ``90m`` or ``0s``.

    :param text: number followed by one unit among s, m, h, d.
    :returns: the duration in seconds (int).
    :raises ReapError: when the text is not a valid duration.
    """
    match = DURATION_RE.match(text.strip())
    if not match:
        raise ReapError(f"invalid duration {text!r}: expected e.g. 72h, 3d, 90m")
    return int(match.group(1)) * DURATION_UNITS[match.group(2)]


def parse_log_ts(line):
    """Return the epoch timestamp of an opencode log line, or None.

    :param line: one raw line of ``opencode.log``.
    :returns: float seconds since epoch (UTC), or None if the line has no
        leading ``timestamp=`` field.
    """
    match = LOG_TS_RE.match(line)
    if not match:
        return None
    stamp = datetime.datetime.fromisoformat(match.group(1))
    return stamp.replace(tzinfo=datetime.timezone.utc).timestamp()


def parse_log(lines):
    """Extract instance starts and per-run session activity from the log.

    A line carrying ``session.id=`` (agent loop, LLM stream, tool
    processing, cancel) is real activity; idle TUIs only log maintenance
    lines without it.

    :param lines: iterable of raw log lines.
    :returns: ``(instances, activity)`` where ``instances`` is a list of
        :class:`Instance` and ``activity`` maps a run id to the tuple
        ``(last_ts, last_session_id)``.
    """
    instances = []
    activity = {}
    for line in lines:
        if "message=\"creating instance\"" in line:
            ts, run, directory = (parse_log_ts(line), LOG_RUN_RE.search(line),
                                  LOG_DIR_RE.search(line))
            if ts is not None and run and directory:
                instances.append(Instance(ts, run.group(1),
                                          directory.group(1) or directory.group(2)))
            continue
        session = LOG_SESSION_RE.search(line)
        if not session:
            continue
        ts, run = parse_log_ts(line), LOG_RUN_RE.search(line)
        if ts is None or not run:
            continue
        previous = activity.get(run.group(1))
        if previous is None or ts >= previous[0]:
            activity[run.group(1)] = (ts, session.group(1))
    return instances, activity


def tui_session_arg(argv):
    """Tell whether ``argv`` is an opencode TUI and extract ``--session``.

    :param argv: the process command line as a list.
    :returns: ``(is_tui, session_id_or_None)``.
    """
    if not argv or os.path.basename(argv[0]) != "opencode":
        return False, None
    session = None
    args = argv[1:]
    for index, arg in enumerate(args):
        if arg in ("--session", "-s") and index + 1 < len(args):
            session = args[index + 1]
        elif arg.startswith("--session="):
            session = arg.split("=", 1)[1]
    positionals = [a for i, a in enumerate(args)
                   if not a.startswith("-") and (i == 0 or args[i - 1] not in
                                                 ("--session", "-s", "--port",
                                                  "--hostname", "--log-level",
                                                  "-m", "--model", "--agent",
                                                  "--prompt"))]
    if positionals and positionals[0] in NON_TUI_SUBCOMMANDS:
        return False, None
    if session is not None and not SESSION_ID_RE.match(session):
        session = None
    return True, session


def boot_epoch():
    """Return the wall-clock epoch of boot, consistent with /proc/uptime.

    :returns: float seconds since epoch.
    """
    with open("/proc/uptime", encoding="ascii") as fh:
        uptime = float(fh.read().split()[0])
    return time.time() - uptime


def tmux_target(pid):
    """Return the ``session:window.pane`` of the tmux pane hosting ``pid``.

    Best effort: reads ``TMUX``/``TMUX_PANE`` from the process environment.
    The tmux binary is looked up in the TUI's own ``PATH`` first, because
    cron runs with a minimal ``PATH`` and tmux may live in ``~/bin``.

    :param pid: process id.
    :returns: the target string, or None when unknown.
    """
    try:
        with open(f"/proc/{pid}/environ", "rb") as fh:
            env = dict(item.split(b"=", 1) for item in fh.read().split(b"\0")
                       if b"=" in item)
    except OSError:
        return None
    socket, pane = env.get(b"TMUX"), env.get(b"TMUX_PANE")
    tmux = shutil.which("tmux", path=env.get(b"PATH", b"").decode(errors="replace")) \
        or shutil.which("tmux")
    if not socket or not pane or not tmux:
        return None
    result = subprocess.run(
        [tmux, "-S", socket.split(b",")[0].decode(), "display-message", "-p",
         "-t", pane.decode(), "#{session_name}:#{window_index}.#{pane_index}"],
        capture_output=True, text=True, check=False, timeout=5)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def find_tuis(exclude=()):
    """List running opencode TUIs owned by the current user.

    :param exclude: pids to ignore (e.g. our own ancestors).
    :returns: list of :class:`Tui`.
    """
    hertz = os.sysconf("SC_CLK_TCK")
    boot = boot_epoch()
    uid = os.getuid()
    tuis = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) in exclude:
            continue
        pid = int(entry)
        try:
            if os.stat(f"/proc/{pid}").st_uid != uid:
                continue
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                argv = [a.decode(errors="replace") for a in fh.read().split(b"\0") if a]
            is_tui, session = tui_session_arg(argv)
            if not is_tui:
                continue
            with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as fh:
                start_ticks = int(fh.read().rsplit(")", 1)[1].split()[19])
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except (OSError, IndexError, ValueError):
            continue  # process vanished or unreadable
        tuis.append(Tui(pid, cwd, boot + start_ticks / hertz, session,
                        tmux_target(pid)))
    return tuis


def match_run(tui, instances):
    """Find the log run id of a TUI, refusing ambiguous matches.

    :param tui: the :class:`Tui`.
    :param instances: all :class:`Instance` entries from the log.
    :returns: ``(run_id, None)`` on a unique match, ``(None, reason)``
        otherwise.
    """
    candidates = [i for i in instances if i.directory == tui.cwd
                  and abs(i.ts - tui.start) <= MATCH_WINDOW_S]
    if not candidates:
        return None, "no matching 'creating instance' line in the log"
    if len({c.run for c in candidates}) > 1:
        return None, "ambiguous: several opencode starts in this directory"
    return candidates[0].run, None


class OpencodeDb:
    """Minimal access to opencode's session table through ``opencode db``."""

    def __init__(self, binary, runner=subprocess.run):
        """Store the opencode binary path and the subprocess runner.

        :param binary: path to the ``opencode`` executable.
        :param runner: callable compatible with :func:`subprocess.run`
            (injected by tests).
        """
        self.binary = binary
        self.runner = runner

    def query(self, sql):
        """Run one SQL statement and return the result rows.

        :param sql: the statement; callers only interpolate validated ids.
        :returns: list of dict rows.
        :raises ReapError: if ``opencode db`` fails.
        """
        result = self.runner([self.binary, "db", "--format", "json", sql],
                             capture_output=True, text=True, check=False,
                             timeout=60)
        if result.returncode != 0:
            raise ReapError(f"opencode db failed ({result.returncode}): "
                            f"{(result.stderr or result.stdout).strip()[:300]}")
        out = result.stdout.strip()
        return json.loads(out) if out else []

    def session(self, session_id):
        """Return one session row (id, title, parent_id, directory, time_updated).

        Each call queries the database (no cache) so that a check made
        after an update sees the new value.

        :param session_id: an ``ses_*`` id.
        :returns: the row dict, or None if it does not exist.
        :raises ReapError: on an invalid id or several rows.
        """
        if not SESSION_ID_RE.match(session_id):
            raise ReapError(f"invalid session id {session_id!r}")
        rows = self.query("SELECT id, title, parent_id, directory, time_updated "
                          f"FROM session WHERE id = '{session_id}'")
        if len(rows) > 1:
            raise ReapError(f"{len(rows)} rows for session {session_id}")
        if rows and "title" not in rows[0]:
            raise ReapError("unexpected session schema: no 'title' column")
        return rows[0] if rows else None

    def root_session(self, session_id):
        """Follow ``parent_id`` up to the top-level session.

        Sub-agent sessions log under the same run as their parent; the
        session to tag is the user's top-level one.

        :param session_id: any session id.
        :returns: the root session row, or None if the chain is broken.
        """
        seen = set()
        row = self.session(session_id)
        while row and row.get("parent_id") and row["id"] not in seen:
            seen.add(row["id"])
            row = self.session(row["parent_id"])
        return row

    def mark_wip(self, session_id):
        """Prefix the session title with ``[WIP] `` once (idempotent).

        Only the ``title`` column changes; ``time_updated`` is preserved so
        the session keeps its place in the session list.

        :param session_id: the session to tag.
        :returns: the new title.
        :raises ReapError: if the session is missing or the update did not
            take effect.
        """
        before = self.session(session_id)
        if before is None:
            raise ReapError(f"session {session_id} not found")
        if not before["title"].startswith(WIP_PREFIX):
            self.query(f"UPDATE session SET title = '{WIP_PREFIX}' || title "
                       f"WHERE id = '{session_id}'")
        after = self.session(session_id)
        if after is None or not after["title"].startswith(WIP_PREFIX):
            raise ReapError(f"title of {session_id} was not updated")
        return after["title"]


def decide(tui, run, activity, session, now, threshold):
    """Compute the decision for one TUI (pure function).

    The last activity is the most recent of: the TUI start, the run's last
    session activity in the log, and the session's ``time_updated``.

    :param tui: the :class:`Tui`.
    :param run: its run id (already matched).
    :param activity: ``(ts, session_id)`` for this run, or None.
    :param session: the root session row, or None.
    :param now: current epoch.
    :param threshold: idle threshold in seconds.
    :returns: a :class:`Decision`.
    """
    candidates = [tui.start]
    if activity:
        candidates.append(activity[0])
    if session and session.get("time_updated"):
        candidates.append(session["time_updated"] / 1000)
    last = max(candidates)
    idle = now - last
    if idle < threshold:
        return Decision(tui, "keep", f"active {format_age(idle)} ago",
                        run, session, last)
    return Decision(tui, "close", f"idle for {format_age(idle)}",
                    run, session, last)


def plan(tuis, instances, activity, db, now, threshold):
    """Build the list of decisions for all TUIs.

    :returns: list of :class:`Decision`.
    """
    decisions = []
    for tui in sorted(tuis, key=lambda t: t.start):
        run, problem = match_run(tui, instances)
        if run is None:
            decisions.append(Decision(tui, "skip", problem))
            continue
        run_activity = activity.get(run)
        session_id = run_activity[1] if run_activity else tui.session_arg
        session = db.root_session(session_id) if session_id else None
        decisions.append(decide(tui, run, run_activity, session, now, threshold))
    return decisions


def terminate(pid, timeout=TERM_TIMEOUT_S):
    """Stop a TUI with SIGTERM and wait for it to exit.

    SIGTERM was verified on opencode 1.19.3 to exit cleanly (status 0),
    stop the MCP child processes and keep the session resumable. No
    SIGKILL escalation: a TUI that ignores SIGTERM is reported instead.

    :param pid: process id.
    :param timeout: seconds to wait.
    :returns: True if the process is gone.
    """
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.2)
    return False


def format_age(seconds):
    """Render a duration as ``3d4h``, ``5h12m`` or ``40m``.

    :param seconds: non-negative duration.
    :returns: compact string.
    """
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"


def format_ts(epoch):
    """Render an epoch as local ``YYYY-MM-DD HH:MM``.

    :param epoch: seconds since epoch, or None.
    :returns: the string, or ``-``.
    """
    if epoch is None:
        return "-"
    return datetime.datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")


def resume_command(directory, session_id):
    """Return the shell command that resumes a session in its directory.

    :param directory: session or TUI working directory.
    :param session_id: the session id.
    :returns: e.g. ``cd /path && opencode --session ses_x``.
    """
    return f"cd {shlex.quote(directory)} && opencode --session {session_id}"


def journal_record(decision, new_title, now):
    """Build the journal entry for a closed TUI.

    :returns: a JSON-serializable dict.
    """
    session = decision.session or {}
    directory = session.get("directory") or decision.tui.cwd
    session_id = session.get("id")
    return {
        "timestamp": now,
        "pid": decision.tui.pid,
        "tmux": decision.tui.tmux_target,
        "directory": directory,
        "session_id": session_id,
        "title": new_title or session.get("title"),
        "last_activity": decision.last_activity,
        "resume": resume_command(directory, session_id) if session_id else None,
    }


def append_journal(state_dir, record):
    """Append one record to the NDJSON journal and to syslog.

    :param state_dir: directory holding ``closed.jsonl``.
    :param record: dict from :func:`journal_record`.
    """
    os.makedirs(state_dir, exist_ok=True)
    with open(os.path.join(state_dir, JOURNAL_NAME), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    if shutil.which("logger"):
        subprocess.run(["logger", "-t", "oc-reap",
                        f"closed pid {record['pid']} session {record['session_id']} "
                        f"({record['title']}) in {record['directory']}"],
                       check=False, timeout=5)


def read_journal(state_dir):
    """Read all journal records, oldest first.

    :returns: list of dicts (empty if no journal yet).
    :raises ReapError: on a corrupt line.
    """
    path = os.path.join(state_dir, JOURNAL_NAME)
    if not os.path.exists(path):
        return []
    records = []
    with open(path, encoding="utf-8") as fh:
        for number, line in enumerate(fh, 1):
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ReapError(f"{path}:{number}: corrupt journal line") from exc
    return records


def ancestors():
    """Return the pids of this process and all its ancestors.

    Prevents ``oc-reap`` from closing the TUI it was launched from.

    :returns: set of pids.
    """
    pids, pid = set(), os.getpid()
    while pid > 1:
        pids.add(pid)
        try:
            with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as fh:
                pid = int(fh.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return pids


def find_opencode(explicit):
    """Locate the opencode executable (needed by cron's minimal PATH).

    :param explicit: ``--opencode`` value or None.
    :returns: an absolute path.
    :raises ReapError: if not found.
    """
    for candidate in (explicit, os.environ.get("OC_REAP_OPENCODE"),
                      shutil.which("opencode"),
                      os.path.expanduser("~/.opencode/bin/opencode")):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    raise ReapError("opencode executable not found; use --opencode PATH")


def cmd_reap(args, out):
    """Implement ``oc-reap reap``.

    :returns: exit code.
    """
    threshold = parse_duration(args.older_than)
    if not os.path.exists(args.log):
        raise ReapError(f"log file not found: {args.log}")
    db = OpencodeDb(find_opencode(args.opencode))
    os.makedirs(args.state_dir, exist_ok=True)
    with open(os.path.join(args.state_dir, LOCK_NAME), "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReapError("another oc-reap run is in progress") from exc
        tuis = find_tuis(exclude=ancestors())
        if args.pid:
            tuis = [t for t in tuis if t.pid in set(args.pid)]
        with open(args.log, encoding="utf-8", errors="replace") as fh:
            instances, activity = parse_log(fh)
        now = time.time()
        decisions = plan(tuis, instances, activity, db, now, threshold)
        failures = 0
        for decision in decisions:
            result = {"action": decision.action, "reason": decision.reason}
            if decision.action == "close" and not args.dry_run:
                if not terminate(decision.tui.pid):
                    failures += 1
                    result = {"action": "error",
                              "reason": "still running after SIGTERM"}
                else:
                    new_title = None
                    if decision.session:
                        try:
                            new_title = db.mark_wip(decision.session["id"])
                        except ReapError as exc:
                            failures += 1
                            result["reason"] += f"; rename failed: {exc}"
                    append_journal(args.state_dir,
                                   journal_record(decision, new_title, now))
                    result["action"] = "closed"
            report(decision, result, args, out)
    return EXIT_ERROR if failures else EXIT_OK


def report(decision, result, args, out):
    """Print one decision in human or NDJSON form."""
    session = decision.session or {}
    if args.json:
        out.write(json.dumps({
            "timestamp": time.time(), "pid": decision.tui.pid,
            "action": result["action"], "reason": result["reason"],
            "last_activity": decision.last_activity,
            "tmux": decision.tui.tmux_target, "directory": decision.tui.cwd,
            "session_id": session.get("id"), "title": session.get("title"),
            "run": decision.run}, ensure_ascii=False) + "\n")
        return
    if args.quiet and result["action"] in ("keep", "skip"):
        return
    label = result["action"] + (" (dry-run)" if args.dry_run and
                                 result["action"] == "close" else "")
    out.write(f"{label:17} pid {decision.tui.pid:<8} {result['reason']}\n"
              f"{'':17} {decision.tui.tmux_target or '-'}  {decision.tui.cwd}\n"
              f"{'':17} {session.get('id') or '(no session)'}  "
              f"{session.get('title') or ''}\n")


def cmd_list(args, out, db=None):
    """Implement ``oc-reap list``: closed sessions, newest first.

    By default only sessions whose title still starts with ``[WIP] `` are
    shown (removing the prefix marks the work as done).

    :param db: an :class:`OpencodeDb` (injected by tests); built from
        ``--opencode`` when None.
    :returns: exit code.
    """
    records = read_journal(args.state_dir)
    latest = {}
    for record in records:
        if record.get("session_id"):
            latest[record["session_id"]] = record
    if latest and db is None:
        db = OpencodeDb(find_opencode(args.opencode))
    rows = []
    for record in sorted(latest.values(), key=lambda r: r["timestamp"], reverse=True):
        current = db.session(record["session_id"])
        title = current["title"] if current else None
        if not args.all and (title is None or not title.startswith(WIP_PREFIX)):
            continue
        rows.append(dict(record, title=title, exists=current is not None))
    for row in rows:
        if args.json:
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
        else:
            out.write(f"{format_ts(row['timestamp'])}  {row['title'] or '(deleted)'}\n"
                      f"    last activity {format_ts(row['last_activity'])}"
                      f"  tmux {row['tmux'] or '-'}\n"
                      f"    {row['resume']}\n")
    if not rows and not args.json and not args.quiet:
        out.write("no closed WIP session\n")
    return EXIT_OK


def build_parser():
    """Create the argument parser.

    :returns: :class:`argparse.ArgumentParser`.
    """
    parser = argparse.ArgumentParser(
        prog="oc-reap",
        description="Close idle opencode TUIs and tag their sessions [WIP].",
        epilog="examples:\n"
               "  oc-reap reap --dry-run           show what would be closed\n"
               "  oc-reap reap -q                  close TUIs idle for 72h+ (cron)\n"
               "  oc-reap reap --older-than 24h\n"
               "  oc-reap list                     closed sessions still [WIP]\n"
               "\nenvironment:\n"
               "  OC_REAP_OPENCODE  path to the opencode executable",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--state-dir", default=DEFAULT_STATE_DIR,
                        help=f"journal directory (default: {DEFAULT_STATE_DIR})")
    common.add_argument("--opencode", help="opencode executable path")
    common.add_argument("-j", "--json", action="store_true", help="NDJSON output")
    common.add_argument("-q", "--quiet", action="store_true",
                        help="only report actions and errors")
    sub = parser.add_subparsers(dest="command", required=True)
    reap = sub.add_parser("reap", parents=[common],
                          help="close idle TUIs and tag their sessions")
    reap.add_argument("--older-than", default=DEFAULT_OLDER_THAN,
                      help=f"idle threshold, e.g. 72h, 3d (default: {DEFAULT_OLDER_THAN})")
    reap.add_argument("-n", "--dry-run", action="store_true",
                      help="show decisions, change nothing")
    reap.add_argument("--pid", type=int, action="append",
                      help="only consider this TUI pid (repeatable)")
    reap.add_argument("--log", default=DEFAULT_LOG,
                      help=f"opencode log (default: {DEFAULT_LOG})")
    lst = sub.add_parser("list", parents=[common], help="list closed sessions")
    lst.add_argument("-a", "--all", action="store_true",
                     help="include sessions whose [WIP] prefix was removed")
    return parser


def main(argv=None, out=sys.stdout):
    """CLI entry point.

    :returns: process exit code.
    """
    args = build_parser().parse_args(argv)
    try:
        if args.command == "reap":
            return cmd_reap(args, out)
        return cmd_list(args, out)
    except ReapError as exc:
        print(f"oc-reap: error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
