from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from ai_control.config import load_config
from ai_control.core.instance_lock import InstanceAlreadyRunningError, InstanceLock
from ai_control.core.logging import configure_logging
from ai_control.doctor import format_doctor, run_doctor
from ai_control.git import GitService
from ai_control.platform import current_platform
from ai_control.projects import ProjectRegistry
from ai_control.sessions import TaskManager
from ai_control.setup import run_setup
from ai_control.storage import Database


async def serve(config_path: Path) -> None:
    config = load_config(config_path)
    data_dir = config.instance.data_dir.expanduser()
    with InstanceLock(data_dir / "ai-control.lock"):
        configure_logging(data_dir, config.instance.log_level)
        database = Database(data_dir / "state.db")
        await database.initialize()
        projects = ProjectRegistry(database)
        await projects.sync_config(config.projects)
        git = GitService()
        tasks = TaskManager(config, database, projects, current_platform(), git)
        await tasks.recover()
        from ai_control.bot import run_bot

        await run_bot(config, database, projects, tasks, git)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-control")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("run", help="run Telegram long polling")
    subcommands.add_parser("doctor", help="run diagnostics without printing secrets")
    subcommands.add_parser("setup", help="interactive first-run setup")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "setup":
        run_setup(args.config)
    elif args.command == "doctor":
        print(format_doctor(asyncio.run(run_doctor(load_config(args.config)))))
    else:
        try:
            asyncio.run(serve(args.config))
        except InstanceAlreadyRunningError as exc:
            raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
