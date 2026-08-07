from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ceph_devstack import config
from ceph_devstack.resources.ceph import CephDevStack
from ceph_devstack.resources.ceph.containers import (
    Archive,
    Beanstalk,
    Paddles,
    Postgres,
    Pulpito,
    TestNode as _TestNode,
    Teuthology,
)


class TestCephDevStackServiceSpecs:
    def test_service_specs_includes_all_services(self):
        devstack = CephDevStack()
        assert "postgres" in devstack.service_specs
        assert "paddles" in devstack.service_specs
        assert "beanstalk" in devstack.service_specs
        assert "pulpito" in devstack.service_specs
        assert "testnode" in devstack.service_specs
        assert "teuthology" in devstack.service_specs
        assert "archive" in devstack.service_specs

    def test_service_specs_single_count_creates_single_object(self):
        config["containers"]["postgres"]["count"] = 1
        devstack = CephDevStack()
        assert len(devstack.service_specs["postgres"]["objects"]) == 1

    def test_service_specs_multiple_count_creates_multiple_objects(self):
        assert config["containers"]["testnode"]["count"] == 3
        devstack = CephDevStack()
        assert len(devstack.service_specs["testnode"]["objects"]) == 3

    def test_service_specs_zero_count_excludes_service(self):
        config["containers"]["beanstalk"]["count"] = 0
        devstack = CephDevStack()
        assert "beanstalk" not in devstack.service_specs

    def test_service_specs_objects_are_correct_types(self):
        devstack = CephDevStack()
        assert isinstance(devstack.service_specs["postgres"]["objects"][0], Postgres)
        assert isinstance(devstack.service_specs["paddles"]["objects"][0], Paddles)
        assert isinstance(devstack.service_specs["beanstalk"]["objects"][0], Beanstalk)
        assert isinstance(devstack.service_specs["pulpito"]["objects"][0], Pulpito)
        assert isinstance(devstack.service_specs["testnode"]["objects"][0], _TestNode)
        assert isinstance(
            devstack.service_specs["teuthology"]["objects"][0], Teuthology
        )
        assert isinstance(devstack.service_specs["archive"]["objects"][0], Archive)

    def test_service_specs_named_objects_when_count_greater_than_one(self):
        devstack = CephDevStack()
        testnode_objects = devstack.service_specs["testnode"]["objects"]
        assert testnode_objects[0].name == "testnode_0"
        assert testnode_objects[1].name == "testnode_1"
        assert testnode_objects[2].name == "testnode_2"

    def test_multiple_testnodes_get_distinct_loop_devices(self, tmp_path):
        config["data_dir"] = str(tmp_path)
        devstack = CephDevStack()
        testnode_objects = devstack.service_specs["testnode"]["objects"]
        all_devices = []
        for node in testnode_objects:
            all_devices.extend(node.devices)
        assert len(all_devices) == len(set(all_devices)), (
            f"Loop device collision: {all_devices}"
        )

    def test_service_specs_sets_postgres_paddles_url(self):
        devstack = CephDevStack()
        paddles_obj = devstack.service_specs["paddles"]["objects"][0]
        assert "PADDLES_SQLALCHEMY_URL" in paddles_obj.env_vars
        assert (
            "postgresql+psycopg2://admin:password@postgres:5432/paddles"
            in paddles_obj.env_vars["PADDLES_SQLALCHEMY_URL"]
        )

    def test_service_specs_does_not_set_postgres_url_when_no_postgres(self):
        config["containers"]["postgres"]["count"] = 0
        devstack = CephDevStack()
        paddles_obj = devstack.service_specs["paddles"]["objects"][0]
        assert "PADDLES_SQLALCHEMY_URL" not in paddles_obj.env_vars


class TestCephDevStackApply:
    @pytest.mark.parametrize("action", ["pull", "create", "start"])
    async def test_apply_dispatches_to_method(self, action):
        devstack = CephDevStack()
        with patch.object(devstack, action, new_callable=AsyncMock) as mock:
            await devstack.apply(action)
            assert mock.called is True


