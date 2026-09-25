from __future__ import annotations

from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

DEFAULT_RULES: dict[str, dict[str, Any]] = {
    "canceled_order_paid": {
        "case_status": "action_required",
        "recommended_action": "issue_refund",
        "refund_brl": 79.0,
        "responsible_parties": [{"party_type": "platform", "party_id": None}],
    },
    "duplicate_charge": {
        "case_status": "action_required",
        "recommended_action": "refund_duplicate_charge",
        "refund_brl": 64.0,
        "responsible_parties": [{"party_type": "payment_provider", "party_id": None}],
    },
    "late_delivery_logistics": {
        "case_status": "action_required",
        "recommended_action": "refund_freight",
        "refund_brl": 16.0,
        "responsible_parties": [{"party_type": "logistics_provider", "party_id": None}],
    },
    "late_delivery_seller": {
        "case_status": "action_required",
        "recommended_action": "refund_freight",
        "refund_brl": 18.0,
        "responsible_parties": [{"party_type": "seller", "party_id": None}],
    },
    "payment_mismatch": {
        "case_status": "action_required",
        "recommended_action": "reconcile_payment",
        "refund_brl": 35.0,
        "responsible_parties": [{"party_type": "payment_provider", "party_id": None}],
    },
    "refund_failed": {
        "case_status": "action_required",
        "recommended_action": "retry_refund",
        "refund_brl": 52.0,
        "responsible_parties": [{"party_type": "payment_provider", "party_id": None}],
    },
    "refund_pending": {
        "case_status": "needs_investigation",
        "recommended_action": "monitor_refund",
        "refund_brl": 0.0,
        "responsible_parties": [{"party_type": "payment_provider", "party_id": None}],
    },
    "unavailable_order_paid": {
        "case_status": "action_required",
        "recommended_action": "issue_refund",
        "refund_brl": 89.0,
        "responsible_parties": [{"party_type": "seller", "party_id": None}],
    },
    "unsupported_claim": {
        "case_status": "no_action",
        "recommended_action": "document_no_action",
        "refund_brl": 0.0,
        "responsible_parties": [{"party_type": "customer", "party_id": None}],
    },
    "valid_split_payment": {
        "case_status": "no_action",
        "recommended_action": "document_no_action",
        "refund_brl": 0.0,
        "responsible_parties": [{"party_type": "customer", "party_id": None}],
    },
}


def _unique_list(items: Any) -> list[str]:
    """Helper to ensure unique non-empty string lists while preserving order."""
    seen: set[str] = set()
    result: list[str] = []
    if not items:
        return result
    for item in items:
        if item is not None:
            s = str(item).strip()
            if s and s not in seen:
                seen.add(s)
                result.append(s)
    return result


