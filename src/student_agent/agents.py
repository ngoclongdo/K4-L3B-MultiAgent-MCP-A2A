from __future__ import annotations

from datetime import datetime
from typing import Any

from mcp.shared.exceptions import MCPError

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

RETRY_ATTEMPTS = 2

ALLOWED_TOOLS: dict[str, frozenset[str]] = {
    "entity-resolver": frozenset({"get_order", "get_customer_history"}),
    "order-agent": frozenset({"get_order_items", "get_product_context"}),
    "shipment-agent": frozenset({"get_shipment_summary", "get_sellers"}),
    "payment-agent": frozenset(
        {"get_order_payments", "get_payment_timeline", "get_refund_timeline"}
    ),
    "policy-agent": frozenset({"get_policy"}),
}

PRIMARY_ISSUES = {
    "canceled_order_paid",
    "unavailable_order_paid",
    "late_delivery_seller",
    "late_delivery_logistics",
    "valid_split_payment",
    "payment_mismatch",
    "duplicate_charge",
    "refund_pending",
    "refund_failed",
    "unsupported_claim",
    "insufficient_evidence",
}
SHIPMENT_TOPIC_VERDICT = {
    "late_delivery_seller": "seller_delay",
    "late_delivery_logistics": "logistics_delay",
}
PAYMENT_TOPIC_VERDICT = {
    "payment_mismatch": "capture_mismatch",
    "duplicate_charge": "duplicate_capture",
    "refund_pending": "refund_pending",
    "refund_failed": "refund_failed",
}


class ToolPermissionError(RuntimeError):
    pass


def pick(data: dict[str, Any] | None, *keys: str) -> Any:
    if not data:
        return None
    for key in keys:
        value = data.get(key)
        if value is not None:
            return value
    return None


def _refs(*evidences: dict[str, Any] | None) -> list[str]:
    return [e["evidence_ref"] for e in evidences if e]


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class CaseContext:
    """Per-case A2A state: tool cache, evidence ledger, bounded retry, least-privilege gate."""

    def __init__(self, case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.case = case
        self.case_id = case["case_id"]
        self.gateway = gateway
        self.trace = trace
        self._cache: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any] | None] = {}
        self.evidence_refs: set[str] = set()

    async def call(self, actor: str, tool_name: str, **kwargs: str) -> dict[str, Any] | None:
        allowed = ALLOWED_TOOLS.get(actor, frozenset())
        if tool_name not in allowed:
            raise ToolPermissionError(f"{actor} is not permitted to call {tool_name}")

        key = (tool_name, tuple(sorted(kwargs.items())))
        if key in self._cache:
            return self._cache[key]

        evidence: dict[str, Any] | None = None
        error: Exception | None = None
        for _ in range(RETRY_ATTEMPTS):
            try:
                evidence = await self.gateway.call(tool_name, case_id=self.case_id, **kwargs)
                error = None
                break
            except (RuntimeError, ValueError, TimeoutError, OSError, MCPError) as exc:
                error = exc

        if evidence is None:
            self.trace.emit(
                case_id=self.case_id,
                event_type="tool_result_consumed",
                actor=actor,
                tool_name=tool_name,
                decision_code="tool_call_failed",
                attributes={"error": str(error)[:160] if error else "no_data"},
            )
            self._cache[key] = None
            return None

        self.evidence_refs.add(evidence["evidence_ref"])
        self._cache[key] = evidence
        self.trace.emit(
            case_id=self.case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool_name,
            evidence_refs=[evidence["evidence_ref"]],
        )
        return evidence


