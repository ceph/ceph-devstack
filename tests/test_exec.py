import asyncio
import os
import sys

import pytest

from ceph_devstack.exec import Command
from ceph_devstack.host import RemoteHost


def test_remote_host_uses_tty_for_streaming():
    host = RemoteHost()
    cmd = host.cmd(["python", "build.py"], stream_output=True)
    assert cmd.args == ["podman", "machine", "ssh", "--", "python", "build.py"]


def test_remote_host_no_tty_for_buffered_remote_cmd():
    host = RemoteHost()
    cmd = host.cmd(["python", "check.py"], stream_output=False)
    assert cmd.args == ["podman", "machine", "ssh", "--", "python", "check.py"]


def test_remote_host_podman_cmd_not_wrapped():
    host = RemoteHost()
    cmd = host.cmd(["podman", "build", "."], stream_output=True)
    assert cmd.args == ["podman", "build", "."]


@pytest.mark.asyncio
async def test_cancel_kills_streaming_build_process():
    script = "import time; time.sleep(3600)"
    cmd = Command([sys.executable, "-c", script], stream_output=True)
    proc = await cmd.arun()
    pid = proc.pid
    task = asyncio.create_task(proc.wait())
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.asyncio
async def test_stream_output_writes_stdout_to_terminal(capfd):
    cmd = Command(
        [sys.executable, "-c", "print('build-output', flush=True)"],
        stream_output=True,
    )
    proc = await cmd.arun()
    assert await proc.wait() == 0
    captured = capfd.readouterr()
    assert "build-output" in captured.out
    assert captured.err == ""


@pytest.mark.asyncio
async def test_stream_output_writes_stderr_to_terminal(capfd):
    cmd = Command(
        [
            sys.executable,
            "-c",
            "import sys; print('build-error', file=sys.stderr, flush=True)",
        ],
        stream_output=True,
    )
    proc = await cmd.arun()
    assert await proc.wait() == 0
    captured = capfd.readouterr()
    assert captured.out == ""
    assert "build-error" in captured.err


@pytest.mark.asyncio
async def test_stream_output_does_not_deadlock_on_large_output(capfd):
    cmd = Command(
        [
            sys.executable,
            "-c",
            "print('x' * 100_000, flush=True); print('done', flush=True)",
        ],
        stream_output=True,
    )
    proc = await cmd.arun()
    await asyncio.wait_for(proc.wait(), timeout=5)
    captured = capfd.readouterr()
    assert "done" in captured.out


@pytest.mark.asyncio
async def test_buffered_output_not_written_to_terminal(capfd):
    cmd = Command(
        [sys.executable, "-c", "print('hidden', flush=True)"],
        stream_output=False,
    )
    proc = await cmd.arun()
    assert await proc.wait() == 0
    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert proc.stdout is not None
    assert (await proc.stdout.read()).decode() == "hidden\n"


@pytest.mark.asyncio
async def test_subprocess_transport_closed_on_cancel():
    cmd = Command(
        [sys.executable, "-c", "import time; time.sleep(3600)"],
        stream_output=True,
    )
    proc = await cmd.arun()
    task = asyncio.create_task(proc.wait())
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert proc.returncode is not None
    assert proc._transport.is_closing()


def test_subprocess_cancel_does_not_error_on_loop_shutdown():
    async def run_build():
        cmd = Command(
            [sys.executable, "-c", "import time; time.sleep(3600)"],
            stream_output=True,
        )
        proc = await cmd.arun()
        await proc.wait()
        return proc.pid

    with pytest.raises(TimeoutError):
        asyncio.run(asyncio.wait_for(run_build(), timeout=0.2))


@pytest.mark.asyncio
async def test_terminate_kills_child_in_process_group(tmp_path):
    child_pid_file = tmp_path / "child.pid"
    script = (
        "import subprocess, sys, time\n"
        f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3600)'])\n"
        f"open({str(child_pid_file)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(3600)\n"
    )
    cmd = Command([sys.executable, "-c", script], stream_output=True)
    proc = await cmd.arun()

    child_pid = None
    for _ in range(20):
        if child_pid_file.is_file():
            child_pid = int(child_pid_file.read_text())
            break
        await asyncio.sleep(0.05)
    assert child_pid is not None

    task = asyncio.create_task(proc.wait())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