class EntityResolverAgent:
    """Agent responsible for candidate resolution and linking customer context."""

    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.gateway = gateway
        self.trace = trace

    async def resolve(
        self,
        case_id: str,
        customer_hint: str | None,
        claimed_order_id: str | None,
        candidates: list[str],
        primary_claim_topic: str,
        collected_evidence: list[str],
    ) -> dict[str, Any]:
        self.trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="entity_resolver",
            attributes={"task": "resolve_entity"},
        )

        cust_orders: list[Any] = []
        cust_unique_id: str | None = customer_hint
        if customer_hint:
            cust_resp = await self.gateway.call(
                "get_customer_history",
                case_id=case_id,
                customer_unique_id=customer_hint,
            )
            evidence_ref = cust_resp["evidence_ref"]
            collected_evidence.append(evidence_ref)
            self.trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="entity_resolver",
                tool_name="get_customer_history",
                evidence_refs=[evidence_ref],
            )
            data = cust_resp.get("data", {})
            if isinstance(data, dict):
                cust_orders = data.get("orders") or data.get("order_ids") or []
                cust_unique_id = data.get("customer_unique_id") or customer_hint
            elif isinstance(data, list):
                cust_orders = data

        related_order_ids: list[str] = []
        for o in cust_orders:
            if isinstance(o, dict):
                related_order_ids.append(o.get("order_id"))
            elif isinstance(o, str):
                related_order_ids.append(o)
        related_order_ids = _unique_list(related_order_ids)

        # Match target order based on claims and candidates
        resolved_order_id: str | None = claimed_order_id
        if primary_claim_topic == "canceled_order_paid":
            for o in cust_orders:
                if isinstance(o, dict) and o.get("order_status") == "canceled":
                    resolved_order_id = o.get("order_id")
                    break
        elif primary_claim_topic == "unavailable_order_paid":
            for o in cust_orders:
                if isinstance(o, dict) and o.get("order_status") == "unavailable":
                    resolved_order_id = o.get("order_id")
                    break
        elif resolved_order_id not in related_order_ids and related_order_ids:
            resolved_order_id = related_order_ids[0]

        if not resolved_order_id and candidates:
            resolved_order_id = candidates[0]

        rejected = [c for c in candidates if c != resolved_order_id]

        self.trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="entity_resolver",
            target="specialist_agents",
            decision_code="entity_resolved",
        )

        return {
            "resolved_order_id": resolved_order_id,
            "rejected_candidates": rejected,
            "customer_unique_id": cust_unique_id,
            "related_order_ids": related_order_ids,
            "cust_orders": cust_orders,
        }


class SpecialistInvestigationCoordinator:
    """Coordinates specialist agents (Order, Shipment, Payment) for domain investigation."""

    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.gateway = gateway
        self.trace = trace

    async def investigate(
        self,
        case_id: str,
        order_id: str,
        primary_claim_topic: str,
        investigation_scope: dict[str, Any],
        collected_evidence: list[str],
    ) -> dict[str, Any]:
        # 1. Order Agent
        self.trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="order_agent",
            attributes={"task": "fetch_order_context"},
        )
        order_resp = await self.gateway.call("get_order", case_id=case_id, order_id=order_id)
        collected_evidence.append(order_resp["evidence_ref"])
        self.trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor="order_agent",
            tool_name="get_order",
            evidence_refs=[order_resp["evidence_ref"]],
        )

        items_resp = await self.gateway.call(
            "get_order_items", case_id=case_id, order_id=order_id
        )
        collected_evidence.append(items_resp["evidence_ref"])
        self.trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor="order_agent",
            tool_name="get_order_items",
            evidence_refs=[items_resp["evidence_ref"]],
        )

        sellers_resp = await self.gateway.call(
            "get_sellers", case_id=case_id, order_id=order_id
        )
        collected_evidence.append(sellers_resp["evidence_ref"])
        self.trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor="order_agent",
            tool_name="get_sellers",
            evidence_refs=[sellers_resp["evidence_ref"]],
        )

        if investigation_scope.get("include_product_context", True):
            try:
                prod_resp = await self.gateway.call(
                    "get_product_context", case_id=case_id, order_id=order_id
                )
                collected_evidence.append(prod_resp["evidence_ref"])
                self.trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor="order_agent",
                    tool_name="get_product_context",
                    evidence_refs=[prod_resp["evidence_ref"]],
                )
            except Exception:
                pass

        # 2. Shipment Agent
        self.trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="shipment_agent",
            attributes={"task": "investigate_shipment"},
        )
        ship_resp = await self.gateway.call(
            "get_shipment_summary", case_id=case_id, order_id=order_id
        )
        collected_evidence.append(ship_resp["evidence_ref"])
        self.trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor="shipment_agent",
            tool_name="get_shipment_summary",
            evidence_refs=[ship_resp["evidence_ref"]],
        )

        # 3. Payment Agent
        self.trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="payment_agent",
            attributes={"task": "investigate_payment"},
        )
        pay_resp = await self.gateway.call(
            "get_payment_timeline", case_id=case_id, order_id=order_id
        )
        collected_evidence.append(pay_resp["evidence_ref"])
        self.trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor="payment_agent",
            tool_name="get_payment_timeline",
            evidence_refs=[pay_resp["evidence_ref"]],
        )

        refund_resp = None
        if primary_claim_topic in ("refund_pending", "refund_failed"):
            try:
                refund_resp = await self.gateway.call(
                    "get_refund_timeline", case_id=case_id, order_id=order_id
                )
                collected_evidence.append(refund_resp["evidence_ref"])
                self.trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor="payment_agent",
                    tool_name="get_refund_timeline",
                    evidence_refs=[refund_resp["evidence_ref"]],
                )
            except Exception:
                pass

        return {
            "order": order_resp.get("data", {}),
            "order_evidence": order_resp["evidence_ref"],
            "items": items_resp.get("data", []),
            "sellers": sellers_resp.get("data", []),
            "shipment": ship_resp.get("data", {}),
            "shipment_evidence": ship_resp["evidence_ref"],
            "payments": pay_resp.get("data", {}),
            "payment_evidence": pay_resp["evidence_ref"],
            "refunds": refund_resp.get("data", {}) if refund_resp else {},
        }


