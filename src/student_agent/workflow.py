"""L3B Multi-Agent Workflow — E-commerce Complaint Investigation.

Architecture:
  Coordinator ─► Entity Resolver ─► Specialist Agents ─► Policy Agent
                                     ├── Order/Product Agent
                                     ├── Shipment Agent
                                     ├── Payment/Refund Agent
                                     └── Customer Agent
               ─► Conflict Resolver ─► Verifier ─► Output

All agents communicate through the coordinator via structured dicts.
MCP calls are made exclusively through the provided EvidenceGateway.
Trace events are emitted at each observable boundary.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# MCP SDK compatibility patch — MUST execute before gateway.call() is used.
#
# The starter-kit mcp_gateway.py accesses ``result.isError`` (camelCase) but
# MCP SDK >=2 renamed the attribute to ``is_error`` (snake_case). We add
# ``isError`` as a property alias on the class so the original gateway code
# works without modification.
# ---------------------------------------------------------------------------
try:
    from mcp.types import CallToolResult as _CallToolResult

    if not hasattr(_CallToolResult, "isError"):
        _CallToolResult.isError = property(  # type: ignore[attr-defined]
            lambda self: self.is_error
        )
except Exception:  # pragma: no cover — guard against any import issue
    pass

import asyncio
import logging
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants & mappings
# ---------------------------------------------------------------------------

# Map claim topics from case input to primary-issue enum values.
_TOPIC_TO_ISSUE: dict[str, str] = {
    "canceled_order_paid": "canceled_order_paid",
    "unavailable_order_paid": "unavailable_order_paid",
    "late_delivery_seller": "late_delivery_seller",
    "late_delivery_logistics": "late_delivery_logistics",
    "valid_split_payment": "valid_split_payment",
    "payment_mismatch": "payment_mismatch",
    "duplicate_charge": "duplicate_charge",
    "refund_pending": "refund_pending",
    "refund_failed": "refund_failed",
    "requested_full_refund": "refund_pending",
    "unsupported_claim": "unsupported_claim",
}

# Map primary-issue enum to root-cause code (must match ^[A-Z][A-Z0-9_]{2,79}$).
_ISSUE_TO_CAUSE: dict[str, str] = {
    "canceled_order_paid": "CANCELED_ORDER_WITH_CAPTURE",
    "unavailable_order_paid": "UNAVAILABLE_ITEM_CHARGED",
    "late_delivery_seller": "SELLER_SHIPPING_SLA_BREACH",
    "late_delivery_logistics": "LOGISTICS_TRANSIT_DELAY",
    "valid_split_payment": "VALID_SPLIT_PAYMENT_STRUCTURE",
    "payment_mismatch": "PAYMENT_CAPTURE_MISMATCH",
    "duplicate_charge": "DUPLICATE_PAYMENT_CAPTURE",
    "refund_pending": "REFUND_PROCESSING_PENDING",
    "refund_failed": "REFUND_PROCESSING_FAILURE",
    "unsupported_claim": "UNSUPPORTED_CLAIM_TYPE",
    "insufficient_evidence": "INSUFFICIENT_EVIDENCE_AVAILABLE",
}

# Cached tool list (populated once per process, shared across all cases).
_discovered_tools: set[str] | None = None

# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------


async def _get_tools(gateway: EvidenceGateway) -> set[str]:
    """Return cached set of available MCP tool names."""
    global _discovered_tools
    if _discovered_tools is None:
        try:
            _discovered_tools = set(await gateway.list_tools())
            logger.info("Discovered MCP tools: %s", _discovered_tools)
        except Exception as exc:
            logger.debug("Tool discovery failed: %s", exc)
            _discovered_tools = set()
    return _discovered_tools


def _find_tool(available: set[str], *candidates: str) -> str | None:
    """Return the first tool name that exists in *available*, or None."""
    for name in candidates:
        if name in available:
            return name
    return None


async def _safe_call(
    gateway: EvidenceGateway,
    tool_name: str | None,
    *,
    case_id: str,
    **kwargs: str,
) -> dict[str, Any] | None:
    """Call an MCP tool; return None on any failure instead of raising."""
    if tool_name is None:
        return None
    try:
        return await gateway.call(tool_name, case_id=case_id, **kwargs)
    except Exception as exc:
        logger.debug("MCP tool %s failed for case %s: %s", tool_name, case_id, exc)
        return None


def _emit(
    trace: TraceWriter,
    *,
    case_id: str,
    event_type: str,
    actor: str,
    target: str | None = None,
    decision_code: str | None = None,
    tool_name: str | None = None,
    evidence_refs: list[str] | None = None,
    attributes: dict[str, str | int | float | bool | None] | None = None,
) -> None:
    """Thin wrapper around TraceWriter.emit — swallows unexpected errors."""
    try:
        trace.emit(
            case_id=case_id,
            event_type=event_type,
            actor=actor,
            target=target,
            decision_code=decision_code,
            tool_name=tool_name,
            evidence_refs=evidence_refs,
            attributes=attributes,
        )
    except Exception as exc:
        logger.debug("trace.emit failed for case %s: %s", case_id, exc)


# ---------------------------------------------------------------------------
# Data extraction helpers
# ---------------------------------------------------------------------------


def _get_str(data: dict, *keys: str) -> str | None:
    """Return the first non-empty string value from *data* matching *keys*."""
    for k in keys:
        v = data.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return None


def _get_float(data: dict, *keys: str) -> float:
    """Return the first numeric value found, default 0.0."""
    for k in keys:
        v = data.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return 0.0


def _get_list(data: dict, *keys: str) -> list:
    """Return the first list found under any of *keys*, default []."""
    for k in keys:
        v = data.get(k)
        if isinstance(v, list):
            return v
    return []


def _unique(items: list[str], limit: int = 20) -> list[str]:
    """Return deduplicated list of non-empty strings, capped at *limit*."""
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if not item or not isinstance(item, str):
            continue
        cleaned = item.strip()
        if not cleaned:
            continue
        cleaned = cleaned[:128]
        if cleaned not in seen:
            result.append(cleaned)
            seen.add(cleaned)
            if len(result) >= limit:
                break
    return result


def _is_synthetic_candidate(cand_id: str) -> bool:
    """Return True if candidate ID is clearly synthetic/fake."""
    if not isinstance(cand_id, str):
        return True
    clean = cand_id.strip()
    if clean.startswith("candidate-"):
        return True
    if len(clean) != 32 or not all(c in "0123456789abcdefABCDEF" for c in clean):
        return True
    return False


# ---------------------------------------------------------------------------
# 1. Entity Resolution Agent
# ---------------------------------------------------------------------------


async def _entity_resolution_agent(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    tools: set[str],
) -> dict[str, Any]:
    """Resolve the correct order among candidates and identify the customer."""
    case_id = case["case_id"]
    _emit(
        trace,
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="entity-agent",
    )

    candidates: list[str] = case.get("candidate_order_ids", [])
    claimed_id: str = case.get("customer_request", {}).get("claimed_order_id", "")
    customer_hint: str | None = case.get("customer_unique_id_hint")

    all_evidence_refs: list[str] = []
    resolved_ids: list[str] = []
    rejected_ids: list[str] = []
    order_data_map: dict[str, dict] = {}
    customer_unique_id: str | None = None
    customer_related_orders: list[str] = []

    order_tool = _find_tool(tools, "get_order")

    # Probe candidates: filter synthetic ones directly, probe plausible ones via MCP
    for cand_id in candidates:
        if _is_synthetic_candidate(cand_id):
            rejected_ids.append(cand_id)
            continue

        ev = await _safe_call(gateway, order_tool, case_id=case_id, order_id=cand_id)
        if ev is not None:
            ref = ev["evidence_ref"]
            all_evidence_refs.append(ref)
            _emit(
                trace,
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="entity-agent",
                tool_name=order_tool,
                evidence_refs=[ref],
            )
            data = ev.get("data") or {}
            if isinstance(data, dict):
                order_data_map[cand_id] = data
            resolved_ids.append(cand_id)
        else:
            rejected_ids.append(cand_id)

    # Fallback to claimed_id if nothing resolved and claimed_id was not yet probed
    if not resolved_ids and claimed_id and claimed_id not in candidates:
        if not _is_synthetic_candidate(claimed_id):
            ev = await _safe_call(gateway, order_tool, case_id=case_id, order_id=claimed_id)
            if ev is not None:
                ref = ev["evidence_ref"]
                all_evidence_refs.append(ref)
                _emit(
                    trace,
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor="entity-agent",
                    tool_name=order_tool,
                    evidence_refs=[ref],
                )
                data = ev.get("data") or {}
                if isinstance(data, dict):
                    order_data_map[claimed_id] = data
                resolved_ids.append(claimed_id)

    # Customer history investigation
    if customer_hint:
        cust_tool = _find_tool(tools, "get_customer_history")
        ev = await _safe_call(
            gateway, cust_tool, case_id=case_id, customer_unique_id=customer_hint
        )
        if ev is not None:
            ref = ev["evidence_ref"]
            all_evidence_refs.append(ref)
            _emit(
                trace,
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="entity-agent",
                tool_name=cust_tool,
                evidence_refs=[ref],
            )
            cust_data = ev.get("data") or {}
            if isinstance(cust_data, dict):
                customer_unique_id = (
                    _get_str(cust_data, "customer_unique_id", "unique_id", "customer_id")
                    or customer_hint
                )
                raw_orders = _get_list(cust_data, "order_ids", "orders", "related_orders")
                seen_oids: set[str] = set()
                for o in raw_orders:
                    if isinstance(o, dict):
                        oid = _get_str(o, "order_id", "id")
                    elif isinstance(o, str) and o:
                        oid = o
                    else:
                        oid = None
                    if oid and oid not in seen_oids:
                        customer_related_orders.append(oid)
                        seen_oids.add(oid)
            else:
                customer_unique_id = customer_hint
        else:
            customer_unique_id = customer_hint

    # Determine status and confidence
    if len(resolved_ids) == 1:
        status = "resolved"
        confidence = 0.95
    elif len(resolved_ids) > 1:
        status = "ambiguous"
        confidence = 0.70
    else:
        status = "not_found"
        confidence = 0.20

    _emit(
        trace,
        case_id=case_id,
        event_type="handoff",
        actor="entity-agent",
        target="coordinator",
        attributes={"status": status, "resolved_count": len(resolved_ids)},
    )

    return {
        "status": status,
        "resolved_order_ids": resolved_ids,
        "rejected_candidates": rejected_ids,
        "confidence": confidence,
        "order_data_map": order_data_map,
        "customer_unique_id": customer_unique_id or customer_hint,
        "customer_related_orders": customer_related_orders,
        "evidence_refs": all_evidence_refs,
    }


# ---------------------------------------------------------------------------
# 2. Order / Product Agent
# ---------------------------------------------------------------------------


async def _order_product_agent(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    entity_result: dict[str, Any],
    tools: set[str],
) -> dict[str, Any]:
    """Collect order items, seller details, and product context."""
    case_id = case["case_id"]
    _emit(
        trace,
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="order-agent",
    )

    evidence_refs: list[str] = []
    item_ids: list[str] = []
    seller_ids: list[str] = []
    seen_sellers: set[str] = set()

    items_tool = _find_tool(tools, "get_order_items")
    sellers_tool = _find_tool(tools, "get_sellers")
    product_tool = _find_tool(tools, "get_product_context")
    want_product = case.get("investigation_scope", {}).get("include_product_context", False)

    for order_id in entity_result.get("resolved_order_ids", []):
        # Fetch items for this order
        ev_items = await _safe_call(gateway, items_tool, case_id=case_id, order_id=order_id)
        if ev_items is not None:
            ref = ev_items["evidence_ref"]
            evidence_refs.append(ref)
            _emit(
                trace,
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="order-agent",
                tool_name=items_tool,
                evidence_refs=[ref],
            )
            items_data = ev_items.get("data")
            raw_items = items_data if isinstance(items_data, list) else _get_list(items_data or {}, "items")
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                iid = _get_str(item, "order_item_id", "item_id", "id")
                if iid and iid not in item_ids:
                    item_ids.append(iid)
                sid = _get_str(item, "seller_id")
                if sid and sid not in seen_sellers:
                    seller_ids.append(sid)
                    seen_sellers.add(sid)

        # Fetch sellers if tool available
        if sellers_tool:
            ev_sellers = await _safe_call(gateway, sellers_tool, case_id=case_id, order_id=order_id)
            if ev_sellers is not None:
                ref = ev_sellers["evidence_ref"]
                evidence_refs.append(ref)
                _emit(
                    trace,
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor="order-agent",
                    tool_name=sellers_tool,
                    evidence_refs=[ref],
                )
                sellers_data = ev_sellers.get("data")
                raw_sellers = sellers_data if isinstance(sellers_data, list) else _get_list(sellers_data or {}, "sellers")
                for s in raw_sellers:
                    if isinstance(s, dict):
                        sid = _get_str(s, "seller_id")
                        if sid and sid not in seen_sellers:
                            seller_ids.append(sid)
                            seen_sellers.add(sid)

        # Optional product context
        if want_product and product_tool:
            ev_prod = await _safe_call(gateway, product_tool, case_id=case_id, order_id=order_id)
            if ev_prod is not None:
                ref = ev_prod["evidence_ref"]
                evidence_refs.append(ref)
                _emit(
                    trace,
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor="order-agent",
                    tool_name=product_tool,
                    evidence_refs=[ref],
                )

    _emit(
        trace,
        case_id=case_id,
        event_type="handoff",
        actor="order-agent",
        target="coordinator",
        attributes={"items_found": len(item_ids), "sellers_found": len(seller_ids)},
    )

    return {
        "item_ids": item_ids,
        "seller_ids": seller_ids,
        "evidence_refs": evidence_refs,
    }


# ---------------------------------------------------------------------------
# 3. Shipment Agent
# ---------------------------------------------------------------------------


async def _shipment_agent(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    entity_result: dict[str, Any],
    tools: set[str],
) -> dict[str, Any]:
    """Analyse shipment timeline and SLA for each resolved order."""
    case_id = case["case_id"]
    _emit(
        trace,
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="shipment-agent",
    )

    evidence_refs: list[str] = []
    shipment_ids: list[str] = []
    verdict = "insufficient_evidence"
    timeline_complete = False
    late_seller_ids: list[str] = []

    ship_tool = _find_tool(tools, "get_shipment_summary")

    for order_id in entity_result.get("resolved_order_ids", []):
        ev = await _safe_call(gateway, ship_tool, case_id=case_id, order_id=order_id)
        if ev is None:
            continue
        ref = ev["evidence_ref"]
        evidence_refs.append(ref)
        _emit(
            trace,
            case_id=case_id,
            event_type="tool_result_consumed",
            actor="shipment-agent",
            tool_name=ship_tool,
            evidence_refs=[ref],
        )

        ship_data = ev.get("data") or {}
        if not isinstance(ship_data, dict):
            continue

        sid = _get_str(ship_data, "shipment_id", "order_id", "tracking_id")
        if sid and sid not in shipment_ids:
            shipment_ids.append(sid)

        # Dates from get_shipment_summary
        estimated = _get_str(ship_data, "estimated_delivery_at", "order_estimated_delivery_date")
        actual = _get_str(ship_data, "delivered_customer_at", "order_delivered_customer_date")
        shipped = _get_str(ship_data, "delivered_carrier_at", "order_delivered_carrier_date")
        status = _get_str(ship_data, "order_status", "status") or ""

        # Shipping limits per seller
        shipping_limits = ship_data.get("shipping_limits") or []

        # Events
        events = ship_data.get("events") or []

        if actual and estimated:
            timeline_complete = True
            if actual > estimated:
                # Check if seller dispatched late beyond shipping limit
                seller_delayed = False
                for limit_entry in shipping_limits:
                    if isinstance(limit_entry, dict):
                        limit_at = _get_str(limit_entry, "shipping_limit_at", "shipping_limit_date")
                        seller = _get_str(limit_entry, "seller_id")
                        if limit_at and shipped and shipped > limit_at:
                            seller_delayed = True
                            if seller and seller not in late_seller_ids:
                                late_seller_ids.append(seller)

                # Also inspect events for seller vs logistics fault
                for ev_item in events:
                    if isinstance(ev_item, dict):
                        actor_type = _get_str(ev_item, "actor")
                        ev_type = _get_str(ev_item, "event_type")
                        if "late" in str(ev_type).lower():
                            if actor_type == "seller":
                                seller_delayed = True

                if seller_delayed:
                    verdict = "seller_delay"
                else:
                    verdict = "logistics_delay"
            else:
                verdict = "on_time"
        elif status.lower() in ("lost", "returned"):
            verdict = status.lower()
            timeline_complete = False
        elif actual or shipped:
            verdict = "on_time"
            timeline_complete = False

    _emit(
        trace,
        case_id=case_id,
        event_type="handoff",
        actor="shipment-agent",
        target="coordinator",
        attributes={"verdict": verdict},
    )

    return {
        "verdict": verdict,
        "late_seller_ids": late_seller_ids,
        "timeline_complete": timeline_complete,
        "shipment_ids": shipment_ids,
        "evidence_refs": evidence_refs,
    }


# ---------------------------------------------------------------------------
# 4. Payment / Refund Agent
# ---------------------------------------------------------------------------


async def _payment_agent(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    entity_result: dict[str, Any],
    tools: set[str],
) -> dict[str, Any]:
    """Analyse payments and refund timelines for each resolved order."""
    case_id = case["case_id"]
    _emit(
        trace,
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="payment-agent",
    )

    evidence_refs: list[str] = []
    payment_refs: list[str] = []
    captured_total: float = 0.0
    refunded_total: float = 0.0

    pay_tool = _find_tool(tools, "get_order_payments", "get_payment_timeline")
    refund_tool = _find_tool(tools, "get_refund_timeline")

    for order_id in entity_result.get("resolved_order_ids", []):
        # 1. Fetch payments
        ev = await _safe_call(gateway, pay_tool, case_id=case_id, order_id=order_id)
        if ev is not None:
            ref = ev["evidence_ref"]
            evidence_refs.append(ref)
            _emit(
                trace,
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="payment-agent",
                tool_name=pay_tool,
                evidence_refs=[ref],
            )
            pay_data = ev.get("data")
            if isinstance(pay_data, list):
                payments = pay_data
            elif isinstance(pay_data, dict):
                payments = _get_list(pay_data, "payments", "payment_list", "items")
                if not payments:
                    payments = [pay_data]
            else:
                payments = []

            for p in payments:
                if not isinstance(p, dict):
                    continue
                val = _get_float(p, "payment_value", "amount", "value", "total")
                captured_total += val
                pid = _get_str(
                    p, "payment_sequential", "payment_id", "payment_type", "reference"
                )
                if pid and pid not in payment_refs:
                    payment_refs.append(pid)

        # 2. Fetch refund timeline (returns None quietly if no refunds exist)
        if refund_tool:
            ev_ref = await _safe_call(gateway, refund_tool, case_id=case_id, order_id=order_id)
            if ev_ref is not None:
                ref = ev_ref["evidence_ref"]
                evidence_refs.append(ref)
                _emit(
                    trace,
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor="payment-agent",
                    tool_name=refund_tool,
                    evidence_refs=[ref],
                )
                ref_data = ev_ref.get("data")
                if isinstance(ref_data, list):
                    refunds = ref_data
                elif isinstance(ref_data, dict):
                    refunds = _get_list(ref_data, "refunds", "events", "refund_list")
                    if not refunds:
                        refunds = [ref_data]
                else:
                    refunds = []

                for r in refunds:
                    if not isinstance(r, dict):
                        continue
                    rval = _get_float(
                        r, "refund_amount", "amount", "value", "refund_value", "amount_brl"
                    )
                    refunded_total += rval

    captured_total = round(captured_total, 2)
    refunded_total = round(refunded_total, 2)
    refundable_total = round(max(captured_total - refunded_total, 0.0), 2)

    if captured_total == 0.0 and refunded_total == 0.0:
        verdict = "insufficient_evidence"
    elif refunded_total > 0 and refundable_total <= 0.0:
        verdict = "refunded"
    elif refunded_total > 0:
        verdict = "refund_pending"
    else:
        verdict = "reconciled"

    _emit(
        trace,
        case_id=case_id,
        event_type="handoff",
        actor="payment-agent",
        target="coordinator",
        attributes={
            "verdict": verdict,
            "captured": captured_total,
            "refunded": refunded_total,
        },
    )

    return {
        "verdict": verdict,
        "captured_total_brl": captured_total,
        "refunded_total_brl": refunded_total,
        "refundable_total_brl": refundable_total,
        "payment_references": payment_refs,
        "evidence_refs": evidence_refs,
    }


# ---------------------------------------------------------------------------
# 5. Policy Agent
# ---------------------------------------------------------------------------


def _determine_primary_issue(
    case: dict[str, Any],
    shipment_result: dict[str, Any],
    payment_result: dict[str, Any],
    entity_result: dict[str, Any],
) -> str:
    """Determine the primary issue code from evidence and claims."""
    claims = case.get("customer_request", {}).get("claims", [])
    claim_topics = [c.get("topic", "") for c in claims if isinstance(c, dict)]

    ship_verdict = shipment_result.get("verdict", "insufficient_evidence")
    pay_verdict = payment_result.get("verdict", "insufficient_evidence")

    # Order status cancellations
    for od in entity_result.get("order_data_map", {}).values():
        status = str(od.get("order_status", "") or od.get("status", "")).lower()
        if status == "canceled":
            if payment_result.get("captured_total_brl", 0) > 0:
                return "canceled_order_paid"
        if status == "unavailable":
            if payment_result.get("captured_total_brl", 0) > 0:
                return "unavailable_order_paid"

    # Shipment verdict
    if ship_verdict == "seller_delay":
        return "late_delivery_seller"
    if ship_verdict == "logistics_delay":
        return "late_delivery_logistics"

    # Payment verdict
    if pay_verdict == "capture_mismatch":
        return "payment_mismatch"
    if pay_verdict == "duplicate_capture":
        return "duplicate_charge"
    if pay_verdict == "refund_pending":
        return "refund_pending"
    if pay_verdict == "refund_failed":
        return "refund_failed"

    # Fall back to matched claim topic
    for topic in claim_topics:
        mapped = _TOPIC_TO_ISSUE.get(topic)
        if mapped:
            return mapped

    return "insufficient_evidence"


def _determine_responsible_parties(
    primary_issue: str,
    entity_result: dict[str, Any],
    shipment_result: dict[str, Any],
) -> list[dict[str, str | None]]:
    """Assign responsible parties based on primary issue."""
    parties: list[dict[str, str | None]] = []

    seller_issues = {"late_delivery_seller", "canceled_order_paid", "unavailable_order_paid"}
    logistics_issues = {"late_delivery_logistics"}
    platform_issues = {
        "payment_mismatch",
        "duplicate_charge",
        "refund_pending",
        "refund_failed",
        "valid_split_payment",
    }

    if primary_issue in seller_issues:
        seller_ids = list(shipment_result.get("late_seller_ids", []))
        for sid in seller_ids[:3]:
            parties.append({"party_type": "seller", "party_id": sid})
        if not parties:
            parties.append({"party_type": "seller", "party_id": None})
    elif primary_issue in logistics_issues:
        parties.append({"party_type": "logistics_provider", "party_id": None})
    elif primary_issue in platform_issues:
        parties.append({"party_type": "platform", "party_id": None})
    else:
        parties.append({"party_type": "unknown", "party_id": None})

    return parties[:5]


def _build_resolution_actions(
    primary_issue: str,
    payment_result: dict[str, Any],
) -> list[str]:
    """Generate distinct human-readable resolution actions."""
    action_map: dict[str, list[str]] = {
        "canceled_order_paid": [
            "Process full refund for canceled order",
            "Notify seller of cancellation",
        ],
        "unavailable_order_paid": [
            "Process refund for unavailable item",
            "Remove listing from catalog",
        ],
        "late_delivery_seller": [
            "Issue partial refund for late delivery",
            "Warn seller about shipping SLA",
        ],
        "late_delivery_logistics": [
            "Issue compensation for logistics delay",
            "File claim with logistics provider",
        ],
        "payment_mismatch": [
            "Reconcile payment discrepancy",
            "Adjust captured amount",
        ],
        "duplicate_charge": [
            "Refund duplicate payment",
            "Investigate payment gateway issue",
        ],
        "refund_pending": ["Expedite pending refund processing"],
        "refund_failed": [
            "Retry failed refund",
            "Escalate to payment operations",
        ],
        "valid_split_payment": ["No action required for valid split payment"],
    }

    actions = list(action_map.get(primary_issue, ["Investigate further"]))

    refundable = payment_result.get("refundable_total_brl", 0.0)
    if refundable > 0 and not any("refund" in a.lower() for a in actions):
        actions.append(f"Process refund of {refundable:.2f} BRL")

    return _unique(actions, limit=8)


def _build_refund_lines(
    primary_issue: str,
    payment_result: dict[str, Any],
    entity_result: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build refund lines based on the analysis."""
    refundable = payment_result.get("refundable_total_brl", 0.0)
    if refundable <= 0.0:
        return []

    entity_id: str | None = None
    resolved = entity_result.get("resolved_order_ids", [])
    if resolved:
        entity_id = resolved[0]

    reason_map: dict[str, str] = {
        "canceled_order_paid": "CANCELED_ORDER_REFUND",
        "unavailable_order_paid": "UNAVAILABLE_ITEM_REFUND",
        "late_delivery_seller": "LATE_DELIVERY_COMPENSATION",
        "late_delivery_logistics": "LOGISTICS_DELAY_COMPENSATION",
        "payment_mismatch": "PAYMENT_CORRECTION",
        "duplicate_charge": "DUPLICATE_CHARGE_REFUND",
        "refund_pending": "PENDING_REFUND_PROCESSING",
        "refund_failed": "FAILED_REFUND_RETRY",
    }

    reason = reason_map.get(primary_issue, "GENERAL_REFUND")
    return [{"reason_code": reason, "amount_brl": refundable, "entity_id": entity_id}]


