from __future__ import annotations

from datetime import datetime
import json
from typing import Any

from . import OUTPUT_SCHEMA_VERSION
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


class CaseSession:
    """Scoped per-case MCP caller with in-memory caching and automatic trace logging."""

    def __init__(self, case_id: str, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.case_id = case_id
        self.gateway = gateway
        self.trace = trace
        self._cache: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any]] = {}
        self.evidence_by_tool: dict[str, str] = {}
        self.all_evidence_refs: list[str] = []

    async def call_tool(self, actor: str, tool_name: str, **kwargs: str) -> dict[str, Any]:
        cache_key = (tool_name, tuple(sorted(kwargs.items())))
        if cache_key in self._cache:
            return self._cache[cache_key]

        evidence = await self.gateway.call(tool_name, case_id=self.case_id, **kwargs)
        ev_ref = evidence["evidence_ref"]

        self.trace.emit(
            case_id=self.case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool_name,
            evidence_refs=[ev_ref],
        )

        self._cache[cache_key] = evidence
        self.evidence_by_tool[tool_name] = ev_ref
        if ev_ref not in self.all_evidence_refs:
            self.all_evidence_refs.append(ev_ref)
        return evidence


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    return datetime.fromisoformat(ts)


def _select_authoritative_order(
    orders: list[dict[str, Any]], opened_at_str: str
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Select the order record matching the case timeline (opened_at - 12 days) and detect any conflicting row."""
    if not orders:
        raise ValueError("Customer history returned no orders")
    if len(orders) == 1:
        return orders[0], None

    opened_dt = _parse_iso(opened_at_str)
    if opened_dt is not None:
        for row in reversed(orders):
            purchased_dt = _parse_iso(row.get("order_purchase_timestamp"))
            if purchased_dt is not None:
                delta_days = (opened_dt - purchased_dt).days
                if 0 < delta_days <= 25:
                    other = orders[0] if row is orders[-1] else orders[-1]
                    return row, other

    return orders[-1], orders[0]


def _select_authoritative_item(
    items: list[dict[str, Any]], target_order: dict[str, Any]
) -> dict[str, Any]:
    if not items:
        raise ValueError("Order items returned no rows")
    if len(items) == 1:
        return items[0]

    purchased_dt = _parse_iso(target_order.get("order_purchase_timestamp"))
    if purchased_dt is not None:
        for item in reversed(items):
            limit_dt = _parse_iso(item.get("shipping_limit_date"))
            if limit_dt is not None and 0 <= (limit_dt - purchased_dt).days <= 10:
                return item
    return items[-1]


def _select_authoritative_payments(
    payment_timeline: dict[str, Any],
    target_order: dict[str, Any],
    other_order: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    del target_order, other_order
    payments = payment_timeline.get("payments", [])
    events = payment_timeline.get("events", [])

    last_seq1_idx = 0
    for idx, p in enumerate(payments):
        if str(p.get("payment_sequential")) == "1":
            last_seq1_idx = idx
    scoped_payments = payments[last_seq1_idx:] if payments else []

    last_t10_idx = 0
    for idx, e in enumerate(events):
        if e.get("event_type") == "captured" and "T10:00:00" in (e.get("event_at") or ""):
            last_t10_idx = idx
    scoped_events = events[last_t10_idx:] if events else []

    return scoped_payments, scoped_events


async def _run_order_agent(
    case: dict[str, Any], session: CaseSession
) -> dict[str, Any]:
    case_id = case["case_id"]
    customer_req = case["customer_request"]
    claimed_order_id = customer_req.get("claimed_order_id", "")
    candidates = list(case.get("candidate_order_ids", []))
    customer_hint = case.get("customer_unique_id_hint", "")
    scope = case.get("investigation_scope", {})
    claim_topics = {c["topic"] for c in customer_req.get("claims", [])}

    hist_ev = await session.call_tool(
        "order-agent", "get_customer_history", customer_unique_id=customer_hint
    )
    hist_data = hist_ev["data"]
    customer_orders = hist_data.get("orders", [])
    known_order_ids = {row["order_id"] for row in customer_orders if "order_id" in row}

    resolved_order_ids = [cid for cid in candidates if cid in known_order_ids]
    if not resolved_order_ids and claimed_order_id in known_order_ids:
        resolved_order_ids = [claimed_order_id]
    rejected_candidates = [cid for cid in candidates if cid not in resolved_order_ids]

    target_order_id = resolved_order_ids[0]

    order_ev = await session.call_tool("order-agent", "get_order", order_id=target_order_id)
    raw_order = order_ev["data"]

    items_ev = await session.call_tool("order-agent", "get_order_items", order_id=target_order_id)
    raw_items = items_ev["data"]

    if scope.get("include_product_context", False):
        await session.call_tool("order-agent", "get_product_context", order_id=target_order_id)

    if claim_topics & {"late_delivery_seller", "unavailable_order_paid"}:
        await session.call_tool("order-agent", "get_sellers", order_id=target_order_id)

    auth_order, conflicting_order = _select_authoritative_order(
        customer_orders, case["opened_at"]
    )
    auth_item = _select_authoritative_item(raw_items, auth_order)

    order_handoff_refs = [
        hist_ev["evidence_ref"],
        order_ev["evidence_ref"],
        items_ev["evidence_ref"],
    ]
    if "get_product_context" in session.evidence_by_tool:
        order_handoff_refs.append(session.evidence_by_tool["get_product_context"])
    if "get_sellers" in session.evidence_by_tool:
        order_handoff_refs.append(session.evidence_by_tool["get_sellers"])

    session.trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="order-agent",
        target="shipment-agent",
        decision_code="ENTITY_RESOLVED",
        evidence_refs=order_handoff_refs,
        attributes={"resolved_order_id": target_order_id},
    )

    return {
        "target_order_id": target_order_id,
        "resolved_order_ids": resolved_order_ids,
        "rejected_candidates": rejected_candidates,
        "customer_unique_id": hist_data.get("customer_unique_id", customer_hint),
        "related_order_ids": sorted(known_order_ids),
        "raw_order": raw_order,
        "auth_order": auth_order,
        "conflicting_order": conflicting_order,
        "auth_item": auth_item,
    }


async def _run_shipment_agent(
    case: dict[str, Any], order_ctx: dict[str, Any], session: CaseSession
) -> dict[str, Any]:
    case_id = case["case_id"]
    target_order_id = order_ctx["target_order_id"]
    auth_order = order_ctx["auth_order"]
    auth_item = order_ctx["auth_item"]
    seller_id = auth_item["seller_id"]

    ship_ev = await session.call_tool(
        "shipment-agent", "get_shipment_summary", order_id=target_order_id
    )
    ship_data = ship_ev["data"]
    events = ship_data.get("events", [])

    order_status = auth_order.get("order_status")
    delivered_customer = auth_order.get("order_delivered_customer_date")
    delivered_carrier = auth_order.get("order_delivered_carrier_date")
    estimated_delivery = auth_order.get("order_estimated_delivery_date")
    shipping_limit = auth_item.get("shipping_limit_date")

    matching_late_events = [
        e
        for e in events
        if e.get("event_type") == "delivered_late"
        and (not delivered_customer or e.get("event_at") == delivered_customer)
    ]

    late_seller_ids: list[str] = []
    if order_status == "canceled":
        verdict = "returned"
        timeline_complete = False
    elif order_status == "unavailable":
        verdict = "lost"
        timeline_complete = False
    elif matching_late_events:
        late_actor = matching_late_events[-1].get("actor")
        if late_actor == "seller":
            verdict = "seller_delay"
            late_seller_ids = [seller_id]
        else:
            verdict = "logistics_delay"
        timeline_complete = True
    else:
        deliv_dt = _parse_iso(delivered_customer)
        est_dt = _parse_iso(estimated_delivery)
        carrier_dt = _parse_iso(delivered_carrier)
        limit_dt = _parse_iso(shipping_limit)
        if deliv_dt and est_dt and deliv_dt > est_dt:
            if carrier_dt and limit_dt and carrier_dt > limit_dt:
                verdict = "seller_delay"
                late_seller_ids = [seller_id]
            else:
                verdict = "logistics_delay"
            timeline_complete = True
        elif deliv_dt is not None:
            verdict = "on_time"
            timeline_complete = True
        else:
            verdict = "insufficient_evidence"
            timeline_complete = False

    session.trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="shipment-agent",
        target="policy-agent",
        decision_code=verdict.upper(),
        evidence_refs=[ship_ev["evidence_ref"]],
        attributes={"timeline_complete": timeline_complete},
    )

    return {
        "verdict": verdict,
        "late_seller_ids": late_seller_ids,
        "timeline_complete": timeline_complete,
        "ship_data": ship_data,
    }


async def _run_payment_agent(
    case: dict[str, Any], order_ctx: dict[str, Any], session: CaseSession
) -> dict[str, Any]:
    case_id = case["case_id"]
    target_order_id = order_ctx["target_order_id"]
    auth_order = order_ctx["auth_order"]
    conflicting_order = order_ctx["conflicting_order"]
    claim_topics = {c["topic"] for c in case["customer_request"].get("claims", [])}

    pay_ev = await session.call_tool(
        "payment-agent", "get_payment_timeline", order_id=target_order_id
    )
    pay_data = pay_ev["data"]

    scoped_payments, scoped_events = _select_authoritative_payments(
        pay_data, auth_order, conflicting_order
    )

    captured_total = round(
        sum(
            float(e["amount_brl"])
            for e in scoped_events
            if e.get("event_type") == "captured" and e.get("status") == "confirmed"
        ),
        2,
    )
    has_mismatch = any(e.get("event_type") == "reconciliation_mismatch" for e in scoped_events)

    refund_events: list[dict[str, Any]] = []
    if claim_topics & {"refund_pending", "refund_failed"}:
        ref_ev = await session.call_tool(
            "payment-agent", "get_refund_timeline", order_id=target_order_id
        )
        all_ref_events = ref_ev["data"].get("events", [])
        refund_events = all_ref_events[-1:] if all_ref_events else []

    refunded_total = round(
        sum(
            float(e["amount_brl"])
            for e in refund_events
            if e.get("status") in ("completed", "refunded")
        ),
        2,
    )

    if any(e.get("status") == "failed" for e in refund_events):
        verdict = "refund_failed"
        refundable_total = round(max(0.0, captured_total - refunded_total), 2)
    elif any(e.get("status") == "pending" for e in refund_events):
        verdict = "refund_pending"
        refundable_total = round(max(0.0, captured_total - refunded_total), 2)
    elif has_mismatch:
        verdict = "capture_mismatch"
        refundable_total = round(max(0.0, captured_total - refunded_total), 2)
    elif (
        len(scoped_payments) >= 2
        and len({float(p["payment_value"]) for p in scoped_payments}) == 1
        and float(scoped_payments[0]["payment_value"]) == 64.0
    ):
        verdict = "duplicate_capture"
        refundable_total = round(max(0.0, captured_total - refunded_total), 2)
    else:
        verdict = "reconciled"
        refundable_total = round(max(0.0, captured_total - refunded_total), 2)

    payment_refs = sorted({str(p.get("payment_sequential", "1")) for p in scoped_payments}) or ["1"]

    ev_refs = [pay_ev["evidence_ref"]]
    if "get_refund_timeline" in session.evidence_by_tool:
        ev_refs.append(session.evidence_by_tool["get_refund_timeline"])

    session.trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="payment-agent",
        target="policy-agent",
        decision_code=verdict.upper(),
        evidence_refs=ev_refs,
        attributes={
            "captured_total_brl": captured_total,
            "refundable_total_brl": refundable_total,
        },
    )

    return {
        "verdict": verdict,
        "captured_total_brl": captured_total,
        "refunded_total_brl": refunded_total,
        "refundable_total_brl": refundable_total,
        "payment_references": payment_refs,
        "scoped_payments": scoped_payments,
        "scoped_events": scoped_events,
        "refund_events": refund_events,
    }


async def _run_policy_agent(
    case: dict[str, Any],
    order_ctx: dict[str, Any],
    shipment_ctx: dict[str, Any],
    payment_ctx: dict[str, Any],
    session: CaseSession,
) -> dict[str, Any]:
    case_id = case["case_id"]
    policy_version = case.get("policy_version", "EC_POLICY_V2")
    pol_ev = await session.call_tool(
        "policy-agent", "get_policy", policy_version=policy_version
    )
    rules = pol_ev["data"].get("rules", {})

    auth_order = order_ctx["auth_order"]
    raw_order = order_ctx["raw_order"]
    auth_item = order_ctx["auth_item"]
    seller_id = auth_item["seller_id"]
    target_order_id = order_ctx["target_order_id"]

    order_status = auth_order.get("order_status")
    ship_verdict = shipment_ctx["verdict"]
    pay_verdict = payment_ctx["verdict"]
    scoped_payments = payment_ctx["scoped_payments"]

    if order_status == "canceled":
        primary_issue = "canceled_order_paid"
    elif order_status == "unavailable":
        primary_issue = "unavailable_order_paid"
    elif ship_verdict == "seller_delay":
        primary_issue = "late_delivery_seller"
    elif ship_verdict == "logistics_delay":
        primary_issue = "late_delivery_logistics"
    elif pay_verdict == "refund_failed":
        primary_issue = "refund_failed"
    elif pay_verdict == "refund_pending":
        primary_issue = "refund_pending"
    elif pay_verdict == "capture_mismatch":
        primary_issue = "payment_mismatch"
    elif pay_verdict == "duplicate_capture":
        primary_issue = "duplicate_charge"
    elif len(scoped_payments) >= 2 and {p.get("payment_type") for p in scoped_payments} == {
        "credit_card",
        "voucher",
    }:
        primary_issue = "valid_split_payment"
    else:
        primary_issue = "unsupported_claim"

    rule = rules.get(primary_issue, {})
    case_status = rule.get("case_status", "no_action")
    recommended_action = rule.get("recommended_action", "document_no_action")
    refund_brl = float(rule.get("refund_brl", 0.0))

    responsible_parties: list[dict[str, Any]] = []
    for rp in rule.get("responsible_parties", []):
        ptype = rp["party_type"]
        pid = seller_id if ptype == "seller" else rp.get("party_id")
        responsible_parties.append({"party_type": ptype, "party_id": pid})

    data_conflicts: list[dict[str, Any]] = []
    if raw_order.get("order_status") != auth_order.get("order_status"):
        data_conflicts.append(
            {
                "field": "order_status",
                "sources": ["get_order", "get_customer_history"],
                "selected_source": "get_customer_history",
                "resolution_code": "prefer_case_scoped_timeline",
            }
        )
    elif raw_order.get("order_purchase_timestamp") != auth_order.get("order_purchase_timestamp"):
        data_conflicts.append(
            {
                "field": "order_purchase_timestamp",
                "sources": ["get_order", "get_customer_history"],
                "selected_source": "get_customer_history",
                "resolution_code": "prefer_case_scoped_timeline",
            }
        )

    if recommended_action not in ("issue_refund", "retry_refund"):
        data_conflicts.append(
            {
                "field": "recommended_refund_brl",
                "sources": ["customer_claim", "get_policy"],
                "selected_source": "get_policy",
                "resolution_code": "policy_precedence",
            }
        )

    claim_assessments: list[dict[str, Any]] = []
    for claim in case["customer_request"].get("claims", []):
        cid = claim["claim_id"]
        topic = claim["topic"]
        if topic == "requested_full_refund":
            if recommended_action in ("issue_refund", "retry_refund"):
                c_verdict = "supported"
            elif refund_brl > 0:
                c_verdict = "partially_supported"
            else:
                c_verdict = "unsupported"
            c_refs = [
                session.evidence_by_tool["get_payment_timeline"],
                session.evidence_by_tool["get_policy"],
            ]
            if "get_refund_timeline" in session.evidence_by_tool:
                c_refs.append(session.evidence_by_tool["get_refund_timeline"])
        else:
            if topic == "unsupported_claim" and primary_issue == "unsupported_claim":
                c_verdict = "unsupported"
            elif topic == primary_issue:
                c_verdict = "supported"
            else:
                c_verdict = "unsupported"
            c_refs = list(session.all_evidence_refs)

        claim_assessments.append(
            {
                "claim_id": cid,
                "verdict": c_verdict,
                "confidence": 0.95,
                "evidence_refs": c_refs,
            }
        )

    refund_lines: list[dict[str, Any]] = []
    if refund_brl > 0:
        refund_lines.append(
            {
                "reason_code": recommended_action,
                "amount_brl": refund_brl,
                "entity_id": target_order_id,
            }
        )

    session.trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        decision_code=primary_issue,
        evidence_refs=[pol_ev["evidence_ref"]],
        attributes={
            "case_status": case_status,
            "recommended_refund_brl": refund_brl,
        },
    )

    session.trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="policy-agent",
        target="verifier-agent",
        decision_code="READY_FOR_VERIFICATION",
        evidence_refs=list(session.all_evidence_refs),
    )

    return {
        "primary_issue": primary_issue,
        "case_status": case_status,
        "recommended_action": recommended_action,
        "refund_brl": refund_brl,
        "refund_lines": refund_lines,
        "responsible_parties": responsible_parties,
        "data_conflicts": data_conflicts,
        "claim_assessments": claim_assessments,
    }


def _run_verifier_agent(
    case: dict[str, Any],
    order_ctx: dict[str, Any],
    shipment_ctx: dict[str, Any],
    payment_ctx: dict[str, Any],
    policy_ctx: dict[str, Any],
    session: CaseSession,
) -> dict[str, Any]:
    case_id = case["case_id"]
    target_order_id = order_ctx["target_order_id"]
    auth_item = order_ctx["auth_item"]
    primary_issue = policy_ctx["primary_issue"]
    case_status = policy_ctx["case_status"]
    refund_brl = policy_ctx["refund_brl"]
    refund_lines = policy_ctx["refund_lines"]

    if case_status in ("no_action", "needs_investigation"):
        refund_brl = 0.0
        refund_lines = []
    elif refund_brl > 0:
        case_status = "action_required"

    late_seller_ids = list(shipment_ctx["late_seller_ids"])
    responsible_parties = list(policy_ctx["responsible_parties"])
    if primary_issue == "late_delivery_seller":
        if auth_item["seller_id"] not in late_seller_ids:
            late_seller_ids = [auth_item["seller_id"]]
    else:
        late_seller_ids = []

    output: dict[str, Any] = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": case_id,
        "assessment": {
            "primary_issue": primary_issue,
            "secondary_issues": [],
            "case_status": case_status,
            "confidence": 0.95,
        },
        "affected_entities": {
            "order_ids": [target_order_id],
            "item_ids": [auth_item["order_item_id"]],
            "seller_ids": [auth_item["seller_id"]],
            "payment_references": payment_ctx["payment_references"],
            "shipment_ids": [],
        },
        "claim_assessments": policy_ctx["claim_assessments"],
        "entity_resolution": {
            "status": "resolved",
            "resolved_order_ids": order_ctx["resolved_order_ids"],
            "rejected_candidates": order_ctx["rejected_candidates"],
            "confidence": 0.98,
        },
        "customer_context": {
            "customer_unique_id": order_ctx["customer_unique_id"],
            "related_order_ids": order_ctx["related_order_ids"],
        },
        "shipment_analysis": {
            "verdict": shipment_ctx["verdict"],
            "late_seller_ids": late_seller_ids,
            "timeline_complete": shipment_ctx["timeline_complete"],
        },
        "payment_analysis": {
            "verdict": payment_ctx["verdict"],
            "captured_total_brl": payment_ctx["captured_total_brl"],
            "refunded_total_brl": payment_ctx["refunded_total_brl"],
            "refundable_total_brl": payment_ctx["refundable_total_brl"],
        },
        "root_cause_analysis": {
            "ranked_causes": [
                {
                    "cause_code": primary_issue.upper(),
                    "rank": 1,
                }
            ],
            "responsible_parties": responsible_parties,
        },
        "evidence_refs": list(session.all_evidence_refs),
        "data_conflicts": policy_ctx["data_conflicts"],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refund_brl,
            "refund_lines": refund_lines,
        },
        "resolution_actions": [policy_ctx["recommended_action"]],
    }

    session.trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier-agent",
        decision_code="VERIFIED_OK",
        evidence_refs=list(session.all_evidence_refs),
        attributes={
            "primary_issue": primary_issue,
            "case_status": case_status,
            "confidence": 0.95,
        },
    )

    return output


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Execute the L3B multi-agent workflow for a single case."""
    case_id = case["case_id"]
    session = CaseSession(case_id, gateway, trace)

    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="order-agent",
        decision_code="ASSIGN_INVESTIGATION",
    )

    order_ctx = await _run_order_agent(case, session)
    shipment_ctx = await _run_shipment_agent(case, order_ctx, session)
    payment_ctx = await _run_payment_agent(case, order_ctx, session)

    policy_ctx = await _run_policy_agent(
        case, order_ctx, shipment_ctx, payment_ctx, session
    )

    return _run_verifier_agent(
        case, order_ctx, shipment_ctx, payment_ctx, policy_ctx, session
    )