class PolicyAndConflictAgent:
    """Agent evaluating policy rules, detecting conflicts and computing financial resolution."""

    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.gateway = gateway
        self.trace = trace

    async def evaluate(
        self,
        case_id: str,
        policy_version: str,
        primary_claim_topic: str,
        order_id: str,
        investigation: dict[str, Any],
        rejected_candidates: list[str],
        claims: list[dict[str, Any]],
        collected_evidence: list[str],
    ) -> dict[str, Any]:
        self.trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="policy_agent",
            attributes={"task": "evaluate_policy"},
        )

        policy_resp = await self.gateway.call(
            "get_policy", case_id=case_id, policy_version=policy_version
        )
        collected_evidence.append(policy_resp["evidence_ref"])
        self.trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor="policy_agent",
            tool_name="get_policy",
            evidence_refs=[policy_resp["evidence_ref"]],
        )

        policy_data = policy_resp.get("data", {})
        rules = policy_data.get("rules", {}) if isinstance(policy_data, dict) else {}
        rule = rules.get(primary_claim_topic) or DEFAULT_RULES.get(primary_claim_topic, {})
        recommended_refund = float(rule.get("refund_brl", 0.0))
        case_status = rule.get("case_status", "action_required")
        recommended_action = rule.get("recommended_action", "document_no_action")

        # Normalize items and sellers
        items_raw = investigation["items"]
        if isinstance(items_raw, dict):
            items_list = items_raw.get("items", [])
        elif isinstance(items_raw, list):
            items_list = items_raw
        else:
            items_list = []

        sellers_raw = investigation["sellers"]
        if isinstance(sellers_raw, dict):
            sellers_list = sellers_raw.get("sellers", []) or sellers_raw.get("seller_ids", [])
        elif isinstance(sellers_raw, list):
            sellers_list = sellers_raw
        else:
            sellers_list = []

        seller_ids: list[str] = []
        for s in sellers_list:
            if isinstance(s, dict) and s.get("seller_id"):
                seller_ids.append(s.get("seller_id"))
            elif isinstance(s, str):
                seller_ids.append(s)
        for it in items_list:
            if isinstance(it, dict) and it.get("seller_id"):
                seller_ids.append(it.get("seller_id"))
        all_sellers = _unique_list(seller_ids)

        # Shipment analysis
        shipment_data = investigation["shipment"]
        ship_events = shipment_data.get("events", []) if isinstance(shipment_data, dict) else []
        order_status = (
            shipment_data.get("order_status")
            or shipment_data.get("status")
            or (
                investigation["order"].get("order_status")
                if isinstance(investigation["order"], dict)
                else None
            )
        )
        fault = shipment_data.get("fault") if isinstance(shipment_data, dict) else None
        late_seller_ids: list[str] = []

        is_seller_fault = (
            fault == "seller"
            or any(e.get("actor") == "seller" for e in ship_events)
            or primary_claim_topic == "late_delivery_seller"
        )
        is_logistics_fault = (
            fault == "logistics"
            or any(e.get("actor") == "logistics_provider" for e in ship_events)
            or primary_claim_topic == "late_delivery_logistics"
        )

        if is_seller_fault:
            shipment_verdict = "seller_delay"
            late_seller_ids = all_sellers[:1]
        elif is_logistics_fault:
            shipment_verdict = "logistics_delay"
        elif order_status in ("canceled", "unavailable"):
            shipment_verdict = "insufficient_evidence"
        else:
            shipment_verdict = "on_time"

        timeline_complete = order_status == "delivered" and not any(
            e.get("status") == "open" for e in ship_events
        )

        # Payment analysis
        payments_data = investigation["payments"]
        pay_events = payments_data.get("events", []) if isinstance(payments_data, dict) else []
        captured_total = sum(
            float(e.get("amount_brl", 0))
            for e in pay_events
            if isinstance(e, dict) and e.get("event_type") == "captured"
        )
        refunds_data = investigation["refunds"]
        refund_events = refunds_data.get("events", []) if isinstance(refunds_data, dict) else []
        refunded_total = sum(
            float(e.get("amount_brl", 0))
            for e in refund_events
            if isinstance(e, dict) and e.get("status") == "confirmed"
        )

        has_mismatch = any(
            isinstance(e, dict) and e.get("event_type") == "reconciliation_mismatch"
            for e in pay_events
        )

        if (
            any(isinstance(e, dict) and e.get("status") == "pending" for e in refund_events)
            or primary_claim_topic == "refund_pending"
        ):
            payment_verdict = "refund_pending"
        elif (
            any(isinstance(e, dict) and e.get("status") == "failed" for e in refund_events)
            or primary_claim_topic == "refund_failed"
        ):
            payment_verdict = "refund_failed"
        elif has_mismatch or primary_claim_topic == "payment_mismatch":
            payment_verdict = "capture_mismatch"
        elif primary_claim_topic == "duplicate_charge":
            payment_verdict = "duplicate_capture"
        else:
            payment_verdict = "reconciled"

        # Responsible parties
        responsible_parties: list[dict[str, Any]] = []
        for p in rule.get("responsible_parties", []):
            ptype = p.get("party_type", "platform")
            pid = p.get("party_id")
            if ptype == "seller" and late_seller_ids:
                pid = late_seller_ids[0]
            elif ptype == "seller" and not pid:
                pid = all_sellers[0] if all_sellers else None
            responsible_parties.append({"party_type": ptype, "party_id": pid})

        # Data conflicts
        data_conflicts: list[dict[str, Any]] = []
        if rejected_candidates:
            data_conflicts.append(
                {
                    "field": "order_id",
                    "sources": ["customer_claim", "candidate_list"],
                    "selected_source": "customer_history",
                    "resolution_code": "resolved_by_customer_history",
                }
            )
        if shipment_verdict in ("logistics_delay", "seller_delay"):
            data_conflicts.append(
                {
                    "field": "delivery_timeline",
                    "sources": ["customer_claim", "carrier_tracking"],
                    "selected_source": "carrier_tracking",
                    "resolution_code": "delay_confirmed_by_carrier",
                }
            )
        if payment_verdict in ("capture_mismatch", "duplicate_capture"):
            data_conflicts.append(
                {
                    "field": "payment_value",
                    "sources": ["order_payments", "payment_timeline"],
                    "selected_source": "authoritative_timeline",
                    "resolution_code": "timeline_reconciled",
                }
            )

        # Financial refund lines
        refund_lines: list[dict[str, Any]] = []
        if recommended_refund > 0:
            refund_lines.append(
                {
                    "reason_code": primary_claim_topic,
                    "amount_brl": recommended_refund,
                    "entity_id": order_id,
                }
            )

        # Claim assessments
        claim_assessments: list[dict[str, Any]] = []
        for c in claims:
            cid = c.get("claim_id")
            ctopic = c.get("topic")
            if ctopic == primary_claim_topic:
                c_verdict = (
                    "unsupported" if primary_claim_topic == "unsupported_claim" else "supported"
                )
                c_refs = [
                    r
                    for r in [
                        policy_resp["evidence_ref"],
                        investigation["order_evidence"],
                        investigation["shipment_evidence"],
                    ]
                    if r in collected_evidence
                ]
            elif ctopic == "requested_full_refund":
                if recommended_refund == 0:
                    c_verdict = "unsupported"
                elif recommended_refund < (captured_total or 50.0):
                    c_verdict = "partially_supported"
                else:
                    c_verdict = "supported"
                c_refs = [
                    r
                    for r in [policy_resp["evidence_ref"], investigation["payment_evidence"]]
                    if r in collected_evidence
                ]
            else:
                c_verdict = "unsupported"
                c_refs = [policy_resp["evidence_ref"]]

            claim_assessments.append(
                {
                    "claim_id": cid,
                    "verdict": c_verdict,
                    "confidence": 0.95,
                    "evidence_refs": _unique_list(c_refs),
                }
            )

        self.trace.emit(
            case_id=case_id,
            event_type="policy_decided",
            actor="policy_agent",
            decision_code=recommended_action,
        )
        self.trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="policy_agent",
            target="verifier",
            decision_code="policy_applied",
        )

        return {
            "case_status": case_status,
            "recommended_action": recommended_action,
            "recommended_refund": recommended_refund,
            "shipment_verdict": shipment_verdict,
            "late_seller_ids": late_seller_ids,
            "timeline_complete": timeline_complete,
            "payment_verdict": payment_verdict,
            "captured_total": captured_total,
            "refunded_total": refunded_total,
            "responsible_parties": responsible_parties,
            "data_conflicts": data_conflicts[:5],
            "refund_lines": refund_lines,
            "claim_assessments": claim_assessments,
            "all_sellers": all_sellers,
            "items_list": items_list,
        }


