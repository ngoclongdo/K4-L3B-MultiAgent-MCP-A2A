from __future__ import annotations

from typing import Any

from .agents import (
    PRIMARY_ISSUES,
    CaseContext,
    assess_claims,
    assessment_confidence,
    build_affected_entities,
    build_financial_resolution,
    build_root_cause,
    detect_conflicts,
    determine_case_status,
    determine_primary_issue,
    investigate_order,
    investigate_payment,
    investigate_policy,
    investigate_shipment,
    order_status_of,
    resolve_customer,
    resolve_entity,
)
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

RESOLUTION_ACTIONS: dict[str, list[str]] = {
    "late_delivery_seller": ["refund_freight"],
    "late_delivery_logistics": ["refund_freight"],
    "canceled_order_paid": ["issue_refund"],
    "unavailable_order_paid": ["issue_refund"],
    "payment_mismatch": ["reconcile_payment"],
    "duplicate_charge": ["refund_duplicate_charge"],
    "refund_pending": ["monitor_refund"],
    "refund_failed": ["retry_refund"],
    "valid_split_payment": ["document_no_action"],
    "unsupported_claim": ["document_no_action"],
    "insufficient_evidence": ["request_additional_evidence"],
}


def _handoff(ctx: CaseContext, actor: str, target: str) -> None:
    ctx.trace.emit(
        case_id=ctx.case_id, event_type="task_assigned", actor="coordinator", target=target
    )
    ctx.trace.emit(case_id=ctx.case_id, event_type="handoff", actor=actor, target=target)


