import asyncio
import logging
import sys

from argparse import Namespace
from pathlib import Path
from subprocess import CalledProcessError

from ceph_devstack import config, logger, parse_args, VERBOSE
from ceph_devstack.requirements import check_requirements
from ceph_devstack.resources.ceph import CephDevStack

CONFIG_HANDLERS = {
    "dump": lambda config, args: print(config.dump()),
    "get": lambda config, args: print(config.get_value(args.name)),
    "set": lambda config, args: print(config.set_value(args.name, args.value)),
    "unset": lambda config, args: config.unset_value(args.name),
}


def _configure_console_logging(verbose: bool) -> None:
    if not verbose:
        return
    for handler in logging.getLogger("root").handlers:
        if not isinstance(handler, logging.FileHandler):
            handler.setLevel(VERBOSE)


def _action_kwargs(args: Namespace) -> dict:
    match args.command:
        case "wait":
            return {"container_name": args.container}
        case "logs":
            return {
                "run_name": args.run_name,
                "job_id": args.job_id,
                "locate": args.locate,
            }
        case _:
            return {}


async def _apply(stack: CephDevStack, action: str, **kwargs) -> int:
    """Map a CLI command to a stack action and apply it."""
    if not all(
        await asyncio.gather(
            check_requirements(),
            stack.check_requirements(),
        )
    ):
        logger.error("Requirements not met!")
        return 1
    if action == "doctor":
        return 0
    result = await stack.apply(action, **kwargs)
    return result if result is not None else 0


def main() -> int:
    args = parse_args(sys.argv[1:])
    config.load(args.config_file)
    try:
        config.apply_stack(args.stack)
    except ValueError as exc:
        logger.error(str(exc))
        return 1
    _configure_console_logging(args.verbose)
    if args.command == "config":
        CONFIG_HANDLERS[args.config_op](config, args)
        return 0
    config["args"] = vars(args)
    Path(config["data_dir"]).expanduser().mkdir(parents=True, exist_ok=True)
    stack = CephDevStack(stack_name=config.active_stack)
    action = args.command
    try:
        return asyncio.run(_apply(stack, action, **_action_kwargs(args)))
    except CalledProcessError as exc:
        return exc.returncode if exc.returncode is not None else 1
    except KeyboardInterrupt:
        logger.debug("Exiting!")
        return 130  # 128 + SIGINT
