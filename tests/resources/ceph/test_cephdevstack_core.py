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
        assert devstack.stack_name == "teuthology"
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

    def test_service_specs_count_attribute(self):
        devstack = CephDevStack()
        assert devstack.service_specs["postgres"]["count"] == 1
        assert devstack.service_specs["testnode"]["count"] == 3


class TestCephDevStackApply:
    async def test_apply_calls_correct_method(self):
        devstack = CephDevStack()
        with patch.object(devstack, "pull", new_callable=AsyncMock) as mock_pull:
            await devstack.apply("pull")
            assert mock_pull.called is True

    async def test_apply_calls_create(self):
        devstack = CephDevStack()
        with patch.object(devstack, "create", new_callable=AsyncMock) as mock_create:
            await devstack.apply("create")
            assert mock_create.called is True

    async def test_apply_calls_start(self):
        devstack = CephDevStack()
        with patch.object(devstack, "start", new_callable=AsyncMock) as mock_start:
            await devstack.apply("start")
            assert mock_start.called is True


class TestCephDevStackPull:
    async def test_pull_calls_pull_on_all_services(self):
        devstack = CephDevStack()
        # Override service_specs to control the objects
        mock_postgres = AsyncMock()
        mock_paddles = AsyncMock()
        devstack.service_specs = {
            "postgres": {"count": 1, "objects": [mock_postgres]},
            "paddles": {"count": 1, "objects": [mock_paddles]},
        }
        with patch("ceph_devstack.logger.info"):
            await devstack.pull()
            mock_postgres.pull.assert_called_once()
            mock_paddles.pull.assert_called_once()


class TestCephDevStackBuild:
    async def test_build_calls_build_on_all_services(self):
        devstack = CephDevStack()
        mock_postgres = AsyncMock()
        mock_paddles = AsyncMock()
        devstack.service_specs = {
            "postgres": {"count": 1, "objects": [mock_postgres]},
            "paddles": {"count": 1, "objects": [mock_paddles]},
        }
        with patch("ceph_devstack.logger.info"):
            await devstack.build()
            mock_postgres.build.assert_called_once()
            mock_paddles.build.assert_called_once()


class TestCephDevStackGetLogFile:
    def test_get_log_file_with_run_name_and_job_id(self, tmp_path):
        devstack = CephDevStack()
        archive_dir = tmp_path / "archive"
        archive_dir.mkdir()
        run_name = "root-2025-01-01_00:00:00-orch:cephadm:smoke-small-main-distro-default-testnode"
        run_dir = archive_dir / run_name
        run_dir.mkdir()
        job_dir = run_dir / "42"
        job_dir.mkdir()
        log_file = job_dir / "teuthology.log"
        log_file.write_text("test log content")

        # Mock Teuthology to return our test archive_dir
        with patch("ceph_devstack.resources.ceph.Teuthology") as MockTeuthology:
            mock_teuthology = MagicMock()
            mock_teuthology.archive_dir = archive_dir
            MockTeuthology.return_value = mock_teuthology
            result = devstack.get_log_file(run_name, "42")
            assert str(result) == str(log_file)

    def test_get_log_file_with_run_name_only(self, tmp_path):
        devstack = CephDevStack()
        archive_dir = tmp_path / "archive"
        archive_dir.mkdir()
        run_name = "root-2025-01-01_00:00:00-orch:cephadm:smoke-small-main-distro-default-testnode"
        run_dir = archive_dir / run_name
        run_dir.mkdir()
        job_dir = run_dir / "1"
        job_dir.mkdir()
        log_file = job_dir / "teuthology.log"
        log_file.write_text("test log content")

        with patch("ceph_devstack.resources.ceph.Teuthology") as MockTeuthology:
            mock_teuthology = MagicMock()
            mock_teuthology.archive_dir = archive_dir
            MockTeuthology.return_value = mock_teuthology
            result = devstack.get_log_file(run_name, "")
            assert str(result) == str(log_file)

    def test_get_log_file_raises_file_not_found_for_missing_job(self, tmp_path):
        devstack = CephDevStack()
        archive_dir = tmp_path / "archive"
        archive_dir.mkdir()
        run_name = "root-2025-01-01_00:00:00-orch:cephadm:smoke-small-main-distro-default-testnode"
        run_dir = archive_dir / run_name
        run_dir.mkdir()

        with patch("ceph_devstack.resources.ceph.Teuthology") as MockTeuthology:
            mock_teuthology = MagicMock()
            mock_teuthology.archive_dir = archive_dir
            MockTeuthology.return_value = mock_teuthology
            with pytest.raises(FileNotFoundError):
                devstack.get_log_file(run_name, "99")

    def test_get_log_file_raises_file_not_found_for_missing_log(self, tmp_path):
        devstack = CephDevStack()
        archive_dir = tmp_path / "archive"
        archive_dir.mkdir()
        run_name = "root-2025-01-01_00:00:00-orch:cephadm:smoke-small-main-distro-default-testnode"
        run_dir = archive_dir / run_name
        run_dir.mkdir()
        job_dir = run_dir / "1"
        job_dir.mkdir()

        with patch("ceph_devstack.resources.ceph.Teuthology") as MockTeuthology:
            mock_teuthology = MagicMock()
            mock_teuthology.archive_dir = archive_dir
            MockTeuthology.return_value = mock_teuthology
            with pytest.raises(FileNotFoundError):
                devstack.get_log_file(run_name, "1")

    def test_get_log_file_uses_most_recent_when_no_run_name(
        self, tmp_path, create_log_file
    ):
        config["data_dir"] = str(tmp_path)
        create_log_file(
            tmp_path, timestamp=datetime(year=2024, month=1, day=1), content="old log"
        )
        new_log_file = create_log_file(
            tmp_path, timestamp=datetime(year=2025, month=1, day=1), content="new log"
        )
        devstack = CephDevStack()
        result = devstack.get_log_file("", "")
        assert str(result) == str(new_log_file)

    def test_get_log_file_returns_latest_job_log_when_multiple_and_no_job_id(
        self, tmp_path
    ):
        devstack = CephDevStack()
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

        with patch("ceph_devstack.resources.ceph.Teuthology") as MockTeuthology:
            mock_teuthology = MagicMock()
            mock_teuthology.archive_dir = archive_dir
            MockTeuthology.return_value = mock_teuthology
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


