import argparse

import uvicorn

from galochka import db
from galochka.api import create_app
from galochka.config import Settings


def _cmd_serve(args: argparse.Namespace, settings: Settings) -> None:
    app = create_app(settings)
    uvicorn.run(app, host=args.host, port=args.port)


def _cmd_migrate(args: argparse.Namespace, settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = db.connect(settings.db_path)
    try:
        db.migrate(conn)
    finally:
        conn.close()
    print("ok")


def main(argv: list[str] | None = None) -> None:
    settings = Settings.from_env()

    parser = argparse.ArgumentParser(prog="galochka")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="run the web server")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=settings.port)
    serve.set_defaults(func=_cmd_serve)

    migrate = subparsers.add_parser("migrate", help="create or upgrade the database")
    migrate.set_defaults(func=_cmd_migrate)

    args = parser.parse_args(argv)
    args.func(args, settings)