# ---------------------------------------------------------------------------
# 6. Conflict Resolver
# ---------------------------------------------------------------------------


def _resolve_conflicts(
    shipment_result: dict[str, Any],
    payment_result: dict[str, Any],
    primary_issue: str,
) -> list[dict[str, Any]]:
    """Detect and record data conflicts between sources."""
    conflicts: list[dict[str, Any]] = []

    ship_verdict = shipment_result.get("verdict", "insufficient_evidence")
    pay_verdict = payment_result.get("verdict", "insufficient_evidence")

    if ship_verdict == "on_time" and primary_issue in (
        "late_delivery_seller",
        "late_delivery_logistics",
    ):
        conflicts.append(
            {
                "field": "delivery_timeliness",
                "sources": ["shipment_tracking", "customer_claim"],
                "selected_source": "shipment_tracking",
                "resolution_code": "AUTHORITATIVE_SOURCE_PRIORITY",
            }
        )

    if pay_verdict == "reconciled" and primary_issue in (
        "payment_mismatch",
        "duplicate_charge",
    ):
        conflicts.append(
            {
                "field": "payment_status",
                "sources": ["payment_records", "customer_claim"],
                "selected_source": "payment_records",
                "resolution_code": "AUTHORITATIVE_SOURCE_PRIORITY",
            }
        )

    if ship_verdict == "conflicting":
        conflicts.append(
            {
                "field": "shipment_data",
                "sources": ["carrier_tracking", "seller_records"],
                "selected_source": "carrier_tracking",
                "resolution_code": "CARRIER_DATA_PRIORITY",
            }
        )

    return conflicts[:5]


