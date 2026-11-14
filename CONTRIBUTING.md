# Contributing to PyDB

Thanks for your interest. PyDB aims to be a database engine that is easy to read and hard
to break. Changes are welcome if they keep it that way.

## Setup

```powershell
git clone https://github.com/praneeth-srikurmadasu/PyDB.git
cd PyDB
py -3.13 -m venv .venv
& ".\.venv\Scripts\python.exe" -m pip install -e ".[dev]"
```

On Linux or macOS: `python3 -m venv .venv` and `.venv/bin/python`.

## Before you open a pull request

All four must pass. CI runs them on Ubuntu and Windows with Python 3.12 and 3.13:

```powershell
& ".\.venv\Scripts\python.exe" -m ruff check .
& ".\.venv\Scripts\python.exe" -m ruff format --check .
& ".\.venv\Scripts\python.exe" -m mypy
& ".\.venv\Scripts\python.exe" -m pytest
```

For changes to storage, indexes, transactions or locking, also run the deep profile once:
`$env:HYPOTHESIS_PROFILE = "thorough"`, plus a longer crash run with
`$env:PYDB_CRASH_ROUNDS = "40"`.

## Rules for the code

- **Standard library only** in `pydb/`. Development tools are pinned in `pyproject.toml`.
  `sqlite3` appears only as a test oracle (`tests/integration/test_query_suite.py`) and in
  `benchmarks/`.
- **Typed:** `mypy --strict` covers `pydb/`, `tests/` and `benchmarks/`. Avoid `Any` and
  `# type: ignore`.
- **Errors:** raise a subclass of `pydb.errors.PyDBError` with a message a user can act on.
- **Docstrings explain why.** Describe invariants and the reason for a design, not what
  the next line does.
- **Docs:** if behaviour or an on-disk format changes, update the matching file in `docs/`
  in the same change.

## Rules for tests

- Every change comes with tests, and a bug fix with a test that fails without it.
- Prefer model-based tests (Hypothesis `RuleBasedStateMachine`) for data structures: run
  random operations against a simple Python model and check invariants after each step.
- Anything that writes pages must stay correct across crashes. Extend `tests/crash/` or
  `tests/property/test_crash_model.py` when you add a new kind of write.
- Concurrency tests must not depend on luck. Wait for a session to block
  (`db.locks.waiting()`) instead of sleeping and hoping.
- Tests use `tmp_path` and never leave `*.db` or `*.wal` files behind.

## Commits and pull requests

- Use [Conventional Commits](https://www.conventionalcommits.org/): `feat: ...`,
  `fix: ...`, `docs: ...`, `test: ...`, `refactor: ...`.
- Keep one logical change per pull request, and describe what changed and how it was
  tested.
- Never commit database files, logs, caches, virtual environments or editor settings;
  `.gitignore` covers them.
- Add a line to `CHANGELOG.md` under an `Unreleased` heading for anything users would
  notice.

## Reporting bugs

Open an issue with the SQL or Python that reproduces the problem, what you expected, what
happened, and your OS and Python version. For a crash or corruption bug, attach the
database and its `.wal` file if you can.