@pytest.mark.parametrize("action", ["pull", "build", "stop"])
class TestCephDevStackServiceDispatch:
    async def test_action_dispatches_to_all_services(self, action):
        devstack = CephDevStack()
        mock_postgres = AsyncMock()
        mock_paddles = AsyncMock()
        devstack.service_specs = {
            "postgres": {"count": 1, "objects": [mock_postgres]},
            "paddles": {"count": 1, "objects": [mock_paddles]},
        }
        with patch("ceph_devstack.logger.info"):
            await getattr(devstack, action)()
            getattr(mock_postgres, action).assert_called_once()
            getattr(mock_paddles, action).assert_called_once()


class TestCephDevStackGetLogFile:
    def test_get_log_file_with_run_name_and_job_id(self, tmp_path):
        devstack = CephDevStack()
        devstack.data_dir = tmp_path
        archive_dir = tmp_path / "archive"
        archive_dir.mkdir()
        run_name = "root-2025-01-01_00:00:00-orch:cephadm:smoke-small-main-distro-default-testnode"
        run_dir = archive_dir / run_name
        run_dir.mkdir()
        job_dir = run_dir / "42"
        job_dir.mkdir()
        log_file = job_dir / "teuthology.log"
        log_file.write_text("test log content")

        result = devstack.get_log_file(run_name, "42")
        assert str(result) == str(log_file)

    def test_get_log_file_with_run_name_only(self, tmp_path):
        devstack = CephDevStack()
        devstack.data_dir = tmp_path
        archive_dir = tmp_path / "archive"
        archive_dir.mkdir()
        run_name = "root-2025-01-01_00:00:00-orch:cephadm:smoke-small-main-distro-default-testnode"
        run_dir = archive_dir / run_name
        run_dir.mkdir()
        job_dir = run_dir / "1"
        job_dir.mkdir()
        log_file = job_dir / "teuthology.log"
        log_file.write_text("test log content")

        result = devstack.get_log_file(run_name, "")
        assert str(result) == str(log_file)

    def test_get_log_file_raises_file_not_found_for_missing_job(self, tmp_path):
        devstack = CephDevStack()
        devstack.data_dir = tmp_path
        archive_dir = tmp_path / "archive"
        archive_dir.mkdir()
        run_name = "root-2025-01-01_00:00:00-orch:cephadm:smoke-small-main-distro-default-testnode"
        run_dir = archive_dir / run_name
        run_dir.mkdir()

        with pytest.raises(FileNotFoundError):
            devstack.get_log_file(run_name, "99")

    def test_get_log_file_raises_file_not_found_for_missing_log(self, tmp_path):
        devstack = CephDevStack()
        devstack.data_dir = tmp_path
        archive_dir = tmp_path / "archive"
        archive_dir.mkdir()
        run_name = "root-2025-01-01_00:00:00-orch:cephadm:smoke-small-main-distro-default-testnode"
        run_dir = archive_dir / run_name
        run_dir.mkdir()
        job_dir = run_dir / "1"
        job_dir.mkdir()

        with pytest.raises(FileNotFoundError):
            devstack.get_log_file(run_name, "1")

    def test_get_log_file_uses_most_recent_when_no_run_name(
        self, tmp_path, create_log_file
    ):
        config["data_dir"] = str(tmp_path)
        devstack = CephDevStack()
        create_log_file(
            devstack.data_dir,
            timestamp=datetime(year=2024, month=1, day=1),
            content="old log",
        )
        new_log_file = create_log_file(
            devstack.data_dir,
            timestamp=datetime(year=2025, month=1, day=1),
            content="new log",
        )
        result = devstack.get_log_file("", "")
        assert str(result) == str(new_log_file)

    def test_get_log_file_returns_latest_job_log_when_multiple_and_no_job_id(
        self, tmp_path
    ):
        devstack = CephDevStack()
        devstack.data_dir = tmp_path
        archive_dir = tmp_path / "archive"
        archive_dir.mkdir()

        run_name = "root-2025-01-01_00:00:00-orch:cephadm:smoke-small-main-distro-default-testnode"
        run_dir = archive_dir / run_name
        run_dir.mkdir()

        job1_dir = run_dir / "1"
        job1_dir.mkdir()
        job1_log = job1_dir / "teuthology.log"
        job1_log.write_text("job 1 log")

        job2_dir = run_dir / "2"
        job2_dir.mkdir()
        job2_log = job2_dir / "teuthology.log"
        job2_log.write_text("job 2 log")

        assert devstack.get_log_file(run_name, "").parent.name == "2"