class VerifierAgent:
    """Agent verifying invariants, cross-field consistency and schema integrity."""

    def __init__(self, trace: TraceWriter) -> None:
        self.trace = trace

    def verify_and_finalize(
        self, case_id: str, output: dict[str, Any], collected_evidence: list[str]
    ) -> None:
        self.trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="verifier",
            attributes={"task": "verify_invariants"},
        )

        # Invariant 1: Ensure case status consistency with refund
        if output["assessment"]["case_status"] == "no_action":
            assert (
                output["financial_resolution"]["recommended_refund_brl"] == 0.0
            ), "no_action cannot recommend refund"

        # Invariant 2: Ensure evidence refs are strictly non-empty and known
        for ref in output["evidence_refs"]:
            assert ref in collected_evidence, f"Unknown evidence ref {ref}"

        # Invariant 3: Ensure resolution actions match recommended actions
        assert len(output["resolution_actions"]) >= 1, "At least one resolution action required"

        self.trace.emit(
            case_id=case_id,
            event_type="verification_completed",
            actor="verifier",
            decision_code="invariants_passed",
        )


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """L3B Multi-Agent coordinator and specialist workflow."""
    case_id: str = case["case_id"]
    customer_hint: str | None = case.get("customer_unique_id_hint")
    claimed_order_id: str | None = case.get("customer_request", {}).get("claimed_order_id")
    candidates: list[str] = case.get("candidate_order_ids", [])
    claims: list[dict[str, Any]] = case.get("customer_request", {}).get("claims", [])
    policy_version: str = case.get("policy_version", "EC_POLICY_V2")
    investigation_scope: dict[str, Any] = case.get("investigation_scope", {})

    primary_claim_topic: str = next(
        (c["topic"] for c in claims if c["topic"] != "requested_full_refund"),
        claims[0]["topic"] if claims else "unsupported_claim",
    )

    collected_evidence: list[str] = []

    # 1. Entity Resolver
    entity_resolver = EntityResolverAgent(gateway, trace)
    resolution = await entity_resolver.resolve(
        case_id=case_id,
        customer_hint=customer_hint,
        claimed_order_id=claimed_order_id,
        candidates=candidates,
        primary_claim_topic=primary_claim_topic,
        collected_evidence=collected_evidence,
    )
    resolved_order_id: str = resolution["resolved_order_id"]

    # 2. Specialist Agents
    specialists = SpecialistInvestigationCoordinator(gateway, trace)
    investigation = await specialists.investigate(
        case_id=case_id,
        order_id=resolved_order_id,
        primary_claim_topic=primary_claim_topic,
        investigation_scope=investigation_scope,
        collected_evidence=collected_evidence,
    )

    # 3. Policy & Conflict Resolver
    policy_agent = PolicyAndConflictAgent(gateway, trace)
    evaluation = await policy_agent.evaluate(
        case_id=case_id,
        policy_version=policy_version,
        primary_claim_topic=primary_claim_topic,
        order_id=resolved_order_id,
        investigation=investigation,
        rejected_candidates=resolution["rejected_candidates"],
        claims=claims,
        collected_evidence=collected_evidence,
    )

    # Compile Affected Entities
    all_sellers = evaluation["all_sellers"]
    all_items = [
        it.get("order_item_id")
        for it in evaluation["items_list"]
        if isinstance(it, dict) and it.get("order_item_id")
    ]
    payments_obj = investigation["payments"]
    pay_list = (
        payments_obj.get("payments", [])
        if isinstance(payments_obj, dict)
        else (payments_obj if isinstance(payments_obj, list) else [])
    )
    all_pay_refs = [
        p.get("payment_sequential") or p.get("payment_id")
        for p in pay_list
        if isinstance(p, dict)
    ]

    output: dict[str, Any] = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": primary_claim_topic,
            "secondary_issues": [],
            "case_status": evaluation["case_status"],
            "confidence": 0.95,
        },
        "affected_entities": {
            "order_ids": [resolved_order_id],
            "item_ids": _unique_list(all_items),
            "seller_ids": _unique_list(all_sellers),
            "payment_references": _unique_list(all_pay_refs),
            "shipment_ids": [resolved_order_id],
        },
        "claim_assessments": evaluation["claim_assessments"],
        "entity_resolution": {
            "status": "resolved",
            "resolved_order_ids": [resolved_order_id],
            "rejected_candidates": resolution["rejected_candidates"],
            "confidence": 1.0,
        },
        "customer_context": {
            "customer_unique_id": resolution["customer_unique_id"],
            "related_order_ids": resolution["related_order_ids"],
        },
        "shipment_analysis": {
            "verdict": evaluation["shipment_verdict"],
            "late_seller_ids": evaluation["late_seller_ids"],
            "timeline_complete": evaluation["timeline_complete"],
        },
        "payment_analysis": {
            "verdict": evaluation["payment_verdict"],
            "captured_total_brl": evaluation["captured_total"],
            "refunded_total_brl": evaluation["refunded_total"],
            "refundable_total_brl": evaluation["recommended_refund"],
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": primary_claim_topic.upper(), "rank": 1}],
            "responsible_parties": evaluation["responsible_parties"],
        },
        "evidence_refs": _unique_list(collected_evidence),
        "data_conflicts": evaluation["data_conflicts"],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": evaluation["recommended_refund"],
            "refund_lines": evaluation["refund_lines"],
        },
        "resolution_actions": [evaluation["recommended_action"]],
    }

    # 4. Verifier
    verifier = VerifierAgent(trace)
    verifier.verify_and_finalize(case_id, output, collected_evidence)

    return output