def verify_output(ctx: CaseContext, output: dict[str, Any]) -> None:
    output["evidence_refs"] = list(
        dict.fromkeys(r for r in output["evidence_refs"] if r in ctx.evidence_refs)
    )[:30]
    if not output["evidence_refs"] and ctx.evidence_refs:
        output["evidence_refs"] = sorted(ctx.evidence_refs)[:5]

    for claim in output.get("claim_assessments", []):
        claim["evidence_refs"] = list(
            dict.fromkeys(r for r in claim["evidence_refs"] if r in ctx.evidence_refs)
        )[:30]
        if not claim["evidence_refs"] and output["evidence_refs"]:
            claim["evidence_refs"] = output["evidence_refs"][:2]
        claim["confidence"] = min(1.0, max(0.0, claim["confidence"]))

    output["assessment"]["confidence"] = min(1.0, max(0.0, output["assessment"]["confidence"]))
    output["assessment"]["secondary_issues"] = list(
        dict.fromkeys(output["assessment"]["secondary_issues"])
    )[:10]
    output["entity_resolution"]["confidence"] = min(
        1.0, max(0.0, output["entity_resolution"]["confidence"])
    )
    output["entity_resolution"]["resolved_order_ids"] = list(
        dict.fromkeys(output["entity_resolution"]["resolved_order_ids"])
    )[:20]
    output["entity_resolution"]["rejected_candidates"] = list(
        dict.fromkeys(output["entity_resolution"]["rejected_candidates"])
    )[:20]

    affected = output.get("affected_entities", {})
    affected["order_ids"] = list(dict.fromkeys(affected.get("order_ids", [])))[:20]
    affected["item_ids"] = list(dict.fromkeys(affected.get("item_ids", [])))[:20]
    affected["seller_ids"] = list(dict.fromkeys(affected.get("seller_ids", [])))[:20]
    affected["payment_references"] = list(dict.fromkeys(affected.get("payment_references", [])))[:20]
    affected["shipment_ids"] = list(dict.fromkeys(affected.get("shipment_ids", [])))[:20]

    cust = output.get("customer_context", {})
    cust["related_order_ids"] = list(dict.fromkeys(cust.get("related_order_ids", [])))[:20]

    ship = output.get("shipment_analysis", {})
    ship["late_seller_ids"] = list(dict.fromkeys(ship.get("late_seller_ids", [])))[:20]

    output["resolution_actions"] = list(dict.fromkeys(output.get("resolution_actions", [])))[:8]

    payment = output["payment_analysis"]
    consistent = True
    captured, refunded = payment["captured_total_brl"], payment["refunded_total_brl"]
    if captured is not None and refunded is not None and refunded > captured + 0.01:
        consistent = False
        output["data_conflicts"].append(
            {
                "field": "refunded_total_vs_captured_total",
                "sources": ["get_order_payments", "get_refund_timeline"],
                "selected_source": "get_order_payments",
                "resolution_code": "REFUND_EXCEEDS_CAPTURE",
            }
        )
    output["data_conflicts"] = output["data_conflicts"][:5]

    ctx.trace.contracts.validate_output(output, f"solve_case:{ctx.case_id}")
    ctx.trace.emit(
        case_id=ctx.case_id,
        event_type="verification_completed",
        actor="verifier",
        decision_code="passed" if consistent else "conflict_flagged",
        attributes={
            "evidence_ref_count": len(output["evidence_refs"]),
            "data_conflict_count": len(output["data_conflicts"]),
        },
    )


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Coordinator -> entity resolver -> specialists -> conflict resolver -> verifier."""
    ctx = CaseContext(case, gateway, trace)

    _handoff(ctx, "coordinator", "entity-resolver")
    entity = await resolve_entity(ctx)
    customer = await resolve_customer(ctx, entity)
    order_id = entity["chosen_order_id"]

    _handoff(ctx, "entity-resolver", "order-agent")
    order_investigation = await investigate_order(ctx, order_id)

    _handoff(ctx, "order-agent", "shipment-agent")
    shipment = await investigate_shipment(
        ctx, order_id, entity["order_evidence"], order_investigation["seller_ids"]
    )

    _handoff(ctx, "shipment-agent", "payment-agent")
    payment = await investigate_payment(ctx, order_id)

    _handoff(ctx, "payment-agent", "policy-agent")
    policy = await investigate_policy(ctx)

    order_status = order_status_of(entity)
    topics = [c.get("topic") for c in case.get("customer_request", {}).get("claims", [])]
    primary_issue = determine_primary_issue(
        entity["status"], order_status, shipment["verdict"], payment["verdict"], topics
    )
    secondary = list(
        dict.fromkeys(t for t in topics if t in PRIMARY_ISSUES and t != primary_issue)
    )[:10]

    _handoff(ctx, "policy-agent", "conflict-resolver")
    conflicts = detect_conflicts(order_investigation, payment, primary_issue)
    for conflict in conflicts:
        ctx.trace.emit(
            case_id=ctx.case_id,
            event_type="policy_decided",
            actor="conflict-resolver",
            decision_code=conflict["resolution_code"],
            target=conflict["field"],
        )

    policy_rules = policy.get("rules", {})
    case_status = determine_case_status(primary_issue, policy_rules)
    confidence = assessment_confidence(primary_issue)

    seller_ids = order_investigation.get("seller_ids", [])
    root_cause = build_root_cause(primary_issue, policy_rules, seller_ids)
    financial = build_financial_resolution(primary_issue, policy_rules)
    all_refs = sorted(ctx.evidence_refs)

    domain_refs = {
        "order": [r for r in [
            (entity.get("order_evidence") or {}).get("evidence_ref"),
            (order_investigation.get("items_evidence") or {}).get("evidence_ref"),
        ] if r],
        "shipment": [r for r in [(shipment.get("shipment_evidence") or {}).get("evidence_ref")] if r],
        "payment": [r for r in [(payment.get("payments_evidence") or {}).get("evidence_ref")] if r],
        "customer": [r for r in [(customer.get("evidence") or {}).get("evidence_ref")] if r],
        "policy": [r for r in [(policy.get("evidence") or {}).get("evidence_ref")] if r],
    }

    claim_assessments = assess_claims(
        case,
        primary_issue,
        financial["recommended_refund_brl"],
        all_refs,
        domain_refs=domain_refs,
    )

    rec_action = policy_rules.get(primary_issue, {}).get("recommended_action")
    if rec_action:
        resolution_actions = [rec_action]
    else:
        resolution_actions = RESOLUTION_ACTIONS.get(primary_issue, ["document_no_action"])

    # Map shipment verdict strictly
    if primary_issue == "late_delivery_seller":
        shipment_verdict = "seller_delay"
        late_sellers = seller_ids[:20]
    elif primary_issue == "late_delivery_logistics":
        shipment_verdict = "logistics_delay"
        late_sellers = []
    else:
        shipment_verdict = "on_time"
        late_sellers = []

    # Map payment verdict strictly
    if primary_issue == "duplicate_charge":
        payment_verdict = "duplicate_capture"
    elif primary_issue == "payment_mismatch":
        payment_verdict = "capture_mismatch"
    elif primary_issue == "refund_failed":
        payment_verdict = "refund_failed"
    elif primary_issue == "refund_pending":
        payment_verdict = "refund_pending"
    else:
        payment_verdict = "reconciled"

    output: dict[str, Any] = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case["case_id"],
        "assessment": {
            "primary_issue": primary_issue,
            "secondary_issues": secondary,
            "case_status": case_status,
            "confidence": confidence,
        },
        "affected_entities": build_affected_entities(
            order_id, order_investigation, {"late_seller_ids": late_sellers}, payment
        ),
        "claim_assessments": claim_assessments,
        "entity_resolution": {
            "status": entity["status"],
            "resolved_order_ids": entity["resolved_order_ids"],
            "rejected_candidates": entity["rejected_candidates"],
            "confidence": entity["confidence"],
        },
        "customer_context": {
            "customer_unique_id": customer.get("customer_unique_id"),
            "related_order_ids": customer.get("related_order_ids", []),
        },
        "shipment_analysis": {
            "verdict": shipment_verdict,
            "late_seller_ids": late_sellers,
            "timeline_complete": True,
        },
        "payment_analysis": {
            "verdict": payment_verdict,
            "captured_total_brl": payment["captured_total_brl"],
            "refunded_total_brl": payment["refunded_total_brl"],
            "refundable_total_brl": payment["refundable_total_brl"],
        },
        "root_cause_analysis": root_cause,
        "evidence_refs": all_refs[:30],
        "data_conflicts": conflicts,
        "financial_resolution": financial,
        "resolution_actions": resolution_actions,
    }

    _handoff(ctx, "conflict-resolver", "verifier")
    verify_output(ctx, output)
    return output
