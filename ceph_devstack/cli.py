import asyncio
import logging
import sys

from pathlib import Path

from ceph_devstack import config, logger, parse_args, VERBOSE
from ceph_devstack.block_pool import BlockPoolError, format_size, shared_pool
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


def _configure_console_logging(verbose: bool) -> None:
    if not verbose:
        return
    for handler in logging.getLogger("root").handlers:
        if not isinstance(handler, logging.FileHandler):
            handler.setLevel(VERBOSE)


def block_pool_status() -> int:
    try:
        pool = shared_pool()
    except BlockPoolError as e:
        logger.error(f"block pool: {e}")
        return 1
    if pool is None:
        logger.info("block pool: disabled (no parent configured)")
        return 0
    logger.info(f"block pool parent: {pool.parent}")
    logger.info(f"enrolled: {pool.enrolled}")
    logger.info(f"pool id: {pool.pool_id}")
    logger.info(f"state file: {pool.state_path}")
    logger.info("allocations:" if pool.allocations else "allocations: (none)")
    for key, region in sorted(pool.allocations.items()):
        logger.info(
            f"  {key}: offset {region['offset']}, "
            f"size {format_size(region['size'])}, last owner {region['owner']}"
        )
    logger.info("free regions:" if pool.free_regions else "free regions: (none)")
    for region in pool.free_regions:
        logger.info(f"  offset {region['offset']}, size {format_size(region['size'])}")
    logger.info(f"next offset: {pool.next_offset}")
    return 0


def main() -> int:
    args = parse_args(sys.argv[1:])
    config.load(args.config_file)
    _configure_console_logging(args.verbose)
    if args.command == "config":
        CONFIG_HANDLERS[args.config_op](config, args)
        return 0
    if args.command == "block-pool":
        if args.block_pool_op != "status":
            logger.error("Usage: ceph-devstack block-pool status")
            return 2
        return block_pool_status()
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
