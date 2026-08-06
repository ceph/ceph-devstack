import pytest
from unittest.mock import AsyncMock, patch

from ceph_devstack.api import create_app, _nodes


@pytest.fixture
def client(aiohttp_client):
    app = create_app()
    return aiohttp_client(app)


@pytest.fixture(autouse=True)
def clear_nodes():
    _nodes.clear()
    yield
    _nodes.clear()


@pytest.mark.asyncio
async def test_health(client):
    c = await client
    resp = await c.get("/health")
    assert resp.status == 200
    data = await resp.json()
    assert data == {"status": "ok"}


@pytest.mark.asyncio
async def test_list_nodes_empty(client):
    c = await client
    resp = await c.get("/nodes")
    assert resp.status == 200
    data = await resp.json()
    assert data == []


@pytest.mark.asyncio
@patch("ceph_devstack.api._make_testnode")
@patch("ceph_devstack.api._get_container_ssh_info")
async def test_create_node(mock_ssh_info, mock_make, client):
    mock_node = AsyncMock()
    mock_node.is_running = AsyncMock(return_value=False)
    mock_node.create = AsyncMock()
    mock_node.start = AsyncMock()
    mock_make.return_value = mock_node
    mock_ssh_info.return_value = {
        "name": "testnode_0",
        "ssh_host": "testnode_0",
        "ssh_port": 32768,
        "status": "running",
    }

    c = await client
    resp = await c.post(
        "/nodes/testnode_0",
        json={"os_type": "ubuntu", "os_version": "22.04"},
    )
    assert resp.status == 201
    data = await resp.json()
    assert data["name"] == "testnode_0"
    assert data["ssh_port"] == 32768
    mock_node.create.assert_awaited_once()
    mock_node.start.assert_awaited_once()


@pytest.mark.asyncio
@patch("ceph_devstack.api._make_testnode")
@patch("ceph_devstack.api._get_container_ssh_info")
async def test_create_node_already_running(mock_ssh_info, mock_make, client):
    mock_node = AsyncMock()
    mock_node.is_running = AsyncMock(return_value=True)
    mock_make.return_value = mock_node
    mock_ssh_info.return_value = {
        "name": "testnode_0",
        "ssh_host": "testnode_0",
        "ssh_port": 32768,
        "status": "running",
    }
    _nodes["testnode_0"] = mock_node

    c = await client
    resp = await c.post("/nodes/testnode_0", json={})
    assert resp.status == 200
    mock_node.create.assert_not_awaited()


@pytest.mark.asyncio
@patch("ceph_devstack.api._make_testnode")
async def test_destroy_node(mock_make, client):
    mock_node = AsyncMock()
    mock_node.remove = AsyncMock()
    _nodes["testnode_0"] = mock_node

    c = await client
    resp = await c.delete("/nodes/testnode_0")
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "removed"
    mock_node.remove.assert_awaited_once()
    assert "testnode_0" not in _nodes


@pytest.mark.asyncio
async def test_destroy_node_not_found(client):
    c = await client
    resp = await c.delete("/nodes/nonexistent")
    assert resp.status == 404
