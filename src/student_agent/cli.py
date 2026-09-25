from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx2

from .cases import load_case_set
from .config import Settings
from .contracts import Contracts
from .mcp_gateway import connect_gateway
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter
from .workflow import solve_case


def _root(value: str) -> Path:
    return Path(value).resolve()


async def _ensure_active_run(settings: Settings) -> None:
    try:
        async with httpx2.AsyncClient() as client:
            resp = await client.post(
                f"{settings.competition_api_url}/api/v2/runs",
                headers={
                    "Authorization": f"Bearer {settings.team_api_key}",
                    "Content-Type": "application/json",
                },
                json={"variant_id": "l3b"},
                timeout=30.0,
            )
            if resp.status_code not in (200, 201):
                print(
                    f"Warning: Failed to ensure active run: {resp.status_code} {resp.text}",
                    file=sys.stderr,
                )
    except Exception as exc:
        print(f"Warning: Could not ensure active run: {exc}", file=sys.stderr)


async def _show_tools(root: Path) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for tool in await gateway.list_tools():
            print(tool)


MAX_RECONNECTS_PER_CASE = 2
MAX_TOTAL_RECONNECTS = 10


async def _run(root: Path) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)

    # ponytail: every `day09 run` starts clean; a case's evidence refs are only
    # valid for the run that produced them, so resuming across invocations risks
    # cross_scope_evidence_ref hard-gate failures on the old, now-stale run.
    for stale in output_root.glob("*.json"):
        stale.unlink()
    trace_path.unlink(missing_ok=True)
    pending_ids = list(case_set.case_ids)

    trace = TraceWriter(trace_path, contracts)
    await _ensure_active_run(settings)

    total_reconnects = 0
    case_attempts: dict[str, int] = {}

    while pending_ids:
        try:
            async with connect_gateway(
                settings.mcp_endpoint, settings.team_api_key, contracts
            ) as gateway:
                discovered_tools = await gateway.list_tools()
                if not discovered_tools:
                    raise RuntimeError("MCP Gateway returned no tools")
                while pending_ids:
                    case_id = pending_ids[0]
                    case = case_set.cases[case_id]
                    trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
                    output = await solve_case(case, gateway, trace)
                    contracts.validate_output(output, f"outputs/{case_id}.json")
                    if output.get("case_id") != case_id:
                        raise ValueError(f"solver returned a mismatched case_id for {case_id}")
                    target = output_root / f"{case_id}.json"
                    temporary = target.with_suffix(".json.tmp")
                    temporary.write_text(
                        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                    )
                    temporary.replace(target)
                    trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
                    pending_ids.pop(0)
                    done_count = len(case_set.case_ids) - len(pending_ids)
                    print(f"[{case_id}] Completed ({done_count}/{len(case_set.case_ids)})")
        except Exception as exc:
            if not pending_ids:
                break
            case_id = pending_ids[0]
            total_reconnects += 1
            case_attempts[case_id] = case_attempts.get(case_id, 0) + 1
            if (
                case_attempts[case_id] > MAX_RECONNECTS_PER_CASE
                or total_reconnects > MAX_TOTAL_RECONNECTS
            ):
                raise RuntimeError(
                    f"Aborting run: {case_id} failed after "
                    f"{case_attempts[case_id] - 1} reconnect(s): {exc}"
                ) from exc
            print(
                f"Transient error on {case_id} "
                f"(reconnect {case_attempts[case_id]}/{MAX_RECONNECTS_PER_CASE}): "
                f"{exc}. Reconnecting in 2s...",
                file=sys.stderr,
            )
            await asyncio.sleep(2.0)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3B student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    commands.add_parser("run", help="run the implemented workflow for all cases")
    commands.add_parser("validate", help="validate outputs and observable trace")
    package = commands.add_parser("package", help="validate and build the submission ZIP")
    package.add_argument("--output", default="dist/submission.zip")
    return result


def main() -> None:
    args = parser().parse_args()
    root = _root(args.root)
    try:
        if args.command == "validate-inputs":
            case_set = load_case_set(root)
            print(
                f"OK: {case_set.variant_id} / {case_set.version} / "
                f"{len(case_set.case_ids)} cases"
            )
        elif args.command == "mcp-tools":
            asyncio.run(_show_tools(root))
        elif args.command == "run":
            asyncio.run(_run(root))
        elif args.command == "validate":
            case_set = load_case_set(root)
            contracts = Contracts(root / "contracts" / "schemas")
            _, trace = validate_artifacts(root, case_set, contracts)
            print(f"OK: {len(case_set.case_ids)} outputs / {len(trace)} trace events")
        elif args.command == "package":
            destination = package_submission(root, root / args.output)
            print(f"OK: {destination}")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
