from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case

ROOT = Path(__file__).resolve().parents[1]


def _hash(payload: str) -> str:
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


class FakeGateway:
    """Schema-valid stub standing in for EvidenceGateway during tests."""

    def __init__(self, contracts: Contracts, responses: dict[str, dict[str, Any]]) -> None:
        self._contracts = contracts
        self._responses = responses
        self.calls: list[str] = []

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append(tool_name)
        domain, data = self._responses[tool_name]
        ref = "ev_" + hashlib.sha256(f"{case_id}:{tool_name}:{arguments}".encode()).hexdigest()[:32]
        evidence = {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": ref,
            "result_hash": _hash(f"{tool_name}:{data}"),
            "domain": domain,
            "data": data,
        }
        self._contracts.validate_evidence(evidence, f"MCP tool {tool_name}")
        return evidence


CASE: dict[str, Any] = {
    "case_id": "L3B_CASE_TEST",
    "customer_request": {
        "claimed_order_id": "order-real",
        "claims": [
            {"claim_id": "c1", "topic": "late_delivery_seller"},
            {"claim_id": "c2", "topic": "requested_full_refund"},
        ],
    },
    "policy_version": "EC_POLICY_V2",
    "candidate_order_ids": ["order-real", "order-fake"],
    "investigation_scope": {
        "include_customer_history": True,
        "include_product_context": True,
        "require_independent_verification": True,
    },
    "customer_unique_id_hint": "cust-1",
}

RESPONSES: dict[str, dict[str, Any]] = {
    "get_order": (
        "order",
        {
            "order_id": "order-real",
            "order_status": "delivered",
            "order_delivered_customer_date": "2018-01-10T00:00:00Z",
            "order_estimated_delivery_date": "2018-01-05T00:00:00Z",
        },
    ),
    "get_customer_history": (
        "customer",
        {"customer_unique_id": "cust-1", "order_ids": ["order-real"]},
    ),
    "get_order_items": (
        "item",
        {
            "items": [
                {
                    "order_item_id": "1",
                    "seller_id": "seller-1",
                    "price": 100.0,
                    "freight_value": 10.0,
                }
            ]
        },
    ),
    "get_product_context": ("product", {"category": "toys"}),
    "get_shipment_summary": (
        "shipment",
        {
            "status": "delivered",
            "delivered_at": "2018-01-10T00:00:00Z",
            "estimated_delivery_date": "2018-01-05T00:00:00Z",
            "fault": "seller",
        },
    ),
    "get_sellers": ("seller", {"seller_ids": ["seller-1"]}),
    "get_order_payments": ("payment", {"payments": [{"payment_id": "p1", "payment_value": 110.0}]}),
    "get_payment_timeline": ("payment", {"status": "ok"}),
    "get_refund_timeline": ("refund", {"status": "none", "refunded_total_brl": 0}),
    "get_policy": ("policy", {"refund_window_days": 30}),
}


def test_solve_case_produces_schema_valid_output_and_trace(tmp_path: Path) -> None:
    contracts = Contracts(ROOT / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    gateway = FakeGateway(contracts, RESPONSES)

    output = asyncio.run(solve_case(CASE, gateway, trace))

    contracts.validate_output(output, "test output")
    assert output["case_id"] == "L3B_CASE_TEST"
    assert output["entity_resolution"]["status"] == "resolved"
    assert output["entity_resolution"]["resolved_order_ids"] == ["order-real"]
    assert "order-fake" in output["entity_resolution"]["rejected_candidates"]
    assert output["shipment_analysis"]["verdict"] == "seller_delay"

    lines = (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    assert lines
    event_types = set()
    for line in lines:
        event = json.loads(line)
        contracts.validate_trace(event, "trace event")
        event_types.add(event["event_type"])

    assert "task_assigned" in event_types
    assert "handoff" in event_types
    assert "verification_completed" in event_types
