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
    "late_delivery_seller": ["notify_seller", "offer_refund"],
    "late_delivery_logistics": ["escalate_logistics_partner", "offer_refund"],
    "canceled_order_paid": ["issue_refund"],
    "unavailable_order_paid": ["issue_refund"],
    "payment_mismatch": ["escalate_payment_reconciliation"],
    "duplicate_charge": ["reverse_duplicate_charge"],
    "refund_pending": ["monitor_refund_completion"],
    "refund_failed": ["retry_refund", "escalate_payment_provider"],
    "valid_split_payment": ["no_action_required"],
    "unsupported_claim": ["close_case_no_action"],
    "insufficient_evidence": ["request_additional_evidence"],
}


def _handoff(ctx: CaseContext, actor: str, target: str) -> None:
    ctx.trace.emit(
        case_id=ctx.case_id, event_type="task_assigned", actor="coordinator", target=target
    )
    ctx.trace.emit(case_id=ctx.case_id, event_type="handoff", actor=actor, target=target)


def verify_output(ctx: CaseContext, output: dict[str, Any]) -> None:
    output["evidence_refs"] = [r for r in output["evidence_refs"] if r in ctx.evidence_refs]
    for claim in output.get("claim_assessments", []):
        claim["evidence_refs"] = [r for r in claim["evidence_refs"] if r in ctx.evidence_refs]
        claim["confidence"] = min(1.0, max(0.0, claim["confidence"]))
    output["assessment"]["confidence"] = min(1.0, max(0.0, output["assessment"]["confidence"]))
    output["entity_resolution"]["confidence"] = min(
        1.0, max(0.0, output["entity_resolution"]["confidence"])
    )

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
    await investigate_policy(ctx)

    _handoff(ctx, "policy-agent", "conflict-resolver")
    conflicts = detect_conflicts(order_investigation, shipment, payment)
    for conflict in conflicts:
        ctx.trace.emit(
            case_id=ctx.case_id,
            event_type="policy_decided",
            actor="conflict-resolver",
            decision_code=conflict["resolution_code"],
            target=conflict["field"],
        )

    order_status = order_status_of(entity)
    topics = [c.get("topic") for c in case.get("customer_request", {}).get("claims", [])]
    primary_issue = determine_primary_issue(
        entity["status"], order_status, shipment["verdict"], payment["verdict"], topics
    )
    secondary = list(
        dict.fromkeys(t for t in topics if t in PRIMARY_ISSUES and t != primary_issue)
    )[:10]
    case_status = determine_case_status(entity["status"], primary_issue)
    confidence = assessment_confidence(
        entity["confidence"], shipment["verdict"], payment["verdict"], primary_issue
    )

    root_cause = build_root_cause(primary_issue, shipment["verdict"], payment["verdict"])
    financial = build_financial_resolution(payment, primary_issue)
    claim_assessments = assess_claims(
        case, order_status, shipment, payment, financial["recommended_refund_brl"]
    )

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
            order_id, order_investigation, shipment, payment
        ),
        "claim_assessments": claim_assessments,
        "entity_resolution": {
            "status": entity["status"],
            "resolved_order_ids": entity["resolved_order_ids"],
            "rejected_candidates": entity["rejected_candidates"],
            "confidence": entity["confidence"],
        },
        "customer_context": customer,
        "shipment_analysis": {
            "verdict": shipment["verdict"],
            "late_seller_ids": shipment["late_seller_ids"],
            "timeline_complete": shipment["timeline_complete"],
        },
        "payment_analysis": {
            "verdict": payment["verdict"],
            "captured_total_brl": payment["captured_total_brl"],
            "refunded_total_brl": payment["refunded_total_brl"],
            "refundable_total_brl": payment["refundable_total_brl"],
        },
        "root_cause_analysis": root_cause,
        "evidence_refs": sorted(ctx.evidence_refs)[:30],
        "data_conflicts": conflicts,
        "financial_resolution": financial,
        "resolution_actions": RESOLUTION_ACTIONS.get(
            primary_issue, ["request_additional_evidence"]
        ),
    }

    _handoff(ctx, "conflict-resolver", "verifier")
    verify_output(ctx, output)
    return output
