"""Container sandbox profile — Docker/seccomp + network egress allowlist.

docs/08-security.md §4: the ``container`` profile isolates high-risk tools in a
throwaway Docker container. ``shell.run`` for an agent whose spec declares
``sandbox.profile = \"container\"`` is executed as ``docker run`` with:

  * a **read-only root filesystem**, every capability dropped, ``no-new-privileges``
    and Docker's default seccomp filter — escape attempts fail closed;
  * a **workspace-only mount** (``-v <workspace>:/ws:rw``, cwd ``/ws``) — no host
    paths are visible inside the container;
  * **per-spec resource limits** (``sandbox.rlimits``) mapped to ``--memory`` /
    ``--cpu-period``/``--cpu-quota`` / ``--pids-limit``; unknown keys are
    rejected (E_INVAL) rather than silently ignored — a dropped limit would be
    *less* restrictive than the spec;
  * a **network egress allowlist**:
      * ``network=none``  -> ``--network none`` (no interface at all);
      * ``network=http``  -> bridge + an operator-configured CONNECT proxy
        (``AIOS_EGRESS_PROXY``) injected as ``HTTP_PROXY``/``HTTPS_PROXY``. An
        allowlist *cannot* be enforced without a filtering proxy, so ``http``
        without one **fails closed** (E_PERM) instead of silently opening egress;
      * ``network=all``   -> unrestricted bridge egress (documented; the spec is
        an explicit per-agent opt-in).
  * **no host secrets** — only the kernel-built ``sandbox_env`` values (granted
    vault keys) are forwarded as ``--env`` flags; the docker CLI itself runs with
    a minimal environment and never inherits ``AIOS_*`` secrets.

The daemon itself is probed once per kernel (``docker info``, 3 s timeout) and
cached. When the CLI is missing or the daemon is down, container execution
**fails closed** (E_BUSY) — it never degrades to an unsandboxed subprocess.

Image policy: the image (``AIOS_SANDBOX_IMAGE``, default ``alpine:3.20``) is
pulled by the daemon on first use, so operators pre-pull it at deploy time; an
offline daemon without the image fails closed with a clear error.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from urllib.parse import urlparse

DEFAULT_SANDBOX_IMAGE = "alpine:3.20"
NETWORK_MODES = ("none", "http", "all")
KNOWN_RLIMITS = {"max_mem_mb", "max_cpu_s", "max_pids"}
_METACHARS = ("|", ";", "&&", "||", ">", "<", "`", "$(")
_CONTAINER_PATH = "/ws"
_CLI_ENV_KEYS = ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT")


def _validate_proxy(proxy: str) -> None:
    """An egress proxy must be a bare http(s) URL — no embedded credentials.

    Credentials in the proxy URL would put a secret in the audit/log surface and
    encourage operator copy-paste of secrets; a vault-backed proxy config is the
    supported path (docs/08-security.md §5).
    """
    parsed = urlparse(proxy)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(
            "AIOS_EGRESS_PROXY must be an http(s)://host[:port] URL"
        )
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(
            "AIOS_EGRESS_PROXY must not embed credentials "
            "(use a vault-backed proxy configuration)"
        )


def build_container_command(
    *,
    workspace: str,
    argv: list[str],
    network: str = "none",
    rlimits: dict | None = None,
    image: str | None = None,
    proxy: str | None = None,
    env: dict[str, str] | None = None,
    call_id: str | None = None,
    docker: str = "docker",
) -> list[str]:
    """Build the ``docker run`` argv for a sandboxed tool invocation.

    Pure and side-effect free so it is directly unit-testable without a daemon.
    Raises ``ValueError`` on any profile element that cannot be honored — the
    tool layer wraps that into an AiosError so the caller fails closed.
    """
    if not argv or not argv[0]:
        raise ValueError("container command must be a non-empty argv")
    if network not in NETWORK_MODES:
        raise ValueError(
            f"sandbox.network must be one of {sorted(NETWORK_MODES)}, got {network!r}"
        )

    limits = dict(rlimits or {})
    unknown = sorted(set(limits) - KNOWN_RLIMITS)
    if unknown:
        raise ValueError(f"unknown sandbox.rlimits keys: {', '.join(unknown)}")

    if proxy is not None:
        _validate_proxy(proxy)
    if network == "http" and proxy is None:
        raise ValueError(
            "sandbox.network='http' requires a proxy (AIOS_EGRESS_PROXY): "
            "an egress allowlist cannot be enforced without a filtering proxy"
        )

    cmd = [
        docker,
        "run",
        "--rm",
        "-i",
        "--name",
        f"aios-sandbox-{call_id or uuid.uuid4().hex[:8]}",
        "--network",
        "none" if network == "none" else "bridge",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--security-opt",
        "seccomp=default",
        "-v",
        f"{workspace}:{_CONTAINER_PATH}:rw",
        "-w",
        _CONTAINER_PATH,
    ]

    # --- resource limits (unknown keys already rejected above) --------------
    if "max_mem_mb" in limits:
        mem = int(limits["max_mem_mb"])
        if mem < 1:
            raise ValueError("rlimits.max_mem_mb must be >= 1")
        cmd += ["--memory", f"{mem}m"]
    if "max_cpu_s" in limits:
        cpu_s = float(limits["max_cpu_s"])
        if cpu_s <= 0:
            raise ValueError("rlimits.max_cpu_s must be > 0")
        # CPU-time *rate* cap: --cpu-quota <n>us per 100ms period (docker's
        # --cpus expresses a share, not seconds; total wall time is bounded by
        # the tool timeout).
        cmd += ["--cpu-period", "100000", "--cpu-quota", str(int(cpu_s * 100_000))]
    if "max_pids" in limits:
        pids = int(limits["max_pids"])
        if pids < 1:
            raise ValueError("rlimits.max_pids must be >= 1")
        cmd += ["--pids-limit", str(pids)]

    # --- egress allowlist ----------------------------------------------------
    if network == "http":
        cmd += ["--env", f"HTTP_PROXY={proxy}", "--env", f"HTTPS_PROXY={proxy}"]
        cmd += ["--env", "NO_PROXY=localhost,127.0.0.1"]

    # --- kernel-built environment (granted vault keys only) ------------------
    for key in sorted(env or {}):
        cmd += ["--env", f"{key}={env[key]}"]

    cmd += [image or DEFAULT_SANDBOX_IMAGE]
    cmd += list(argv)
    return cmd


def docker_cli_env() -> dict[str, str]:
    """Minimal environment for the docker CLI process itself.

    The CLI is trusted (operator-controlled) but it still must not inherit
    ``AIOS_*`` secrets or the operator shell's full environment — the sandbox
    boundary is the container, and this keeps host secrets off the CLI's
    process table too.
    """
    env = {}
    for key in _CLI_ENV_KEYS:
        if key in os.environ:
            env[key] = os.environ[key]
    env.setdefault("PATH", os.environ.get("PATH", "/usr/bin:/bin"))
    env.setdefault("HOME", "/tmp")
    return env


def has_metacharacters(command: str) -> bool:
    """True when a command string uses shell metacharacters (never allowed)."""
    return any(part in command for part in _METACHARS)


class DockerProbe:
    """Cheap cached liveness probe for the docker CLI + daemon (fail-closed).

    ``docker info`` exits non-zero when the daemon is unreachable; a missing CLI
    raises FileNotFoundError. Both are folded into ``available() == False`` so
    container tool calls fail closed (E_BUSY) instead of degrading.
    """

    def __init__(self, *, timeout_s: float = 3.0) -> None:
        self.timeout_s = timeout_s
        self._result: bool | None = None

    def reset(self) -> None:
        """Forget the cached result (operator re-configures the daemon)."""
        self._result = None

    async def available(self, docker: str = "docker") -> bool:
        if self._result is not None:
            return self._result
        try:
            proc = await asyncio.create_subprocess_exec(
                docker,
                "info",
                "--format",
                "{{.ServerVersion}}",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                code = await asyncio.wait_for(proc.wait(), timeout=self.timeout_s)
                self._result = code == 0
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except (ProcessLookupError, OSError):
                    pass
                self._result = False
        except (OSError, FileNotFoundError):
            self._result = False
        return self._result


__all__ = [
    "DEFAULT_SANDBOX_IMAGE",
    "NETWORK_MODES",
    "KNOWN_RLIMITS",
    "build_container_command",
    "docker_cli_env",
    "has_metacharacters",
    "DockerProbe",
]