"""Command entrypoint for the MissionOS Gateway fixture server."""

from __future__ import annotations

import argparse

from .server import run_web


def main() -> None:
    parser = argparse.ArgumentParser(prog="missionos-gateway")
    subparsers = parser.add_subparsers(dest="command", required=True)

    web = subparsers.add_parser("web", help="Run the MissionOS Gateway HTTP server.")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", default=18791, type=int)

    args = parser.parse_args()
    if args.command == "web":
        run_web(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