# ---------------------------------------------------------------------------
# 7. Verifier & Calibrator
# ---------------------------------------------------------------------------


def _verify_and_calibrate(
    primary_issue: str,
    entity_result: dict[str, Any],
    shipment_result: dict[str, Any],
    payment_result: dict[str, Any],
    conflicts: list[dict[str, Any]],
    all_evidence_refs: list[str],
) -> float:
    """Compute calibrated confidence considering evidence quality."""
    base_confidence = entity_result.get("confidence", 0.5)
    adjustments: float = 0.0

    if len(all_evidence_refs) >= 4:
        adjustments += 0.05
    elif len(all_evidence_refs) <= 1:
        adjustments -= 0.10

    if shipment_result.get("timeline_complete"):
        adjustments += 0.05
    else:
        adjustments -= 0.05

    if payment_result.get("captured_total_brl", 0) > 0:
        adjustments += 0.05
    if payment_result.get("verdict") == "insufficient_evidence":
        adjustments -= 0.10

    adjustments -= len(conflicts) * 0.05

    if primary_issue == "insufficient_evidence":
        return round(min(max(0.30, base_confidence + adjustments), 0.50), 2)

    confidence = base_confidence + adjustments
    return round(min(max(confidence, 0.10), 0.95), 2)


# ---------------------------------------------------------------------------
# 8. Claim Assessment Builder
# ---------------------------------------------------------------------------