class TestCephDevStackRemove:
    async def test_remove_calls_remove_on_all_containers(self):
        devstack = CephDevStack()
        mock_postgres = AsyncMock()
        mock_paddles = AsyncMock()
        devstack.service_specs = {
            "postgres": {"count": 1, "objects": [mock_postgres]},
            "paddles": {"count": 1, "objects": [mock_paddles]},
        }
        with patch("ceph_devstack.resources.ceph.CephDevStackNetwork") as MockNetwork:
            mock_network_instance = MagicMock()
            mock_network_instance.remove = AsyncMock()
            MockNetwork.return_value = mock_network_instance
            mock_secret = MagicMock()
            mock_secret.remove = AsyncMock()
            devstack.secrets = [MagicMock(return_value=mock_secret)]
            with patch("ceph_devstack.logger.info"):
                await devstack.remove()
                mock_postgres.remove.assert_called_once()
                mock_paddles.remove.assert_called_once()
                mock_network_instance.remove.assert_called_once()
                mock_secret.remove.assert_called_once()


class TestCephDevStackWait:
    async def test_wait_returns_process_id(self):
        devstack = CephDevStack()
        mock_container = AsyncMock()
        mock_container.name = "teuthology"
        mock_container.wait = AsyncMock(return_value=42)
        devstack.service_specs = {
            "teuthology": {"count": 1, "objects": [mock_container]},
        }
        result = await devstack.wait("teuthology")
        assert result == 42

    async def test_wait_returns_one_for_nonexistent_container(self):
        devstack = CephDevStack()
        mock_container = AsyncMock()
        mock_container.name = "teuthology"
        devstack.service_specs = {
            "teuthology": {"count": 1, "objects": [mock_container]},
        }
        result = await devstack.wait("nonexistent")
        assert result == 1


class TestCephDevStackLogs:
    async def test_logs_with_locate_true(self, tmp_path):
        devstack = CephDevStack()
        devstack.data_dir = tmp_path
        archive_dir = tmp_path / "archive"
        archive_dir.mkdir()
        run_name = "root-2025-01-01_00:00:00-orch:cephadm:smoke-small-main-distro-default-testnode"
        run_dir = archive_dir / run_name
        run_dir.mkdir()
        job_dir = run_dir / "1"
        job_dir.mkdir()
        log_file = job_dir / "teuthology.log"
        log_file.write_text("test log content")

        import contextlib
        import io

        f = io.StringIO()
        with contextlib.redirect_stdout(f):
            await devstack.logs(locate=True)
        output = f.getvalue()
        assert str(log_file) in output

    async def test_logs_with_locate_false(self, tmp_path):
        devstack = CephDevStack()
        devstack.data_dir = tmp_path
        archive_dir = tmp_path / "archive"
        archive_dir.mkdir()
        run_name = "root-2025-01-01_00:00:00-orch:cephadm:smoke-small-main-distro-default-testnode"
        run_dir = archive_dir / run_name
        run_dir.mkdir()
        job_dir = run_dir / "1"
        job_dir.mkdir()
        log_file = job_dir / "teuthology.log"
        log_file.write_text("test log content")

        import contextlib
        import io

        f = io.StringIO()
        with contextlib.redirect_stdout(f):
            await devstack.logs(locate=False)
        output = f.getvalue()
        assert "test log content" in output

    async def test_logs_with_missing_file_shows_error(self, tmp_path, caplog):
        devstack = CephDevStack()
        devstack.data_dir = tmp_path
        archive_dir = tmp_path / "archive"
        archive_dir.mkdir()

        await devstack.logs()
        assert "No log file found" in caplog.text