async def resolve_entity(ctx: CaseContext) -> dict[str, Any]:
    case = ctx.case
    request = case.get("customer_request", {})
    candidates = list(dict.fromkeys(case.get("candidate_order_ids") or []))
    if not candidates and request.get("claimed_order_id"):
        candidates = [request["claimed_order_id"]]

    valid: list[str] = []
    rejected: list[str] = []
    order_evidence: dict[str, dict[str, Any]] = {}
    for order_id in candidates:
        evidence = await ctx.call("entity-resolver", "get_order", order_id=order_id)
        if evidence is None or not (evidence.get("data") or {}).get("order_id"):
            rejected.append(order_id)
            continue
        valid.append(order_id)
        order_evidence[order_id] = evidence

    claimed = request.get("claimed_order_id")
    chosen = claimed if claimed in valid else (valid[0] if valid else None)
    for order_id in valid:
        if order_id != chosen:
            rejected.append(order_id)
    rejected = list(dict.fromkeys(rejected))

    if chosen is None:
        status, confidence = "not_found", 0.0
    elif len(valid) > 1:
        status, confidence = ("resolved", 0.85) if chosen == claimed else ("ambiguous", 0.55)
    else:
        status, confidence = "resolved", 0.9

    ctx.trace.emit(
        case_id=ctx.case_id,
        event_type="policy_decided",
        actor="entity-resolver",
        decision_code=f"entity_{status}",
        attributes={"chosen_order_id": chosen, "candidate_count": len(candidates)},
    )

    return {
        "status": status,
        "chosen_order_id": chosen,
        "resolved_order_ids": [chosen] if chosen else [],
        "rejected_candidates": rejected,
        "confidence": confidence,
        "order_evidence": order_evidence.get(chosen) if chosen else None,
    }


def order_status_of(entity: dict[str, Any]) -> str | None:
    evidence = entity.get("order_evidence")
    if not evidence:
        return None
    return pick(evidence.get("data") or {}, "order_status", "status")


async def resolve_customer(ctx: CaseContext, entity: dict[str, Any]) -> dict[str, Any]:
    case = ctx.case
    scope = case.get("investigation_scope", {})
    hint = case.get("customer_unique_id_hint")
    customer_id = hint
    related_orders: list[str] = [entity["chosen_order_id"]] if entity["chosen_order_id"] else []

    if hint and scope.get("include_customer_history", True):
        evidence = await ctx.call(
            "entity-resolver", "get_customer_history", customer_unique_id=hint
        )
        if evidence:
            data = evidence.get("data") or {}
            history_orders = pick(data, "order_ids", "related_order_ids", "orders") or []
            if isinstance(history_orders, list):
                related_orders.extend(str(o) for o in history_orders if o)
            customer_id = pick(data, "customer_unique_id") or hint

    return {
        "customer_unique_id": customer_id,
        "related_order_ids": list(dict.fromkeys(related_orders))[:20],
    }


async def investigate_order(ctx: CaseContext, order_id: str | None) -> dict[str, Any]:
    if not order_id:
        return {
            "items": [],
            "item_ids": [],
            "seller_ids": [],
            "items_evidence": None,
            "product_evidence": None,
        }

    items_evidence = await ctx.call("order-agent", "get_order_items", order_id=order_id)
    product_evidence = None
    if ctx.case.get("investigation_scope", {}).get("include_product_context", True):
        product_evidence = await ctx.call("order-agent", "get_product_context", order_id=order_id)

    items = pick((items_evidence or {}).get("data") or {}, "items", "order_items") or []
    items = items if isinstance(items, list) else []
    item_ids = [
        str(pick(i, "order_item_id", "item_id"))
        for i in items
        if pick(i, "order_item_id", "item_id")
    ]
    seller_ids = list(
        dict.fromkeys(str(pick(i, "seller_id")) for i in items if pick(i, "seller_id"))
    )

    return {
        "items": items,
        "item_ids": item_ids[:20],
        "seller_ids": seller_ids[:20],
        "items_evidence": items_evidence,
        "product_evidence": product_evidence,
    }


async def investigate_shipment(
    ctx: CaseContext,
    order_id: str | None,
    order_evidence: dict[str, Any] | None,
    seller_ids: list[str],
) -> dict[str, Any]:
    if not order_id:
        return {
            "verdict": "insufficient_evidence",
            "late_seller_ids": [],
            "timeline_complete": False,
            "shipment_evidence": None,
            "sellers_evidence": None,
            "delivered": None,
            "estimated": None,
        }

    shipment_evidence = await ctx.call("shipment-agent", "get_shipment_summary", order_id=order_id)
    sellers_evidence = await ctx.call("shipment-agent", "get_sellers", order_id=order_id)

    order_data = (order_evidence or {}).get("data") or {}
    shipment_data = (shipment_evidence or {}).get("data") or {}

    delivered = pick(shipment_data, "delivered_at", "order_delivered_customer_date") or pick(
        order_data, "order_delivered_customer_date"
    )
    estimated = pick(
        shipment_data, "estimated_delivery_date", "order_estimated_delivery_date"
    ) or pick(order_data, "order_estimated_delivery_date")
    status = pick(shipment_data, "status", "delivery_status") or pick(order_data, "order_status")
    fault = pick(shipment_data, "fault", "responsible_party", "delay_cause")

    late = None
    delivered_ts, estimated_ts = _parse_ts(delivered), _parse_ts(estimated)
    if delivered_ts and estimated_ts:
        late = delivered_ts > estimated_ts

    if shipment_evidence is None and order_evidence is None:
        verdict = "insufficient_evidence"
    elif status == "lost":
        verdict = "lost"
    elif status in ("returned", "unavailable"):
        verdict = "returned"
    elif late is True:
        verdict = "logistics_delay" if fault == "logistics" else "seller_delay"
    elif late is False:
        verdict = "on_time"
    else:
        verdict = "insufficient_evidence"

    return {
        "verdict": verdict,
        "late_seller_ids": seller_ids[:20] if late is True else [],
        "timeline_complete": bool(delivered and estimated),
        "shipment_evidence": shipment_evidence,
        "sellers_evidence": sellers_evidence,
        "delivered": delivered,
        "estimated": estimated,
    }


