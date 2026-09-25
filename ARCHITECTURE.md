# L3B Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Luồng xử lý tuần tự chính, với specialist agents chạy song song ở Phase 2:

```text
Input Case
    │
    ▼
┌──────────────────┐
│   Coordinator    │  (cli.py emits case_received)
│   Phase 0        │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│ Entity Resolver  │  → Resolve candidates → get_order, get_customer_history
│   Phase 1        │  → Identify customer, accepted/rejected order IDs
└────────┬─────────┘
         │ handoff → coordinator
         ▼
┌──────────────────────────────────────────────────────┐
│              Phase 2: Parallel Specialists           │
│  ┌─────────────┐ ┌───────────────┐ ┌──────────────┐ │
│  │ Order/Prod  │ │  Shipment     │ │ Payment/     │ │
│  │   Agent     │ │   Agent       │ │ Refund Agent │ │
│  └──────┬──────┘ └───────┬───────┘ └──────┬───────┘ │
│         │ handoff        │ handoff         │ handoff  │
└─────────┼────────────────┼─────────────────┼─────────┘
          └────────────────┼─────────────────┘
                           ▼
                  ┌──────────────────┐
                  │  Policy Agent    │  → primary_issue, responsible_parties,
                  │   Phase 3        │    financial_resolution, actions
                  └────────┬─────────┘
                           │ policy_decided
                           ▼
                  ┌──────────────────┐
                  │ Conflict Resolver│  → data_conflicts
                  │   Phase 4        │
                  └────────┬─────────┘
                           │
                           ▼
                  ┌──────────────────┐
                  │    Verifier      │  → confidence calibration
                  │   Phase 5        │    cross-field consistency check
                  └────────┬─────────┘
                           │ verification_completed
                           ▼
                     Output JSON
                  (cli.py emits case_finalized)
```

Tất cả MCP calls đi qua `EvidenceGateway`. Trace events được ghi tại mọi điểm chuyển giao.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| **Coordinator** | Case JSON | Điều phối luồng, gán task, tổng hợp output | Không gọi tool trực tiếp | Orchestrate pipeline |
| **Entity/Customer** | `candidate_order_ids`, `customer_unique_id_hint` | Resolve entity, xác định order hợp lệ, lấy customer history | `get_order`, `get_customer_history` | `entity_result` dict → coordinator |
| **Order/Product** | `order_data_map` từ entity agent | Thu thập item IDs, seller IDs, product context | `get_product` | `order_result` dict → coordinator |
| **Shipment** | `resolved_order_ids` | Phân tích timeline giao hàng, xác định delay | `get_shipment` | `shipment_result` dict → coordinator |
| **Payment/Refund** | `resolved_order_ids` | Tính tổng captured, refunded, refundable | `get_payment`, `get_refund` | `payment_result` dict → coordinator |
| **Policy** | Kết quả tổng hợp từ specialists | Xác định primary_issue, responsible_parties, actions | Không gọi MCP | `primary_issue`, `resolution_actions` |
| **Conflict Resolver** | Kết quả shipment + payment + primary_issue | Phát hiện mâu thuẫn giữa nguồn dữ liệu | Không gọi MCP | `data_conflicts` list |
| **Verifier** | Toàn bộ kết quả pipeline | Cross-field consistency, confidence calibration | Không gọi MCP | `confidence` score, PASS/FAIL |

Áp dụng least privilege: Entity agent chỉ gọi `get_order` + `get_customer_history`; Order agent chỉ gọi `get_product`; Shipment agent chỉ gọi `get_shipment`; Payment agent chỉ gọi `get_payment` + `get_refund`. Policy/Conflict/Verifier không gọi MCP tool.

## 3. Entity resolution và A2A protocol

### Quy trình resolve candidate:
1. Duyệt tuần tự `candidate_order_ids`, gọi `get_order` cho từng candidate.
2. Candidate nào MCP trả về data hợp lệ → **accepted** vào `resolved_order_ids`.
3. Candidate nào MCP lỗi / không tìm thấy → **rejected** vào `rejected_candidates`.
4. Nếu không candidate nào resolve được, fallback thử `claimed_order_id` riêng.

### Confidence threshold:
- 1 candidate resolved → confidence = 0.95
- Nhiều candidates resolved → confidence = 0.80
- Không resolve được → confidence = 0.20, status = `not_found`

### Message flow (A2A):
- Mỗi agent nhận input dưới dạng Python dict, trả về Python dict.
- `case_id` được truyền xuyên suốt qua tham số, đảm bảo correlation.
- Handoff được ghi trace event `handoff` với `actor` → `target`.