class TestCephDevStackStacks:
    def test_custom_stack_limits_services(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            """
[stacks.minimal]
services = ["postgres", "paddles"]
"""
        )
        config.load(config_file)
        devstack = CephDevStack(stack_name="minimal")
        assert devstack.stack_name == "minimal"
        assert set(devstack.service_specs) == {"postgres", "paddles"}

    def test_ceph_stack_has_expected_services(self):
        devstack = CephDevStack(stack_name="ceph")
        assert devstack.stack_name == "ceph"
        assert set(devstack.service_specs) == {"ceph_node"}
        assert devstack.secrets == []

    async def test_ceph_stack_create_prepares_node(self):
        devstack = CephDevStack(stack_name="ceph")
        mock_node = AsyncMock()
        devstack.service_specs = {
            "ceph_node": {"count": 1, "objects": [mock_node]},
        }
        with patch("ceph_devstack.resources.ceph.CephDevStackNetwork") as MockNetwork:
            mock_network = MagicMock()
            mock_network.create = AsyncMock()
            MockNetwork.return_value = mock_network
            await devstack.create()
            mock_node.create.assert_awaited_once()

    async def test_build_ceph_stack_start_calls_builder_start(self):
        """Verify that CephBuilder.start() is called during build-ceph stack start."""
        devstack = CephDevStack(stack_name="build-ceph")
        mock_builder = AsyncMock()
        devstack.service_specs = {
            "ceph_builder": {"count": 1, "objects": [mock_builder]},
        }
        with patch("ceph_devstack.resources.ceph.CephDevStackNetwork") as MockNetwork:
            mock_network = MagicMock()
            mock_network.create = AsyncMock()
            MockNetwork.return_value = mock_network
            await devstack.start()
            mock_builder.start.assert_awaited_once()


class TestCephDevStackDepends:
    async def test_run_depends_runs_dependency_stack(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            """
[stacks.dep]
services = ["beanstalk"]

[stacks.main]
services = ["postgres"]
depends = ["dep"]
"""
        )
        config.load(config_file)
        devstack = CephDevStack(stack_name="main")
        started_stacks = []
        original_run_depends = CephDevStack._run_depends

        async def tracking_start(dep_self):
            started_stacks.append(dep_self.stack_name)

        with patch.object(CephDevStack, "start", tracking_start):
            await original_run_depends(devstack)
        assert "dep" in started_stacks

    async def test_run_depends_restores_active_stack(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            """
[stacks.dep]
services = ["beanstalk"]

[stacks.main]
services = ["postgres"]
depends = ["dep"]
"""
        )
        config.load(config_file)
        devstack = CephDevStack(stack_name="main")
        with patch.object(CephDevStack, "start", new_callable=AsyncMock):
            original_run_depends = CephDevStack._run_depends
            await original_run_depends(devstack)
        assert devstack.stack_name == "main"

    async def test_run_depends_noop_when_no_depends(self):
        devstack = CephDevStack(stack_name="ceph")
        await devstack._run_depends()
        assert devstack.stack_name == "ceph"

    async def test_stop_and_remove_do_not_cascade_depends(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            """
[stacks.dep]
services = ["beanstalk"]

[stacks.main]
services = ["postgres"]
depends = ["dep"]
"""
        )
        config.load(config_file)
        devstack = CephDevStack(stack_name="main")
        mock_service = AsyncMock()
        devstack.service_specs = {
            "postgres": {"count": 1, "objects": [mock_service]},
        }
        with (
            patch("ceph_devstack.resources.ceph.CephDevStackNetwork") as MockNetwork,
            patch("ceph_devstack.logger.info"),
        ):
            mock_network = MagicMock()
            mock_network.remove = AsyncMock()
            MockNetwork.return_value = mock_network
            await devstack.stop()
            await devstack.remove()
        mock_service.stop.assert_awaited_once()
        mock_service.remove.assert_awaited_once()