async def investigate_payment(ctx: CaseContext, order_id: str | None) -> dict[str, Any]:
    if not order_id:
        return {
            "verdict": "insufficient_evidence",
            "captured_total_brl": None,
            "refunded_total_brl": None,
            "refundable_total_brl": None,
            "payments_evidence": None,
            "timeline_evidence": None,
            "refund_evidence": None,
            "line_count": 0,
        }

    payments_evidence = await ctx.call("payment-agent", "get_order_payments", order_id=order_id)
    timeline_evidence = await ctx.call("payment-agent", "get_payment_timeline", order_id=order_id)
    refund_evidence = await ctx.call("payment-agent", "get_refund_timeline", order_id=order_id)

    payments_data = (payments_evidence or {}).get("data") or {}
    lines = pick(payments_data, "payments", "items") or []
    lines = lines if isinstance(lines, list) else []
    values = [pick(line, "payment_value", "amount") for line in lines]
    values = [v for v in values if isinstance(v, (int, float))]
    captured = round(sum(values), 2) if values else None

    refund_data = (refund_evidence or {}).get("data") or {}
    refunded = pick(refund_data, "refunded_total_brl", "total_refunded", "refunded_amount")
    refunded = round(refunded, 2) if isinstance(refunded, (int, float)) else None

    timeline_data = (timeline_evidence or {}).get("data") or {}
    timeline_status = pick(timeline_data, "status", "verdict")
    refund_status = pick(refund_data, "status", "verdict")
    sequences = {pick(line, "payment_sequential", "sequence") for line in lines}
    duplicate = len(lines) > 1 and len(sequences) != len(lines)

    if captured is None and refunded is None:
        verdict = "insufficient_evidence"
    elif refund_status == "failed" or timeline_status == "refund_failed":
        verdict = "refund_failed"
    elif refund_status in ("pending", "processing"):
        verdict = "refund_pending"
    elif refunded and captured and refunded >= captured:
        verdict = "refunded"
    elif duplicate:
        verdict = "duplicate_capture"
    elif timeline_status == "mismatch":
        verdict = "capture_mismatch"
    else:
        verdict = "reconciled"

    refundable = round(max(captured - (refunded or 0), 0), 2) if captured is not None else None

    return {
        "verdict": verdict,
        "captured_total_brl": captured,
        "refunded_total_brl": refunded,
        "refundable_total_brl": refundable,
        "payments_evidence": payments_evidence,
        "timeline_evidence": timeline_evidence,
        "refund_evidence": refund_evidence,
        "line_count": len(lines),
    }


async def investigate_policy(ctx: CaseContext) -> dict[str, Any]:
    version = ctx.case.get("policy_version")
    if not version:
        return {"evidence": None}
    evidence = await ctx.call("policy-agent", "get_policy", policy_version=version)
    return {"evidence": evidence}


