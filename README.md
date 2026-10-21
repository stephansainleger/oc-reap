# oc-reap

Close idle [opencode](https://opencode.ai) TUIs and tag their sessions
`[WIP] ` so unfinished work is easy to find again.

A TUI left open for days keeps hundreds of MB of RAM, gigabytes of swap
and tens of thousands of inotify watches. `oc-reap reap` stops the TUIs
whose session has been idle longer than a threshold (72 h by default).
**Sessions are never deleted**: the TUI process exits, the session stays
in opencode's database and can be resumed at any time.

## What it does

For every opencode TUI owned by the current user:

1. Identify its log run: the `creating instance` line of
   `~/.local/share/opencode/log/opencode.log` with the same working
   directory, within 120 s of the process start. No match, or several
   matches: the TUI is **skipped**, never closed.
2. Compute its last activity: the most recent of the process start, the
   last log line of that run carrying a `session.id=` (agent loop, LLM
   stream, tool call), and the session's `time_updated`. Maintenance log
   lines (file watcher, project refresh) are ignored.
3. If idle longer than the threshold: send `SIGTERM` (verified on opencode
   1.19.3: clean exit, MCP children stopped, session resumable), wait up
   to 15 s, never escalate to `SIGKILL`.
4. Prefix the session title with `[WIP] ` (once; sub-agent activity is
   resolved to the top-level session). Only the `title` column changes,
   through `opencode db`; `time_updated` is kept so the session list order
   does not change.
5. Append a record to `~/.local/state/oc-reap/closed.jsonl` and to syslog
   (`journalctl -t oc-reap`).

The TUI that runs `oc-reap` (and its ancestors) is never considered.

## Usage

```
oc-reap reap --dry-run            # show decisions, change nothing
oc-reap reap                      # close TUIs idle for 72h+
oc-reap reap --older-than 24h
oc-reap list                      # closed sessions still tagged [WIP]
oc-reap list --all                # include sessions marked done
```

Example (`reap --dry-run`):

```
close (dry-run)   pid 642871   idle for 3d0h
                  gogocarto:0.0  /home/u/dev/Odoo/18.0/partner-tools
                  ses_eaf28dce3ffeqRGUtyLKHMme15  partner_gogocarto_export_api 16.0 to 18.0
keep              pid 1450279  active 2m ago
                  elabore:0.1  /home/u/dev/Odoo/18.0
                  ses_eac86cd5effewku44MvTWQxwZO  Fix main_domain_name field
```

Example (`list`):

```
2026-10-22 03:00  [WIP] partner_gogocarto_export_api 16.0 to 18.0
    last activity 2026-10-19 02:06  tmux gogocarto:0.0
    cd /home/u/dev/Odoo/18.0/partner-tools && opencode --session ses_eaf28dce3ffeqRGUtyLKHMme15
```

### Finding and closing your WIP sessions

- In the opencode session picker, type `WIP`.
- `oc-reap list` gives the resume command for each one.
- When the work is done, rename the session in the TUI (command palette,
  "Rename Session") and remove the `[WIP] ` prefix: it disappears from
  `oc-reap list`.

### Options

| Option | Commands | Meaning |
| --- | --- | --- |
| `--older-than DURATION` | reap | idle threshold: `72h` (default), `3d`, `90m`, `0s` |
| `-n`, `--dry-run` | reap | print decisions only |
| `--pid PID` | reap | only consider this TUI (repeatable) |
| `--log PATH` | reap | opencode log (default `~/.local/share/opencode/log/opencode.log`) |
| `-a`, `--all` | list | include sessions whose `[WIP] ` prefix was removed |
| `--state-dir DIR` | both | journal directory (default `$XDG_STATE_HOME/oc-reap`) |
| `--opencode PATH` | both | opencode executable (also `$OC_REAP_OPENCODE`) |
| `-j`, `--json` | both | NDJSON output |
| `-q`, `--quiet` | both | only print actions and errors |

Exit codes: `0` success, `1` error (including a TUI that ignored
`SIGTERM` or a failed rename), `2` usage error.

### NDJSON fields

`reap --json`: `timestamp`, `pid`, `action` (`close`, `closed`, `keep`,
`skip`, `error`), `reason`, `last_activity`, `tmux`, `directory`,
`session_id`, `title`, `run`.

`list --json` (journal record plus current state): `timestamp`, `pid`,
`tmux`, `directory`, `session_id`, `title` (current), `last_activity`,
`resume`, `exists`. Timestamps are UTC float seconds since epoch.

## Installation

Requires Python 3.10+, opencode, and optionally tmux (pane names) and
`logger`.

```
ln -s ~/dev/oc-reap/oc_reap.py ~/.local/bin/oc-reap
```

Hourly cron job (`crontab -e`):

```
0 * * * * $HOME/.local/bin/oc-reap reap -q >> $HOME/.local/state/oc-reap/cron.log 2>&1
```

## Tests

```
python3 -m unittest discover -s tests            # unit tests
OC_REAP_INTEGRATION=1 python3 -m unittest tests.test_integration
ruff check .
```

The integration test imports a throwaway session from
`tests/fixtures/session.json` (no LLM call), opens it in a TUI inside an
isolated tmux server, reaps it, checks the `[WIP] ` title, the journal and
the resume, then deletes the session.

## Limits

- Relies on opencode internals verified on **1.19.3**: the log line
  formats and the `session` table schema. Each run checks the `title`
  column and fails loudly if it is missing; re-run the integration test
  after an opencode upgrade.
- A TUI started with `opencode --session <id>` directly as a tmux window
  command closes its window too (no shell behind it).

## License

AGPL-3.0-or-later.
