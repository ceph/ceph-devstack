"""HTTP API for on-demand testnode container lifecycle.

Runs as a service in the ceph-devstack stack, allowing teuthology's
devstack provisioner to create and destroy testnode containers dynamically.
"""

import asyncio
import json

from aiohttp import web

from ceph_devstack import config, logger
from ceph_devstack.resources.ceph.containers import TestNode

routes = web.RouteTableDef()

_nodes: dict[str, TestNode] = {}
_lock = asyncio.Lock()


def _make_testnode(name: str) -> TestNode:
    """Instantiate a TestNode with the stack's current settings."""
    return TestNode(
        name=name,
        data_dir=None,
        active_services=list(
            config.get("stacks", {}).get("teuthology", {}).get("services", [])
        ),
    )


async def _get_container_ssh_info(node: TestNode) -> dict:
    """Inspect a running testnode to extract its SSH connection details."""
    proc = await node.cmd(
        ["podman", "container", "inspect", node.name],
        check=False,
    )
    stdout, _ = await proc.collect_output()
    if proc.returncode != 0:
        return {}
    info = json.loads(stdout)
    if not info:
        return {}
    ports = info[0].get("NetworkSettings", {}).get("Ports", {})
    ssh_port_mappings = ports.get("22/tcp", [])
    host_port = int(ssh_port_mappings[0]["HostPort"]) if ssh_port_mappings else None
    container_name = info[0].get("Name", node.name).lstrip("/")
    return {
        "name": container_name,
        "ssh_host": container_name,
        "ssh_port": host_port,
        "status": info[0].get("State", {}).get("Status", "unknown"),
    }


@routes.get("/health")
async def health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


@routes.get("/nodes")
async def list_nodes(request: web.Request) -> web.Response:
    result = []
    async with _lock:
        for name, node in _nodes.items():
            running = await node.is_running()
            result.append({"name": name, "running": running})
    return web.json_response(result)


@routes.post("/nodes/{name}")
async def create_node(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    body = await request.json() if request.body_exists else {}
    os_type = body.get("os_type", "")
    os_version = body.get("os_version", "")

    async with _lock:
        node = _nodes.get(name)
        if node and await node.is_running():
            info = await _get_container_ssh_info(node)
            return web.json_response(info, status=200)

        if not node:
            node = _make_testnode(name)
            _nodes[name] = node

    logger.info(
        f"api: creating testnode {name} (os_type={os_type}, os_version={os_version})"
    )
    try:
        await node.create()
        await node.start()
    except Exception:  # noqa: BLE001
        logger.exception(f"api: failed to create/start {name}")
        return web.json_response({"error": f"failed to create {name}"}, status=500)

    info = await _get_container_ssh_info(node)
    return web.json_response(info, status=201)


@routes.delete("/nodes/{name}")
async def destroy_node(request: web.Request) -> web.Response:
    name = request.match_info["name"]

    async with _lock:
        node = _nodes.pop(name, None)

    if not node:
        return web.json_response({"error": "not found"}, status=404)

    logger.info(f"api: destroying testnode {name}")
    try:
        await node.remove()
    except Exception:  # noqa: BLE001
        logger.exception(f"api: failed to remove {name}")
        return web.json_response({"error": f"failed to remove {name}"}, status=500)
    return web.json_response({"status": "removed"})


def create_app() -> web.Application:
    app = web.Application()
    app.add_routes(routes)
    return app


def run_api(host: str = "0.0.0.0", port: int = 8090) -> None:
    """Entry point for running the API server standalone."""
    app = create_app()
    web.run_app(app, host=host, port=port)
