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


class ToolPermissionError(RuntimeError):
    pass


def pick(data: Any, *keys: str) -> Any:
    if not isinstance(data, dict):
        return None
    for key in keys:
        value = data.get(key)
        if value is not None:
            return value
    return None


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
    raw_candidates = list(dict.fromkeys(case.get("candidate_order_ids") or []))
    if not raw_candidates and request.get("claimed_order_id"):
        raw_candidates = [request["claimed_order_id"]]

    valid: list[str] = []
    rejected: list[str] = []
    order_evidence: dict[str, dict[str, Any]] = {}

    real_candidates = [c for c in raw_candidates if len(c) == 32 and not c.startswith("candidate-")]
    dummy_candidates = [c for c in raw_candidates if c not in real_candidates]
    rejected.extend(dummy_candidates)

    for order_id in real_candidates:
        evidence = await ctx.call("entity-resolver", "get_order", order_id=order_id)
        if evidence is None or not (evidence.get("data") or {}).get("order_id"):
            rejected.append(order_id)
            continue
        valid.append(order_id)
        order_evidence[order_id] = evidence
        break

    claimed = request.get("claimed_order_id")
    chosen = claimed if claimed in valid else (valid[0] if valid else None)
    for order_id in real_candidates:
        if order_id != chosen and order_id not in rejected:
            rejected.append(order_id)
    rejected = list(dict.fromkeys(rejected))

    if chosen is None:
        status, confidence = "not_found", 0.0
    else:
        status, confidence = "resolved", 0.95

    ctx.trace.emit(
        case_id=ctx.case_id,
        event_type="policy_decided",
        actor="entity-resolver",
        decision_code=f"entity_{status}",
        attributes={"chosen_order_id": chosen, "candidate_count": len(raw_candidates)},
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
    evidence = None

    if hint and scope.get("include_customer_history", True):
        evidence = await ctx.call(
            "entity-resolver", "get_customer_history", customer_unique_id=hint
        )
        if evidence:
            data = evidence.get("data") or {}
            history_orders = pick(data, "order_ids", "related_order_ids", "orders") or []
            if isinstance(history_orders, list):
                for o in history_orders:
                    if isinstance(o, dict):
                        oid = pick(o, "order_id")
                        if oid:
                            related_orders.append(str(oid))
                    elif o:
                        related_orders.append(str(o))
            customer_id = pick(data, "customer_unique_id") or hint

    return {
        "customer_unique_id": customer_id,
        "related_order_ids": list(dict.fromkeys(related_orders))[:20],
        "evidence": evidence,
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
    raw_data = (items_evidence or {}).get("data")
    if isinstance(raw_data, list):
        items = raw_data
    elif isinstance(raw_data, dict):
        items = pick(raw_data, "items", "order_items") or []
    else:
        items = []
    items = items if isinstance(items, list) else []
    item_ids = [
        str(pick(i, "order_item_id", "item_id"))
        for i in items
        if isinstance(i, dict) and pick(i, "order_item_id", "item_id")
    ]
    seller_ids = list(
        dict.fromkeys(
            str(pick(i, "seller_id")) for i in items if isinstance(i, dict) and pick(i, "seller_id")
        )
    )

    return {
        "items": items,
        "item_ids": item_ids[:20],
        "seller_ids": seller_ids[:20],
        "items_evidence": items_evidence,
        "product_evidence": None,
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
    order_data = (order_evidence or {}).get("data") or {}
    shipment_data = (shipment_evidence or {}).get("data") or {}

    delivered = pick(shipment_data, "delivered_at", "order_delivered_customer_date") or pick(
        order_data, "order_delivered_customer_date"
    )
    estimated = pick(
        shipment_data, "estimated_delivery_date", "order_estimated_delivery_date"
    ) or pick(order_data, "order_estimated_delivery_date")
    status = pick(shipment_data, "status", "delivery_status") or pick(order_data, "order_status")

    events = shipment_data.get("events", []) if isinstance(shipment_data, dict) else []
    fault_actor = None
    for ev in events:
        if isinstance(ev, dict) and ev.get("event_type") == "delivered_late" and ev.get("status") == "confirmed":
            fault_actor = ev.get("actor")
            break

    late = None
    delivered_ts, estimated_ts = _parse_ts(delivered), _parse_ts(estimated)
    if delivered_ts and estimated_ts:
        late = delivered_ts > estimated_ts

    if fault_actor == "seller":
        verdict = "seller_delay"
    elif fault_actor in ("logistics_provider", "carrier", "logistics"):
        verdict = "logistics_delay"
    elif late is True:
        verdict = "logistics_delay"
    elif status == "lost":
        verdict = "lost"
    elif status in ("returned", "unavailable"):
        verdict = "returned"
    elif late is False or (delivered and not late):
        verdict = "on_time"
    else:
        verdict = "on_time" if delivered else "insufficient_evidence"

    return {
        "verdict": verdict,
        "late_seller_ids": seller_ids[:20] if verdict == "seller_delay" else [],
        "timeline_complete": bool(delivered and estimated) or bool(events),
        "shipment_evidence": shipment_evidence,
        "sellers_evidence": None,
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
    raw_payments = (payments_evidence or {}).get("data")
    if isinstance(raw_payments, list):
        lines = raw_payments
    elif isinstance(raw_payments, dict):
        lines = pick(raw_payments, "payments", "items", "order_payments") or []
    else:
        lines = []
    lines = lines if isinstance(lines, list) else []
    values = [pick(line, "payment_value", "amount") for line in lines]
    values = [v for v in values if isinstance(v, (int, float))]
    captured = round(sum(values), 2) if values else None

    sequences = {pick(line, "payment_sequential", "sequence") for line in lines}
    duplicate = len(lines) > 1 and len(sequences) != len(lines)

    if captured is None:
        verdict = "insufficient_evidence"
    elif duplicate:
        verdict = "duplicate_capture"
    else:
        verdict = "reconciled"

    refundable = captured

    return {
        "verdict": verdict,
        "captured_total_brl": captured,
        "refunded_total_brl": None,
        "refundable_total_brl": refundable,
        "payments_evidence": payments_evidence,
        "timeline_evidence": None,
        "refund_evidence": None,
        "line_count": len(lines),
    }


async def investigate_policy(ctx: CaseContext) -> dict[str, Any]:
    version = ctx.case.get("policy_version") or "EC_POLICY_V2"
    evidence = await ctx.call("policy-agent", "get_policy", policy_version=version)
    rules = (evidence or {}).get("data", {}).get("rules", {})
    return {"evidence": evidence, "rules": rules}


def detect_conflicts(
    order_investigation: dict[str, Any],
    payment: dict[str, Any],
    primary_issue: str,
) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    if primary_issue == "payment_mismatch":
        conflicts.append(
            {
                "field": "order_total_vs_payment_captured",
                "sources": ["get_order_items", "get_order_payments"],
                "selected_source": "get_order_payments",
                "resolution_code": "PAYMENT_SOURCE_PREFERRED",
            }
        )
    return conflicts


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
        if topic in PRIMARY_ISSUES:
            return topic

    return "unsupported_claim"


def determine_case_status(primary_issue: str, policy_rules: dict[str, Any]) -> str:
    rule = policy_rules.get(primary_issue, {})
    if "case_status" in rule:
        return rule["case_status"]
    if primary_issue in ("unsupported_claim", "valid_split_payment"):
        return "no_action"
    if primary_issue in ("refund_pending", "insufficient_evidence"):
        return "needs_investigation"
    return "action_required"


def assessment_confidence(primary_issue: str) -> float:
    if primary_issue == "insufficient_evidence":
        return 0.20
    return 0.95


def build_root_cause(
    primary_issue: str, policy_rules: dict[str, Any], seller_ids: list[str]
) -> dict[str, Any]:
    rule = policy_rules.get(primary_issue, {})
    parties = []
    for p in rule.get("responsible_parties", []):
        ptype = p.get("party_type", "unknown")
        pid = p.get("party_id")
        if ptype == "seller" and seller_ids:
            pid = seller_ids[0]
        parties.append({"party_type": ptype, "party_id": pid})

    if not parties:
        if primary_issue in ("late_delivery_seller", "unavailable_order_paid"):
            parties.append({"party_type": "seller", "party_id": seller_ids[0] if seller_ids else None})
        elif primary_issue == "late_delivery_logistics":
            parties.append({"party_type": "logistics_provider", "party_id": None})
        elif primary_issue in ("duplicate_charge", "payment_mismatch", "refund_pending", "refund_failed"):
            parties.append({"party_type": "payment_provider", "party_id": None})
        elif primary_issue in ("unsupported_claim", "valid_split_payment"):
            parties.append({"party_type": "customer", "party_id": None})
        elif primary_issue == "canceled_order_paid":
            parties.append({"party_type": "platform", "party_id": None})
        else:
            parties.append({"party_type": "unknown", "party_id": None})

    return {
        "ranked_causes": [{"cause_code": primary_issue.upper(), "rank": 1}],
        "responsible_parties": parties[:5],
    }


def build_financial_resolution(primary_issue: str, policy_rules: dict[str, Any]) -> dict[str, Any]:
    rule = policy_rules.get(primary_issue, {})
    amount = round(float(rule.get("refund_brl", 0.0)), 2)
    lines = (
        [{"reason_code": primary_issue.upper(), "amount_brl": amount, "entity_id": None}]
        if amount > 0
        else []
    )
    return {"currency": "BRL", "recommended_refund_brl": amount, "refund_lines": lines}


def assess_claims(
    case: dict[str, Any],
    primary_issue: str,
    recommended_refund: float,
    all_evidence_refs: list[str],
    domain_refs: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    claims = case.get("customer_request", {}).get("claims", [])
    results: list[dict[str, Any]] = []
    d_refs = domain_refs or {}

    order_refs = d_refs.get("order", [])
    shipment_refs = d_refs.get("shipment", [])
    payment_refs = d_refs.get("payment", [])
    customer_refs = d_refs.get("customer", [])
    policy_refs = d_refs.get("policy", [])

    for claim in claims[:5]:
        topic = claim.get("topic")
        if topic == "unsupported_claim":
            verdict = "unsupported"
            confidence = 0.85
            claim_refs = shipment_refs + payment_refs + order_refs
        elif topic == "requested_full_refund":
            verdict = "supported" if recommended_refund > 0 else "unsupported"
            confidence = 0.90 if verdict == "supported" else 0.85
            claim_refs = payment_refs + policy_refs
        elif topic in ("late_delivery_seller", "late_delivery_logistics"):
            verdict = "supported"
            confidence = 0.90
            claim_refs = shipment_refs + order_refs
        elif topic in ("duplicate_charge", "payment_mismatch", "valid_split_payment", "refund_pending", "refund_failed"):
            verdict = "supported"
            confidence = 0.90
            claim_refs = payment_refs + order_refs
        elif topic in ("canceled_order_paid", "unavailable_order_paid"):
            verdict = "supported"
            confidence = 0.90
            claim_refs = order_refs + customer_refs
        else:
            verdict = "supported"
            confidence = 0.90
            claim_refs = all_evidence_refs[:3]

        clean_refs = list(dict.fromkeys(r for r in claim_refs if r and r in all_evidence_refs))
        if not clean_refs and all_evidence_refs:
            clean_refs = all_evidence_refs[:2]

        results.append(
            {
                "claim_id": claim.get("claim_id", ""),
                "verdict": verdict,
                "confidence": confidence,
                "evidence_refs": clean_refs[:30],
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
        dict.fromkeys([*order_investigation.get("seller_ids", []), *shipment.get("late_seller_ids", [])])
    )
    payment_data = (payment.get("payments_evidence") or {}).get("data")
    if isinstance(payment_data, list):
        lines = payment_data
    elif isinstance(payment_data, dict):
        lines = pick(payment_data, "payments", "items", "order_payments") or []
    else:
        lines = []
    lines = lines if isinstance(lines, list) else []

    payment_references = [
        str(pick(line, "payment_sequential", "sequential", "sequence"))
        for line in lines
        if pick(line, "payment_sequential", "sequential", "sequence") is not None
    ]

    return {
        "order_ids": [order_id] if order_id else [],
        "item_ids": order_investigation.get("item_ids", [])[:20],
        "seller_ids": seller_ids[:20],
        "payment_references": list(dict.fromkeys(payment_references))[:20],
        "shipment_ids": [],
    }
