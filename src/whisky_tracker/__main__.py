"""Manual one-shot command: ``python -m whisky_tracker run``."""

import argparse
import asyncio
import logging

from whisky_tracker.application.bootstrap import build_runtime
from whisky_tracker.application.config import load_config
from whisky_tracker.application.formatting import format_run_summary


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m whisky_tracker")
    subcommands = parser.add_subparsers(dest="command", required=True)
    run_parser = subcommands.add_parser("run", help="execute one collection run")
    run_parser.add_argument("--dry-run", action="store_true")
    run_parser.add_argument(
        "--retailer",
        action="append",
        choices=("carrefour", "coto", "jumbo", "mercadolibre"),
        help="limit collection; may be repeated",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(_run(args))


async def _run(args: argparse.Namespace) -> None:
    config = load_config()
    selected = frozenset(args.retailer) if args.retailer else None
    runtime = build_runtime(config, selected_retailers=selected)
    try:
        summary = await runtime.runner.run(dry_run=args.dry_run)
        print(format_run_summary(summary, include_alert_messages=args.dry_run))
    finally:
        await runtime.close()


if __name__ == "__main__":
    main()
