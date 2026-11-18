# oc-reap — agent notes

Single-file Python CLI (`oc_reap.py`, stdlib only) that closes idle
opencode TUIs and prefixes their session title with `[WIP] `.

## Invariants (do not break)

- Never delete a session, never write any column other than `title`.
- Never close a TUI that cannot be matched to exactly one session (v2) or
  one log run (v1). V2 mapping uses the `@oc_session_id` tmux pane option,
  then `--session`, then an environment session only when it is not inherited
  from oc-reap's own session and matches the TUI directory.
- Never send `SIGKILL`; never consider our own ancestors.
- Session ids are validated (`^ses_[A-Za-z0-9]+$`) before any SQL or API call.

## Tests

- `python3 -m unittest discover -s tests` and `ruff check .` must pass.
- `OC_REAP_INTEGRATION=1 python3 -m unittest tests.test_integration`
  touches the real opencode database (throwaway session, deleted at the
  end) and a throwaway tmux server: always `-L <socket> -f /dev/null`,
  always killed with its socket removed. On v2 the throwaway session is
  created through `POST /api/session`; on v1 it is imported from the fixture.
- Never target real TUIs in tests: use `--pid` on the test TUI.