### Vòng lặp:
- Không có retry vòng lặp giữa agents. Pipeline là DAG một chiều.
- Mỗi MCP tool call chỉ được gọi tối đa 1 lần per entity per case.

## 4. Evidence và conflict lifecycle

### Validate MCP response:
- `EvidenceGateway.call()` tự validate response theo `mcp-evidence-response-v1.schema.json`.
- Nếu validation fail → raise exception → `_safe_call` trả `None`.

### Lưu `evidence_ref`:
- Mỗi MCP call thành công → lấy `evidence["evidence_ref"]` → append vào agent-level list.
- **Không bao giờ** tự tạo hoặc sửa đổi `evidence_ref`.

### Emit `tool_result_consumed`:
- Ngay sau khi nhận MCP response thành công, agent emit:
  ```
  trace.emit(event_type="tool_result_consumed", actor=<agent>, tool_name=<tool>, evidence_refs=[ref])
  ```

### Source conflict resolution:
- Conflict Resolver so sánh shipment verdict vs claim topic và payment verdict vs claim topic.
- Khi có mâu thuẫn: tạo `dataConflict` object với `selected_source` = nguồn authoritative (MCP data).
- Resolution code: `AUTHORITATIVE_SOURCE_PRIORITY`.

### Evidence scope:
- Evidence không được tái sử dụng giữa các case (mỗi case chạy pipeline riêng biệt).

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout | 0 (no retry) | Return `None` from `_safe_call`, agent proceeds with partial data | Warning logged, no trace event |
| Entity not found/ambiguous | 0 | `status="not_found"`, `confidence=0.2` | handoff with `status` attribute |
| Source conflict | 0 | Select authoritative source, record in `data_conflicts` | `policy_decided` with `decision_code` |
| Invalid specialist result | 0 | Use default/empty values | Warning logged |

### Query budget strategy:
- **Per-case tool calls**: Entity agent gọi tối đa `len(candidates) + 1` (customer history) lần. Mỗi specialist gọi tối đa `len(resolved_order_ids)` lần per tool. Product calls chỉ khi `include_product_context=true`.
- **Cache**: Không có cross-case cache (mỗi case pipeline độc lập). Trong case, order data từ entity agent được reuse bởi order agent (không gọi lại `get_order`).
- **Tránh gọi thừa**: `get_product` chỉ gọi khi case yêu cầu `include_product_context`. Retry budget = 0 cho mọi tool.

## 6. Verification invariants

Trước khi finalize output, Verifier kiểm tra:

1. **Schema compliance**: Output phải khớp `l3b-output-v2.schema.json` (được `Contracts.validate_output` verify).
2. **Entity scope**: `resolved_order_ids` ⊆ `candidate_order_ids ∪ {claimed_order_id}`.
3. **Rejected candidates**: `rejected_candidates ∩ resolved_order_ids = ∅`.
4. **Evidence ownership**: Tất cả `evidence_refs` trong output đến từ MCP calls của case hiện tại.
5. **Claim linkage**: Mỗi claim assessment tham chiếu evidence thực tế.
6. **Timeline consistency**: Nếu `shipment_analysis.timeline_complete=true` thì verdict ≠ `insufficient_evidence`.
7. **Payment/refund totals**: `refundable_total_brl = max(captured - refunded, 0)`.
8. **Source precedence**: MCP data ưu tiên hơn customer claim khi có mâu thuẫn.
9. **Responsibility/action consistency**: `responsible_parties` phải consistent với `primary_issue`.
10. **Confidence bounds**: `[0.1, 0.95]` — không bao giờ = 1.0. Conflicts giảm confidence.

## 7. Reproducibility

- **Runtime**: Python 3.11+, thuần async (asyncio.gather cho parallel specialists).
- **Framework**: Thuần Python async state-machine, không dùng external agent framework.
- **Dependencies**: Theo `pyproject.toml` — httpx2, jsonschema, mcp, python-dotenv.
- **Concurrency**: 3 specialist agents chạy song song per case via `asyncio.gather`. Các case chạy tuần tự.
- **Random seed**: Không sử dụng randomness trong logic nghiệp vụ (chỉ `secrets.token_urlsafe` cho event_id trong trace).
- **Lệnh chạy**:
  ```bash
  python -m pip install -e ".[dev]"
  day09 run
  day09 validate
  day09 package --output dist/submission.zip
  ```
- **Giới hạn tài nguyên**: Không giới hạn cứng. MCP timeout mặc định 300s (từ gateway config).
