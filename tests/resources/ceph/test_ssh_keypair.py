import pytest
from unittest.mock import AsyncMock, patch


from ceph_devstack.resources.ceph import SSHKeyPair
from tests.resources.test_misc import TestMiscResource as _TestMiscResource


class TestSSHKeyPair(_TestMiscResource):
    @pytest.fixture
    def cls(self):
        return SSHKeyPair

    def test_name(self, cls):
        obj = cls()
        assert obj.name == "id_rsa"

    def test_repr(self, cls):
        obj = cls()
        class_name = cls.__name__
        assert repr(obj) == f'{class_name}(name="id_rsa")'
        obj = cls(name="foo")
        assert repr(obj) == f'{class_name}(name="foo")'

    def test_ssh_key_pair_default_paths(self, cls):
        pair = SSHKeyPair()
        assert pair.privkey_path == "id_rsa"
        assert pair.pubkey_path == "id_rsa.pub"

    async def test_action_for_each_key(self, cls, action):
        with patch.object(cls, "cmd"):
            obj = cls()
            cmds = getattr(obj, f"{action}_cmds")
            assert len(cmds) == 2
            assert obj.format_cmd(cmds[0])[-1] == obj.privkey_path
            assert obj.format_cmd(cmds[1])[-1] == obj.pubkey_path

    def test_ssh_key_pair_cmd_vars(self, cls):
        obj = cls()
        assert "name" in obj.cmd_vars
        assert "privkey_path" in obj.cmd_vars
        assert "pubkey_path" in obj.cmd_vars

    @pytest.mark.parametrize("exists", [True, False])
    async def test_create_when_not_exists(self, cls, exists):
        obj = cls()
        with (
            patch.object(obj, "exists", return_value=exists),
            patch.object(obj, "cmd") as mock_cmd,
        ):
            mock_proc = AsyncMock()
            mock_proc.wait = AsyncMock(return_value=(0 if exists else 1))
            mock_cmd.return_value = mock_proc
            await obj.create()
            assert len(mock_cmd.call_args_list) == (0 if exists else 3)

    async def test_ssh_key_pair_exists_both_present(self, cls):
        obj = cls()
        with patch.object(obj, "cmd") as mock_cmd:
            mock_proc1 = AsyncMock()
            mock_proc1.wait = AsyncMock(return_value=0)
            mock_proc2 = AsyncMock()
            mock_proc2.wait = AsyncMock(return_value=0)
            mock_cmd.side_effect = [mock_proc1, mock_proc2]
            result = await obj.exists()
            assert result is True
            assert mock_cmd.call_count == 2

    async def test_ssh_key_pair_exists_first_missing(self, cls):
        obj = cls()
        with patch.object(obj, "cmd") as mock_cmd:
            mock_proc1 = AsyncMock()
            mock_proc1.wait = AsyncMock(return_value=1)
            mock_cmd.return_value = mock_proc1
            result = await obj.exists()
            assert result is False

    async def test_ssh_key_pair_exists_second_missing(self, cls):
        obj = cls()
        with patch.object(obj, "cmd") as mock_cmd:
            mock_proc1 = AsyncMock()
            mock_proc1.wait = AsyncMock(return_value=0)
            mock_proc2 = AsyncMock()
            mock_proc2.wait = AsyncMock(return_value=1)
            mock_cmd.side_effect = [mock_proc1, mock_proc2]
            result = await obj.exists()
            assert result is False

    async def test_ssh_key_pair_exists_when_already_exists(self, cls):
        obj = cls()
        with (
            patch.object(obj, "exists") as mock_exists,
            patch.object(obj, "_get_ssh_keys") as mock_get_keys,
            patch.object(obj, "cmd") as mock_cmd,
        ):
            mock_exists.return_value = True
            await obj.create()
            mock_exists.assert_called_once()
            mock_get_keys.assert_not_called()
            mock_cmd.assert_not_called()

    async def test_ssh_key_pair_remove_calls_both_commands(self, cls):
        obj = cls()
        with patch.object(obj, "cmd") as mock_cmd:
            mock_proc = AsyncMock()
            mock_proc.wait = AsyncMock(return_value=0)
            mock_cmd.return_value = mock_proc
            await obj.remove()
            assert mock_cmd.call_count == 2
