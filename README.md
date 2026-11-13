# PyDB

A disk-based relational database engine written in pure Python (standard library only).

Work in progress.

## Try it

```powershell
& "$HOME\Projects\PyDB\.venv\Scripts\python.exe" -m pydb.cli.repl demo.db
```

```sql
pydb> CREATE TABLE users (id INT PRIMARY KEY, name TEXT NOT NULL);
pydb> INSERT INTO users VALUES (1, 'ann'), (2, 'bob');
pydb> SELECT name FROM users WHERE id > 1;
```

Meta commands: `.tables`, `.schema [table]`, `.timer on|off`, `.help`, `.quit`.

## Development setup (Windows PowerShell)

```powershell
cd "$HOME\Projects\PyDB"
py -3.13 -m venv .venv
& "$HOME\Projects\PyDB\.venv\Scripts\python.exe" -m pip install --upgrade pip
& "$HOME\Projects\PyDB\.venv\Scripts\python.exe" -m pip install -e ".[dev]"
```

## Checks

```powershell
& "$HOME\Projects\PyDB\.venv\Scripts\python.exe" -m ruff check .
& "$HOME\Projects\PyDB\.venv\Scripts\python.exe" -m ruff format --check .
& "$HOME\Projects\PyDB\.venv\Scripts\python.exe" -m mypy
& "$HOME\Projects\PyDB\.venv\Scripts\python.exe" -m pytest
```

On Linux or macOS, use `.venv/bin/python` instead.

Property-based tests run 50 examples each by default. For a deeper run (about 10x longer):

```powershell
$env:HYPOTHESIS_PROFILE = "thorough"
& "$HOME\Projects\PyDB\.venv\Scripts\python.exe" -m pytest
Remove-Item Env:HYPOTHESIS_PROFILE
```

## License

MIT, see [LICENSE](LICENSE).
