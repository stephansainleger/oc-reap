# oc-reap — agent notes

Single-file Python CLI (`oc_reap.py`, stdlib only) that closes idle
opencode TUIs and prefixes their session title with `[WIP] `.

## Invariants (do not break)

- Never delete a session, never write any column other than `title`.
- Never close a TUI that cannot be matched to exactly one log run.
- Never send `SIGKILL`; never consider our own ancestors.
- Session ids are validated (`^ses_[A-Za-z0-9]+$`) before any SQL.

## Tests

- `python3 -m unittest discover -s tests` and `ruff check .` must pass.
- `OC_REAP_INTEGRATION=1 python3 -m unittest tests.test_integration`
  touches the real opencode database (throwaway session, deleted at the
  end) and a throwaway tmux server: always `-L <socket> -f /dev/null`,
  always killed with its socket removed.
- Never target real TUIs in tests: use `--pid` on the test TUI.