def _build_claim_assessments(
    case: dict[str, Any],
    primary_issue: str,
    evidence_refs: list[str],
) -> list[dict[str, Any]]:
    """Assess each claim in the case input."""
    claims = case.get("customer_request", {}).get("claims", [])
    assessments: list[dict[str, Any]] = []

    for claim in claims[:5]:
        if not isinstance(claim, dict):
            continue
        claim_id = str(claim.get("claim_id") or "claim-unknown")[:64]
        topic = str(claim.get("topic") or "")

        mapped_issue = _TOPIC_TO_ISSUE.get(topic)
        if mapped_issue == primary_issue:
            verdict = "supported"
            conf = 0.85
        elif mapped_issue:
            verdict = "partially_supported"
            conf = 0.50
        elif topic == "requested_full_refund":
            verdict = "partially_supported"
            conf = 0.60
        else:
            verdict = "insufficient_evidence"
            conf = 0.30

        assessments.append(
            {
                "claim_id": claim_id,
                "verdict": verdict,
                "confidence": conf,
                "evidence_refs": evidence_refs[:10],
            }
        )

    return assessments


# ---------------------------------------------------------------------------
# Main entry-point: Coordinator
# ---------------------------------------------------------------------------


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Execute the L3B coordinator and specialist-agent workflow."""
    case_id = case["case_id"]

    # Discover available MCP tools
    tools = await _get_tools(gateway)

    # 1. Entity Resolution
    entity_result = await _entity_resolution_agent(case, gateway, trace, tools)

    # 2. Specialist investigation (parallel via asyncio.gather)
    order_result, shipment_result, payment_result = await asyncio.gather(
        _order_product_agent(case, gateway, trace, entity_result, tools),
        _shipment_agent(case, gateway, trace, entity_result, tools),
        _payment_agent(case, gateway, trace, entity_result, tools),
    )

    # 3. Policy decision
    primary_issue = _determine_primary_issue(
        case, shipment_result, payment_result, entity_result
    )
    responsible_parties = _determine_responsible_parties(
        primary_issue, entity_result, shipment_result
    )
    resolution_actions = _build_resolution_actions(primary_issue, payment_result)
    refund_lines = _build_refund_lines(primary_issue, payment_result, entity_result)
    refundable = payment_result.get("refundable_total_brl", 0.0)

    _emit(
        trace,
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        decision_code=primary_issue,
        attributes={"recommended_refund_brl": refundable},
    )

    # 4. Conflict resolution
    conflicts = _resolve_conflicts(shipment_result, payment_result, primary_issue)

    # 5. Verification & calibration
    all_evidence_refs: list[str] = []
    seen: set[str] = set()
    for ref_list in [
        entity_result.get("evidence_refs", []),
        order_result.get("evidence_refs", []),
        shipment_result.get("evidence_refs", []),
        payment_result.get("evidence_refs", []),
    ]:
        for ref in ref_list:
            if ref not in seen:
                all_evidence_refs.append(ref)
                seen.add(ref)

    confidence = _verify_and_calibrate(
        primary_issue,
        entity_result,
        shipment_result,
        payment_result,
        conflicts,
        all_evidence_refs,
    )

    # Determine case_status
    no_action_issues = {"valid_split_payment", "unsupported_claim"}
    if primary_issue in no_action_issues:
        case_status = "no_action"
    elif primary_issue == "insufficient_evidence":
        case_status = "needs_investigation"
    else:
        case_status = "action_required"

    # Secondary issues
    secondary_issues: list[str] = []
    claims = case.get("customer_request", {}).get("claims", [])
    for c in claims:
        if not isinstance(c, dict):
            continue
        topic = c.get("topic", "")
        mapped = _TOPIC_TO_ISSUE.get(topic)
        if mapped and mapped != primary_issue and mapped not in secondary_issues:
            secondary_issues.append(mapped)

    # Claim assessments
    claim_assessments = _build_claim_assessments(case, primary_issue, all_evidence_refs)

    # Root cause code
    cause_code = _ISSUE_TO_CAUSE.get(primary_issue, "INSUFFICIENT_EVIDENCE_AVAILABLE")

    _emit(
        trace,
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier-agent",
        decision_code="PASS" if confidence >= 0.5 else "LOW_CONFIDENCE",
        attributes={"confidence": confidence, "conflicts": len(conflicts)},
    )

    # Assemble final output adhering strictly to day09-l3b-output-v2 schema
    output: dict[str, Any] = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": primary_issue,
            "secondary_issues": secondary_issues[:10],
            "case_status": case_status,
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": _unique(entity_result["resolved_order_ids"]),
            "item_ids": _unique(order_result.get("item_ids", [])),
            "seller_ids": _unique(order_result.get("seller_ids", [])),
            "payment_references": _unique(payment_result.get("payment_references", [])),
            "shipment_ids": _unique(shipment_result.get("shipment_ids", [])),
        },
        "claim_assessments": claim_assessments[:5],
        "entity_resolution": {
            "status": entity_result["status"],
            "resolved_order_ids": _unique(entity_result["resolved_order_ids"]),
            "rejected_candidates": _unique(entity_result["rejected_candidates"]),
            "confidence": entity_result["confidence"],
        },
        "customer_context": {
            "customer_unique_id": entity_result.get("customer_unique_id"),
            "related_order_ids": _unique(entity_result.get("customer_related_orders", [])),
        },
        "shipment_analysis": {
            "verdict": shipment_result["verdict"],
            "late_seller_ids": _unique(shipment_result["late_seller_ids"]),
            "timeline_complete": shipment_result["timeline_complete"],
        },
        "payment_analysis": {
            "verdict": payment_result["verdict"],
            "captured_total_brl": payment_result["captured_total_brl"],
            "refunded_total_brl": payment_result["refunded_total_brl"],
            "refundable_total_brl": payment_result["refundable_total_brl"],
        },
        "root_cause_analysis": {
            "ranked_causes": [
                {"cause_code": cause_code, "rank": 1},
            ],
            "responsible_parties": responsible_parties[:5],
        },
        "evidence_refs": _unique(all_evidence_refs, limit=30),
        "data_conflicts": conflicts[:5],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refundable,
            "refund_lines": refund_lines[:10],
        },
        "resolution_actions": resolution_actions[:8],
    }

    return output