def detect_conflicts(
    order_investigation: dict[str, Any], shipment: dict[str, Any], payment: dict[str, Any]
) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    items = order_investigation.get("items") or []
    item_values = [pick(i, "price") for i in items]
    item_values = [v for v in item_values if isinstance(v, (int, float))]
    freight_values = [
        v for v in (pick(i, "freight_value") for i in items) if isinstance(v, (int, float))
    ]
    item_total = round(sum(item_values) + sum(freight_values), 2) if item_values else None
    captured = payment.get("captured_total_brl")
    if item_total is not None and captured is not None and abs(item_total - captured) > 0.05:
        conflicts.append(
            {
                "field": "order_total_vs_payment_captured",
                "sources": ["get_order_items", "get_order_payments"],
                "selected_source": "get_order_payments",
                "resolution_code": "PAYMENT_SOURCE_PREFERRED",
            }
        )

    if shipment.get("verdict") == "insufficient_evidence" and (
        shipment.get("delivered") or shipment.get("estimated")
    ):
        conflicts.append(
            {
                "field": "shipment_timeline",
                "sources": ["get_shipment_summary", "get_order"],
                "selected_source": None,
                "resolution_code": "TIMELINE_UNRESOLVED",
            }
        )
    return conflicts[:5]


def determine_primary_issue(
    entity_status: str,
    order_status: str | None,
    shipment_verdict: str,
    payment_verdict: str,
    topics: list[str],
) -> str:
    if entity_status != "resolved":
        return "insufficient_evidence"
    for topic in topics:
        if topic not in PRIMARY_ISSUES:
            continue
        if topic in SHIPMENT_TOPIC_VERDICT:
            if shipment_verdict == SHIPMENT_TOPIC_VERDICT[topic]:
                return topic
            continue
        if topic in PAYMENT_TOPIC_VERDICT:
            if payment_verdict == PAYMENT_TOPIC_VERDICT[topic]:
                return topic
            continue
        if topic == "canceled_order_paid" and order_status == "canceled":
            return topic
        if topic == "unavailable_order_paid" and order_status == "unavailable":
            return topic
        if topic in ("valid_split_payment", "unsupported_claim"):
            return topic
    return "insufficient_evidence"


def determine_case_status(entity_status: str, primary_issue: str) -> str:
    if entity_status != "resolved" or primary_issue == "insufficient_evidence":
        return "needs_investigation"
    if primary_issue in ("valid_split_payment", "unsupported_claim"):
        return "no_action"
    return "action_required"


def assessment_confidence(
    entity_conf: float, shipment_verdict: str, payment_verdict: str, primary_issue: str
) -> float:
    if primary_issue == "insufficient_evidence":
        return round(min(0.3, entity_conf), 2)
    base = 0.5 + 0.25 * entity_conf
    if shipment_verdict not in ("insufficient_evidence", "conflicting"):
        base += 0.1
    if payment_verdict != "insufficient_evidence":
        base += 0.1
    return round(min(base, 0.95), 2)


def build_root_cause(
    primary_issue: str, shipment_verdict: str, payment_verdict: str
) -> dict[str, Any]:
    causes: list[dict[str, Any]] = []
    codes: set[str] = set()
    if primary_issue != "insufficient_evidence":
        causes.append({"cause_code": primary_issue.upper(), "rank": len(causes) + 1})
        codes.add(primary_issue.upper())
    if (
        shipment_verdict in ("seller_delay", "logistics_delay")
        and shipment_verdict.upper() not in codes
    ):
        causes.append({"cause_code": shipment_verdict.upper(), "rank": len(causes) + 1})
        codes.add(shipment_verdict.upper())
    if (
        payment_verdict not in ("reconciled", "insufficient_evidence")
        and payment_verdict.upper() not in codes
    ):
        causes.append({"cause_code": payment_verdict.upper(), "rank": len(causes) + 1})
    if not causes:
        causes.append({"cause_code": "INSUFFICIENT_EVIDENCE", "rank": 1})

    parties: list[dict[str, Any]] = []
    if shipment_verdict == "seller_delay":
        parties.append({"party_type": "seller", "party_id": None})
    elif shipment_verdict == "logistics_delay":
        parties.append({"party_type": "logistics_provider", "party_id": None})
    if payment_verdict in (
        "capture_mismatch",
        "duplicate_capture",
        "refund_failed",
        "refund_pending",
    ):
        parties.append({"party_type": "payment_provider", "party_id": None})
    if not parties:
        parties.append({"party_type": "unknown", "party_id": None})

    return {"ranked_causes": causes[:5], "responsible_parties": parties[:5]}


