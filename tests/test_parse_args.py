import pytest

from ceph_devstack import parse_args

from pathlib import Path


class TestParseArgs:
    def test_parse_args_no_args(self):
        args = parse_args([])
        assert args.command is None
        assert args.verbose is False
        assert args.dry_run is False

    def test_parse_args_verbose(self):
        args = parse_args(["-v"])
        assert args.verbose is True

    def test_parse_args_dry_run(self):
        args = parse_args(["--dry-run"])
        assert args.dry_run is True

    def test_parse_args_config_file(self):
        args = parse_args(["-c", "/custom/path.toml"])
        assert args.config_file == Path("/custom/path.toml")

    def test_parse_args_config_dump(self):
        args = parse_args(["config", "dump"])
        assert args.command == "config"
        assert args.config_op == "dump"

    def test_parse_args_config_get(self):
        args = parse_args(["config", "get", "data_dir"])
        assert args.command == "config"
        assert args.config_op == "get"
        assert args.name == "data_dir"

    def test_parse_args_config_set(self):
        args = parse_args(["config", "set", "data_dir", "/custom/path"])
        assert args.command == "config"
        assert args.config_op == "set"
        assert args.name == "data_dir"
        assert args.value == "/custom/path"

    def test_parse_args_doctor(self):
        args = parse_args(["doctor"])
        assert args.command == "doctor"

    def test_parse_args_doctor_fix(self):
        args = parse_args(["doctor", "--fix"])
        assert args.command == "doctor"
        assert args.fix is True

    def test_parse_args_pull(self):
        args = parse_args(["pull"])
        assert args.command == "pull"

    def test_parse_args_pull_with_images(self):
        args = parse_args(["pull", "image1", "image2"])
        assert args.command == "pull"
        assert "image1" in args.image
        assert "image2" in args.image

    def test_parse_args_build(self):
        args = parse_args(["build"])
        assert args.command == "build"

    def test_parse_args_build_with_images(self):
        args = parse_args(["build", "image1"])
        assert args.command == "build"
        assert "image1" in args.image

    def test_parse_args_create(self):
        args = parse_args(["create"])
        assert args.command == "create"

    def test_parse_args_create_with_build(self):
        args = parse_args(["create", "-b"])
        assert args.command == "create"
        assert args.build is True

    def test_parse_args_create_with_wait(self):
        args = parse_args(["create", "-w"])
        assert args.command == "create"
        assert args.wait is True

    def test_parse_args_remove(self):
        args = parse_args(["remove"])
        assert args.command == "remove"

    def test_parse_args_start(self):
        args = parse_args(["start"])
        assert args.command == "start"

    def test_parse_args_stop(self):
        args = parse_args(["stop"])
        assert args.command == "stop"

    def test_parse_args_watch(self):
        args = parse_args(["watch"])
        assert args.command == "watch"

    def test_parse_args_wait(self):
        args = parse_args(["wait", "teuthology"])
        assert args.command == "wait"
        assert args.container == "teuthology"

    def test_parse_args_logs(self):
        args = parse_args(["logs"])
        assert args.command == "logs"

    def test_parse_args_logs_with_run_name(self):
        args = parse_args(["logs", "-r", "my-run"])
        assert args.command == "logs"
        assert args.run_name == "my-run"

    def test_parse_args_logs_with_job_id(self):
        args = parse_args(["logs", "-j", "42"])
        assert args.command == "logs"
        assert args.job_id == "42"

    def test_parse_args_logs_with_locate(self):
        args = parse_args(["logs", "--locate"])
        assert args.command == "logs"
        assert args.locate is True

    def test_parse_args_logs_with_no_locate(self):
        args = parse_args(["logs", "--no-locate"])
        assert args.command == "logs"
        assert args.locate is False

    def test_parse_args_logs_with_all_options(self):
        args = parse_args(["logs", "-r", "my-run", "-j", "42", "--locate"])
        assert args.command == "logs"
        assert args.run_name == "my-run"
        assert args.job_id == "42"
        assert args.locate is True


class TestParseArgsDefaults:
    def test_parse_args_default_verbose_false(self):
        args = parse_args([])
        assert args.verbose is False

    def test_parse_args_default_dry_run_false(self):
        args = parse_args([])
        assert args.dry_run is False

    def test_parse_args_default_config_path(self):
        args = parse_args([])
        assert args.config_file == Path("~/.config/ceph-devstack/config.toml")

    def test_parse_args_default_command_is_none(self):
        args = parse_args([])
        assert args.command is None

    def test_parse_args_default_config_op_is_none(self):
        args = parse_args(["doctor"])
        assert not hasattr(args, "config_op") or args.config_op is None

    def test_parse_args_create_default_build_false(self):
        args = parse_args(["create"])
        assert args.build is False

    def test_parse_args_create_default_wait_false(self):
        args = parse_args(["create"])
        assert args.wait is False

    def test_parse_args_doctor_default_fix_false(self):
        args = parse_args(["doctor"])
        assert args.fix is False

    def test_parse_args_logs_default_run_name_none(self):
        args = parse_args(["logs"])
        assert args.run_name is None

    def test_parse_args_logs_default_job_id_none(self):
        args = parse_args(["logs"])
        assert args.job_id is None


class TestParseArgsEdgeCases:
    def test_parse_args_with_help(self):
        with pytest.raises(SystemExit):
            parse_args(["--help"])

    def test_parse_args_with_subcommand_help(self):
        with pytest.raises(SystemExit):
            parse_args(["config", "--help"])

    def test_parse_args_with_invalid_flag(self):
        with pytest.raises(SystemExit):
            parse_args(["--invalid-flag"])

    def test_parse_args_config_dump_requires_no_args(self):
        args = parse_args(["config", "dump"])
        assert args.config_op == "dump"

    def test_parse_args_config_get_requires_name(self):
        args = parse_args(["config", "get", "test_name"])
        assert args.config_op == "get"
        assert args.name == "test_name"

    def test_parse_args_config_set_requires_name_and_value(self):
        args = parse_args(["config", "set", "test_name", "test_value"])
        assert args.config_op == "set"
        assert args.name == "test_name"
        assert args.value == "test_value"

    def test_parse_args_wait_requires_container_name(self):
        args = parse_args(["wait", "teuthology"])
        assert args.command == "wait"
        assert args.container == "teuthology"

    def test_parse_args_pull_accepts_multiple_images(self):
        args = parse_args(["pull", "img1", "img2", "img3"])
        assert args.command == "pull"
        assert len(args.image) == 3
        assert "img1" in args.image
        assert "img2" in args.image
        assert "img3" in args.image

    def test_parse_args_build_accepts_multiple_images(self):
        args = parse_args(["build", "img1", "img2"])
        assert args.command == "build"
        assert len(args.image) == 2
        assert "img1" in args.image
        assert "img2" in args.image
