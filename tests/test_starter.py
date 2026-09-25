from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from student_agent import OUTPUT_SCHEMA_VERSION, VARIANT_ID
from student_agent import cli as cli_module
from student_agent.cases import CaseSet, load_case_set
from student_agent.config import Settings
from student_agent.contracts import Contracts
from student_agent.submission import build_manifest


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_load_case_set_rejects_wrong_variant(tmp_path: Path) -> None:
    write_json(
        tmp_path / "case-set.json",
        {"case_set_version": "test-v1", "variant_id": "l3a", "case_ids": ["CASE_001"]},
    )
    write_json(tmp_path / "inputs" / "CASE_001.json", {"case_id": "CASE_001"})
    with pytest.raises(ValueError, match="expected variant"):
        load_case_set(tmp_path, expected_count=1)


def test_load_case_set_accepts_exact_input_inventory(tmp_path: Path) -> None:
    case_ids = ["CASE_001", "CASE_002"]
    write_json(
        tmp_path / "case-set.json",
        {"case_set_version": "test-v1", "variant_id": VARIANT_ID, "case_ids": case_ids},
    )
    for case_id in case_ids:
        write_json(tmp_path / "inputs" / f"{case_id}.json", {"case_id": case_id})
    loaded = load_case_set(tmp_path, expected_count=2)
    assert loaded.case_ids == tuple(case_ids)


def test_generated_manifest_matches_public_contract() -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    case_set = CaseSet("test-v1", VARIANT_ID, ("CASE_001",), {})
    manifest = build_manifest(case_set)
    contracts.validate_manifest(manifest)
    assert manifest["output_schema_version"] == OUTPUT_SCHEMA_VERSION


def test_run_aborts_instead_of_looping_forever_on_persistent_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case_set = CaseSet(
        "test-v1", VARIANT_ID, ("CASE_001",), {"CASE_001": {"case_id": "CASE_001"}}
    )
    monkeypatch.setattr(cli_module, "load_case_set", lambda root: case_set)
    monkeypatch.setattr(
        Settings,
        "load",
        classmethod(
            lambda cls, root=None: cls(
                "http://example.invalid",
                "sk-team-" + "a" * 16,
                "http://example.invalid/mcp",
                tmp_path,
            )
        ),
    )
    real_schemas_root = Path(__file__).resolve().parents[1] / "contracts" / "schemas"
    monkeypatch.setattr(cli_module, "Contracts", lambda _root: Contracts(real_schemas_root))

    async def _noop_ensure_active_run(settings: object) -> None:
        return None

    monkeypatch.setattr(cli_module, "_ensure_active_run", _noop_ensure_active_run)

    reconnect_attempts = 0

    @asynccontextmanager
    async def _always_failing_gateway(endpoint: str, team_api_key: str, contracts: object):
        class _Gateway:
            async def list_tools(self) -> list[str]:
                return ["get_order"]

        yield _Gateway()

    async def _boom_solve_case(case: object, gateway: object, trace: object) -> dict:
        nonlocal reconnect_attempts
        reconnect_attempts += 1
        raise RuntimeError("simulated transient failure")

    monkeypatch.setattr(cli_module, "connect_gateway", _always_failing_gateway)
    monkeypatch.setattr(cli_module, "solve_case", _boom_solve_case)

    with pytest.raises(RuntimeError, match="Aborting run"):
        asyncio.run(cli_module._run(tmp_path))

    assert reconnect_attempts == cli_module.MAX_RECONNECTS_PER_CASE + 1
