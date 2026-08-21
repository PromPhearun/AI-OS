"""Unit tests: container sandbox profile (Phase 5, Slice 5.2).

Pure-construction tests for ``build_container_command`` (no live daemon needed)
plus fail-closed behavior of the ``DockerProbe`` daemon liveness check.
"""

from __future__ import annotations

import asyncio

import pytest

from aios_kernel.modules.sandbox import (
    DEFAULT_SANDBOX_IMAGE,
    DockerProbe,
    build_container_command,
    docker_cli_env,
    has_metacharacters,
)

WS = "/tmp/ws-42"


def _cmd(**kw) -> list[str]:
    return build_container_command(workspace=WS, argv=["echo", "hi"], **kw)


# ------------------------------------------------------------ command shape
def test_container_command_hardening_flags() -> None:
    cmd = _cmd()
    assert cmd[0] == "docker" and cmd[1] == "run"
    assert "--rm" in cmd
    assert "--read-only" in cmd
    assert cmd[cmd.index("--cap-drop") + 1] == "ALL"
    secopts = [cmd[i + 1] for i in range(len(cmd)) if cmd[i] == "--security-opt"]
    assert "no-new-privileges" in secopts
    assert "seccomp=default" in secopts


def test_container_command_mounts_only_workspace_rw() -> None:
    cmd = _cmd()
    assert f"{WS}:/ws:rw" in cmd
    assert cmd[cmd.index("-w") + 1] == "/ws"
    # no other -v mounts are present
    assert cmd.count("-v") == 1


def test_container_command_network_modes() -> None:
    cmd = _cmd(network="none")
    assert cmd[cmd.index("--network") + 1] == "none"
    cmd = _cmd(network="http", proxy="http://egress.internal:3128")
    assert cmd[cmd.index("--network") + 1] == "bridge"
    assert "HTTP_PROXY=http://egress.internal:3128" in cmd
    assert "HTTPS_PROXY=http://egress.internal:3128" in cmd
    cmd = _cmd(network="all")
    assert cmd[cmd.index("--network") + 1] == "bridge"


def test_container_command_http_requires_proxy() -> None:
    with pytest.raises(ValueError, match="AIOS_EGRESS_PROXY"):
        _cmd(network="http")  # no proxy -> cannot be constructed at all


def test_container_command_rejects_proxy_with_credentials() -> None:
    with pytest.raises(ValueError, match="credentials"):
        _cmd(network="http", proxy="http://user:pass@egress.internal:3128")


def test_container_command_rejects_bad_network() -> None:
    with pytest.raises(ValueError, match="network"):
        _cmd(network="full-access")


def test_container_command_rlimits_mapping() -> None:
    cmd = _cmd(rlimits={"max_mem_mb": 256, "max_cpu_s": 5, "max_pids": 64})
    assert cmd[cmd.index("--memory") + 1] == "256m"
    assert "--cpu-period" in cmd and cmd[cmd.index("--cpu-quota") + 1] == "500000"
    assert cmd[cmd.index("--pids-limit") + 1] == "64"


def test_container_command_unknown_rlimit_keys_rejected() -> None:
    with pytest.raises(ValueError, match="unknown sandbox.rlimits"):
        _cmd(rlimits={"max_mem_mb": 256, "some_other_limit": 1})


def test_container_command_never_inherits_host_secrets() -> None:
    env = {"PATH": "/usr/bin:/bin", "AIOS_PID": "7", "DB_RO_URL": "postgres://ro@db"}
    cmd = _cmd(env=env)
    flags = [cmd[i + 1] for i in range(len(cmd)) if cmd[i] == "--env"]
    assert "DB_RO_URL=postgres://ro@db" in flags
    # every env var travels as an explicit --env flag; nothing is inherited
    assert all(flag.startswith(("PATH=", "AIOS_", "DB_")) for flag in flags)


def test_container_command_image_and_argv_are_last() -> None:
    cmd = _cmd()
    assert cmd[-3:] == [DEFAULT_SANDBOX_IMAGE, "echo", "hi"]


def test_has_metacharacters() -> None:
    for bad in ("a | b", "a;b", "a && b", "a || b", "a > f", "a < f", "a `b`", "a $(b)"):
        assert has_metacharacters(bad), bad
    assert not has_metacharacters("echo hello")


# ----------------------------------------------------------- docker_cli_env
def test_docker_cli_env_minimal(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("AIOS_MASTER_KEY", "sekret")
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
    env = docker_cli_env()
    assert "AIOS_MASTER_KEY" not in env
    assert env["DOCKER_HOST"] == "unix:///var/run/docker.sock"
    assert env["PATH"] == "/usr/bin:/bin"
    assert "HOME" in env


# --------------------------------------------------------- DockerProbe (CLI)
class _FakeProc:
    def __init__(self, code: int, *, hang: bool = False):
        self._code = code
        self._hang = hang
        self.killed = False

    async def wait(self) -> int:
        if self._hang:
            await asyncio.sleep(30)
        return self._code

    def kill(self) -> None:
        self.killed = True


@pytest.mark.asyncio
async def test_docker_probe_available_when_info_ok(monkeypatch) -> None:
    probe = DockerProbe()

    async def _spawn(*a, **k):
        return _FakeProc(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    assert await probe.available() is True
    assert probe._result is True


@pytest.mark.asyncio
async def test_docker_probe_fails_closed_when_daemon_down(monkeypatch) -> None:
    probe = DockerProbe()

    async def _spawn(*a, **k):
        return _FakeProc(1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    assert await probe.available() is False


@pytest.mark.asyncio
async def test_docker_probe_fails_closed_when_cli_missing(monkeypatch) -> None:
    probe = DockerProbe()

    async def _boom(*a, **k):
        raise FileNotFoundError("docker not found")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)
    assert await probe.available() is False


@pytest.mark.asyncio
async def test_docker_probe_timeout_is_fail_closed(monkeypatch) -> None:
    probe = DockerProbe(timeout_s=0.05)
    fake = _FakeProc(0, hang=True)

    async def _spawn(*a, **k):
        return fake

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    assert await probe.available() is False
    assert fake.killed is True


@pytest.mark.asyncio
async def test_docker_probe_caches_result(monkeypatch) -> None:
    probe = DockerProbe()
    calls = []

    async def _spawn(*a, **k):
        calls.append(a)
        return _FakeProc(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    assert await probe.available() is True
    assert await probe.available() is True
    assert len(calls) == 1  # second call served from the cache
    probe.reset()
    assert await probe.available() is True
    assert len(calls) == 2