"""aios CLI — Phase 1 control surface.

Subcommands:
    aios --version
    aios demo                    run the two example agents + ps-style table
    aios run SPEC [SPEC...]      run agents from spec files (see --agents-module)
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import sys

from aios_kernel import Kernel
from aios_sdk import run_agents

__version__ = "0.1.0"


def _load_specs(paths: list[str]) -> list[dict]:
    specs = []
    for p in paths:
        with open(p, "r", encoding="utf-8") as fh:
            specs.append(json.load(fh))
    return specs


def _load_agents_module(spec: str | None) -> None:
    """Import a module that registers @agent definitions (name:module.path)."""
    if spec:
        importlib.import_module(spec)


def _print_table(summaries) -> None:
    header = f"{'PID':>4}  {'NAME':<16} {'STATE':<11} {'EXIT':<8} {'TURNS':>5} {'TOKENS':>7} {'COST':>9} {'TOOLCALLS':>9}"
    print(header)
    print("-" * len(header))
    for s in summaries:
        print(
            f"{s.pid:>4}  {s.name:<16} {s.state:<11} {str(s.exit_status or '-'):<8} "
            f"{s.turns:>5} {s.tokens:>7} ${s.cost_usd:>8.5f} {s.tool_calls:>9}"
        )


async def _demo(kernel) -> None:
    from examples.agents import demo_specs

    summaries = await run_agents(kernel, demo_specs())
    _print_table(summaries)


async def _run(kernel, paths: list[str], agents_module: str | None, timeout: float | None) -> None:
    _load_agents_module(agents_module)
    specs = _load_specs(paths)
    summaries = await run_agents(kernel, specs, timeout=timeout)
    _print_table(summaries)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aios", description="AI OS — Phase 1 MVP kernel")
    parser.add_argument("--version", action="store_true", help="print version and exit")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("demo", help="run the bundled example agents")

    run = sub.add_parser("run", help="run agents from spec files")
    run.add_argument("specs", nargs="+", help="paths to agent spec JSON files")
    run.add_argument("--agents-module", default=None, help="module that registers @agent definitions")
    run.add_argument("--timeout", type=float, default=None, help="per-agent wait timeout (s)")

    args = parser.parse_args(argv)

    if args.version or not args.command:
        print(f"aios {__version__}")
        return 0

    async def _main():
        kernel = Kernel()
        try:
            if args.command == "demo":
                await _demo(kernel)
            elif args.command == "run":
                await _run(kernel, args.specs, args.agents_module, args.timeout)
            return 0
        finally:
            await kernel.shutdown()

    return asyncio.run(_main())


if __name__ == "__main__":
    sys.exit(main())