class TestCephDevStackStop:
    async def test_stop_calls_stop_on_all_containers(self):
        devstack = CephDevStack()
        mock_postgres = AsyncMock()
        mock_paddles = AsyncMock()
        devstack.service_specs = {
            "postgres": {"count": 1, "objects": [mock_postgres]},
            "paddles": {"count": 1, "objects": [mock_paddles]},
        }
        with patch("ceph_devstack.logger.info"):
            await devstack.stop()
            mock_postgres.stop.assert_called_once()
            mock_paddles.stop.assert_called_once()


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
        with patch("ceph_devstack.resources.ceph.Teuthology") as MockTeuthology:
            mock_teuthology = MagicMock()
            mock_teuthology.archive_dir = archive_dir
            MockTeuthology.return_value = mock_teuthology

            def mock_listdir(path):
                if str(path) == str(archive_dir):
                    return [run_name]
                if str(path) == str(run_dir):
                    return ["1"]
                return []

            with (
                patch("os.listdir", side_effect=mock_listdir),
                contextlib.redirect_stdout(f),
            ):
                await devstack.logs(locate=True)
        output = f.getvalue()
        assert str(log_file) in output

    async def test_logs_with_locate_false(self, tmp_path):
        devstack = CephDevStack()
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
        with patch("ceph_devstack.resources.ceph.Teuthology") as MockTeuthology:
            mock_teuthology = MagicMock()
            mock_teuthology.archive_dir = archive_dir
            MockTeuthology.return_value = mock_teuthology

            def mock_listdir(path):
                if str(path) == str(archive_dir):
                    return [run_name]
                if str(path) == str(run_dir):
                    return ["1"]
                return []

            with (
                patch("os.listdir", side_effect=mock_listdir),
                contextlib.redirect_stdout(f),
            ):
                await devstack.logs(locate=False)
        output = f.getvalue()
        assert "test log content" in output

    async def test_logs_with_missing_file_shows_error(self, tmp_path, caplog):
        devstack = CephDevStack()
        archive_dir = tmp_path / "archive"
        archive_dir.mkdir()
        run_name = "root-2025-01-01_00:00:00-orch:cephadm:smoke-small-main-distro-default-testnode"
        run_dir = archive_dir / run_name
        run_dir.mkdir()

        with patch("ceph_devstack.resources.ceph.Teuthology") as MockTeuthology:
            mock_teuthology = MagicMock()
            mock_teuthology.archive_dir = archive_dir
            MockTeuthology.return_value = mock_teuthology

            def mock_listdir(path):
                if str(path) == str(archive_dir):
                    return [run_name]
                if str(path) == str(run_dir):
                    return ["1"]
                return []

            with patch("os.listdir", side_effect=mock_listdir):
                await devstack.logs()
        assert "No log file found" in caplog.text


class TestCephDevStackInit:
    def test_init_without_postgres(self):
        config["containers"] = {
            "postgres": {"image": "postgres:latest", "count": 0},
            "paddles": {"image": "paddles:latest", "count": 1},
            "beanstalk": {"image": "beanstalk:latest", "count": 1},
            "pulpito": {"image": "pulpito:latest", "count": 1},
            "testnode": {
                "image": "testnode:latest",
                "count": 3,
                "loop_device_count": 1,
            },
            "teuthology": {"image": "teuthology:latest", "count": 1},
            "archive": {"image": "archive:latest", "count": 1},
        }
        devstack = CephDevStack()
        assert "archive" in devstack.service_specs
        assert "postgres" not in devstack.service_specs


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
            # Verify that CephBuilder.start() was called (which triggers compilation)
            mock_builder.start.assert_awaited_once()