def build_financial_resolution(payment: dict[str, Any], primary_issue: str) -> dict[str, Any]:
    refundable = payment.get("refundable_total_brl")
    refund_eligible = primary_issue in (
        "late_delivery_seller",
        "late_delivery_logistics",
        "canceled_order_paid",
        "unavailable_order_paid",
        "refund_pending",
        "refund_failed",
        "payment_mismatch",
        "duplicate_charge",
    )
    amount = refundable if refund_eligible and refundable else 0.0
    lines = (
        [{"reason_code": primary_issue.upper(), "amount_brl": amount, "entity_id": None}]
        if amount
        else []
    )
    return {"currency": "BRL", "recommended_refund_brl": round(amount, 2), "refund_lines": lines}


def _shipment_claim_verdict(actual: str, expected: str) -> str:
    if actual == expected:
        return "supported"
    if actual == "conflicting":
        return "partially_supported"
    if actual == "insufficient_evidence":
        return "insufficient_evidence"
    return "unsupported"


def _match_verdict(actual: str, expected: str) -> str:
    if actual == expected:
        return "supported"
    if actual == "insufficient_evidence":
        return "insufficient_evidence"
    return "unsupported"


_VERDICT_CONFIDENCE = {
    "supported": 0.85,
    "partially_supported": 0.55,
    "unsupported": 0.75,
    "insufficient_evidence": 0.2,
}


def assess_claims(
    case: dict[str, Any],
    order_status: str | None,
    shipment: dict[str, Any],
    payment: dict[str, Any],
    recommended_refund: float,
) -> list[dict[str, Any]]:
    claims = case.get("customer_request", {}).get("claims", [])
    shipment_refs = _refs(shipment.get("shipment_evidence"), shipment.get("sellers_evidence"))
    payment_refs = _refs(
        payment.get("payments_evidence"),
        payment.get("timeline_evidence"),
        payment.get("refund_evidence"),
    )

    results: list[dict[str, Any]] = []
    for claim in claims[:5]:
        topic = claim.get("topic")
        if topic in SHIPMENT_TOPIC_VERDICT:
            verdict = _shipment_claim_verdict(shipment["verdict"], SHIPMENT_TOPIC_VERDICT[topic])
            refs = shipment_refs
        elif topic in PAYMENT_TOPIC_VERDICT:
            verdict = _match_verdict(payment["verdict"], PAYMENT_TOPIC_VERDICT[topic])
            refs = payment_refs
        elif topic == "requested_full_refund":
            verdict = (
                "insufficient_evidence"
                if payment["verdict"] == "insufficient_evidence"
                else "supported"
                if recommended_refund > 0
                else "unsupported"
            )
            refs = payment_refs
        elif topic == "valid_split_payment":
            verdict = (
                "insufficient_evidence"
                if payment["verdict"] == "insufficient_evidence"
                else "supported"
                if payment.get("line_count", 0) > 1
                else "unsupported"
            )
            refs = payment_refs
        elif topic in ("canceled_order_paid", "unavailable_order_paid"):
            if order_status is None:
                verdict = "insufficient_evidence"
            else:
                match = (topic == "canceled_order_paid" and order_status == "canceled") or (
                    topic == "unavailable_order_paid" and order_status == "unavailable"
                )
                verdict = "supported" if match else "unsupported"
            refs = shipment_refs
        else:
            verdict = "insufficient_evidence"
            refs = []
        results.append(
            {
                "claim_id": claim.get("claim_id", ""),
                "verdict": verdict,
                "confidence": _VERDICT_CONFIDENCE[verdict],
                "evidence_refs": refs[:30],
            }
        )
    return results


def build_affected_entities(
    order_id: str | None,
    order_investigation: dict[str, Any],
    shipment: dict[str, Any],
    payment: dict[str, Any],
) -> dict[str, Any]:
    seller_ids = list(
        dict.fromkeys([*order_investigation["seller_ids"], *shipment["late_seller_ids"]])
    )
    payment_data = (payment.get("payments_evidence") or {}).get("data") or {}
    lines = pick(payment_data, "payments", "items") or []
    payment_refs = [
        str(pick(line, "payment_id", "payment_sequential"))
        for line in lines
        if pick(line, "payment_id", "payment_sequential")
    ]
    shipment_data = (shipment.get("shipment_evidence") or {}).get("data") or {}
    shipment_id = pick(shipment_data, "shipment_id")

    return {
        "order_ids": [order_id] if order_id else [],
        "item_ids": order_investigation["item_ids"],
        "seller_ids": seller_ids[:20],
        "payment_references": list(dict.fromkeys(payment_refs))[:20],
        "shipment_ids": [str(shipment_id)] if shipment_id else [],
    }
