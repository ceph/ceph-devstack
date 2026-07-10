import asyncio
import logging
import sys

from pathlib import Path

from ceph_devstack import config, logger, parse_args, VERBOSE
from ceph_devstack.requirements import check_requirements
from ceph_devstack.resources.ceph import CephDevStack

CONFIG_HANDLERS = {
    "dump": lambda config, args: print(config.dump()),
    "get": lambda config, args: print(config.get_value(args.name)),
    "set": lambda config, args: print(config.set_value(args.name, args.value)),
    "unset": lambda config, args: config.unset_value(args.name),
}

COMMAND_HANDLERS = {
    "doctor": None,
    "apply": lambda args, obj: obj.apply(args.command),
    "pull": lambda _, obj: obj.pull(),
    "build": lambda _, obj: obj.build(),
    "create": lambda _, obj: obj.create(),
    "remove": lambda _, obj: obj.remove(),
    "start": lambda _, obj: obj.start(),
    "stop": lambda _, obj: obj.stop(),
    "watch": lambda _, obj: obj.watch(),
    "wait": lambda args, obj: obj.wait(container_name=args.container),
    "logs": lambda args, obj: obj.logs(
        run_name=args.run_name, job_id=args.job_id, locate=args.locate
    ),
}


def main() -> int:
    args = parse_args(sys.argv[1:])
    config.load(args.config_file)
    if args.verbose:
        for handler in logging.getLogger("root").handlers:
            if not isinstance(handler, logging.FileHandler):
                handler.setLevel(VERBOSE)
    if args.command == "config":
        CONFIG_HANDLERS[args.config_op](config, args)
        return 0
    config["args"] = vars(args)
    data_path = Path(config["data_dir"]).expanduser()
    data_path.mkdir(parents=True, exist_ok=True)
    obj = CephDevStack()

    async def run():
        if not all(
            await asyncio.gather(
                check_requirements(),
                obj.check_requirements(),
            )
        ):
            logger.error("Requirements not met!")
            return 1
        handler = COMMAND_HANDLERS.get(args.command)
        if handler:
            return await handler(args, obj)

    try:
        sys.exit(asyncio.run(run()))
    except KeyboardInterrupt:
        logger.debug("Exiting!")
        return 130  # 128 + SIGINT
