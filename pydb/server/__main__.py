"""Run the PyDB server.

Usage::

    python -m pydb.server data.db [--host 127.0.0.1] [--port 5433]

Connect with ``python -m pydb.cli.repl --connect 127.0.0.1:5433`` or :class:`pydb.client.Client`.
Stop with Ctrl+C: open transactions are rolled back and the database is closed cleanly.
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence

from pydb import __version__
from pydb.config import DEFAULT_PORT, LOCK_TIMEOUT_SECONDS
from pydb.database import Database
from pydb.errors import PyDBError
from pydb.server.server import DEFAULT_MAX_CONNECTIONS, Server


async def serve(server: Server, db: Database) -> None:
    await server.start()
    host, port = server.address
    print(f"PyDB {__version__} serving {db.path} on {host}:{port}. Press Ctrl+C to stop.")
    sys.stdout.flush()
    try:
        await server.serve_forever()
    finally:
        await server.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pydb.server", description="PyDB server")
    parser.add_argument("database", help="path of the database file (created if missing)")
    parser.add_argument("--host", default="127.0.0.1", help="address to listen on")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="TCP port")
    parser.add_argument(
        "--max-connections", type=int, default=DEFAULT_MAX_CONNECTIONS, help="client limit"
    )
    parser.add_argument(
        "--lock-timeout",
        type=float,
        default=LOCK_TIMEOUT_SECONDS,
        help="seconds a statement waits for a table lock",
    )
    args = parser.parse_args(argv)
    try:
        db = Database(args.database, lock_timeout=args.lock_timeout)
    except PyDBError as exc:
        print(f"error: cannot open {args.database}: {exc}", file=sys.stderr)
        return 1
    server = Server(db, args.host, args.port, max_connections=args.max_connections)
    try:
        asyncio.run(serve(server, db))
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        print(f"error: cannot listen on {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()
        print("PyDB server stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
