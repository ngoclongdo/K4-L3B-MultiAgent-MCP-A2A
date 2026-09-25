# L3B Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

`solve_case()` (`src/student_agent/workflow.py`) chạy một state machine tuần tự, không dùng framework ngoài. Mỗi bước phát `task_assigned` + `handoff` trước khi actor kế tiếp làm việc; `cli.py` đã tự phát `case_received`/`case_finalized` bao quanh lời gọi `solve_case`.

```text
Input → Entity/Customer Resolver → Coordinator handoff → {Order/Item, Shipment, Payment/Refund} agents → Policy agent → Conflict Resolver → Verifier → Output
            │                              │                  │             │
            └──────────────────────────── MCP (EvidenceGateway, least-privilege allow-list per actor) ──┴──────────── Trace (TraceWriter.emit, observable events only)
```

Luồng thực tế trong `solve_case`:
`coordinator → entity-resolver → order-agent → shipment-agent → payment-agent → policy-agent → conflict-resolver → verifier`. Mỗi mũi tên là một `handoff` event; verifier validate output qua `Contracts.validate_output` (dùng `trace.contracts`, cùng instance cli.py truyền vào) trước khi trả về.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Entity/customer resolver | `candidate_order_ids`, `claimed_order_id`, `customer_unique_id_hint` | Gọi `get_order` cho từng candidate, xếp hạng theo evidence (order thật vs candidate placeholder), chọn 1 order, ghi rejected; gọi `get_customer_history` nếu `investigation_scope.include_customer_history` | `get_order`, `get_customer_history` | `entity_resolution`, `customer_context` → handoff `order-agent` |
| Coordinator | case, kết quả mỗi bước | Điều phối tuần tự, phát `task_assigned`/`handoff`, không tự suy luận nghiệp vụ | không gọi MCP | route giữa các actor |
| Order/item agent | `order_id` đã resolve | Lấy line item, seller_id liên quan, product context | `get_order_items`, `get_product_context` | `item_ids`, `seller_ids` → handoff `shipment-agent` |
| Shipment agent | `order_id`, order evidence, seller_ids | So `delivered_at` vs `estimated_delivery_date` (ưu tiên field của `get_shipment_summary`, fallback field của `get_order`), suy verdict và seller trễ | `get_shipment_summary`, `get_sellers` | `shipment_analysis` → handoff `payment-agent` |
| Payment/refund agent | `order_id` | Tổng captured từ `get_order_payments`, refunded từ `get_refund_timeline`, đối chiếu `get_payment_timeline` để phát hiện mismatch/duplicate | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` | `payment_analysis` → handoff `policy-agent` |
| Policy agent | `policy_version` | Lấy policy version áp dụng cho case (dùng làm evidence, không tạo luật mới) | `get_policy` | evidence bổ sung → handoff `conflict-resolver` |
| Conflict resolver | kết quả order/shipment/payment | So khớp order-items-total vs payment-captured-total, phát hiện timeline thiếu; chọn `selected_source` theo domain precedence (nguồn chuyên biệt hơn thắng nguồn tổng quát) | không gọi MCP | `data_conflicts` → handoff `verifier` |
| Verifier | output nháp, `ctx.evidence_refs` | Lọc evidence_ref không thuộc case, clamp confidence về [0,1], check refunded ≤ captured, validate schema qua `Contracts.validate_output`, emit `verification_completed` | không gọi MCP | output đã validate |

Least privilege được enforce bằng code: `CaseContext.call(actor, tool_name, ...)` trong `agents.py` tra `ALLOWED_TOOLS[actor]` và raise `ToolPermissionError` nếu actor gọi tool ngoài allow-list; actor không có trong bảng (coordinator, conflict-resolver, verifier) mặc định bị từ chối mọi tool call.

## 3. Entity resolution và A2A protocol

- Candidate ranking: với mỗi `order_id` trong `candidate_order_ids` (hoặc `claimed_order_id` nếu thiếu candidate), gọi `get_order`; candidate không trả evidence hợp lệ (lỗi hoặc `data.order_id` rỗng) bị đưa vào `rejected_candidates`.
- Nếu còn candidate hợp lệ khớp đúng `claimed_order_id` → chọn candidate đó, `status=resolved`, `confidence=0.9`. Nếu nhiều candidate hợp lệ và candidate khớp `claimed_order_id` → `resolved` (0.85); nếu nhiều candidate hợp lệ nhưng không khớp → `ambiguous` (0.55). Không còn candidate hợp lệ → `not_found` (0.0). Mọi candidate hợp lệ không được chọn cũng bị thêm vào `rejected_candidates`.
- Quyết định resolve được ghi bằng `policy_decided` (actor `entity-resolver`, `decision_code=entity_{status}`), không chép lý do suy luận, chỉ chép `chosen_order_id` và `candidate_count`.
- A2A message envelope: mọi trace event là dict được `TraceWriter.emit` chuẩn hoá, luôn có `case_id` để correlate. Handoff explicit qua cặp event `task_assigned` (actor=`coordinator`, target=actor kế) + `handoff` (actor=actor hiện tại, target=actor kế) — đây là "message" A2A, không mang nội dung suy luận.
- Timeout/vòng lặp: state machine là danh sách bước tuyến tính cố định (không có loop/retry ở cấp state machine), nên không thể xảy ra vòng lặp actor. Timeout ở cấp tool call xem mục 5.

## 4. Evidence và conflict lifecycle

- Mọi lời gọi MCP đi qua `CaseContext.call`: cache theo `(tool_name, sorted(kwargs))` trong phạm vi 1 case (dedupe gọi trùng), luôn truyền `case_id=ctx.case_id`. `EvidenceGateway.call` (đã sửa `mcp_gateway.py` để khớp field name `is_error`/`structured_content` của package `mcp==2.2.0` đang cài) validate response qua `Contracts.validate_evidence` trước khi trả về — evidence sai schema sẽ raise ngay tại gateway.
- `evidence_ref` chỉ lấy nguyên văn từ response, không sinh/sửa; mỗi ref được thêm vào `ctx.evidence_refs` (set trong phạm vi case hiện tại) và ghi vào `tool_result_consumed`.
- Conflict được phát hiện tại 2 điểm:
  1. `detect_conflicts` (trước verifier): so tổng `price+freight_value` từ `get_order_items` với tổng `payment_value` từ `get_order_payments` (lệch > 0.05 BRL); so thời gian giao hàng thiếu dữ liệu (`shipment_timeline`).
  2. `verify_output` (trong verifier): `refunded_total_brl > captured_total_brl` → conflict `REFUND_EXCEEDS_CAPTURE`.
  Mỗi conflict là 1 phần tử `data_conflicts` với `sources` (tên tool), `selected_source` (tool có domain thẩm quyền cao hơn, vd `get_order_payments` ưu tiên hơn `get_order_items` cho số tiền) và `resolution_code`. Conflict resolver phát `policy_decided` cho từng conflict phát hiện trước verifier.
- Evidence không dùng chéo case: `CaseContext` là instance mới mỗi lần `solve_case` chạy, cache và `evidence_refs` không share giữa case.
- Map evidence vào claim: `assess_claims` gắn `evidence_refs` liên quan theo domain của từng `claim_id` (shipment evidence cho claim giao hàng, payment evidence cho claim thanh toán/hoàn tiền).

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout / lỗi transient | 1 retry (2 lần gọi tổng) | Tool trả `None`, domain liên quan hạ xuống `insufficient_evidence`/giá trị `null`, không suy đoán số liệu | `tool_result_consumed` actor=<agent>, `decision_code=tool_call_failed`, `attributes.error` |
| Entity not found/ambiguous | không retry (danh sách candidate cố định) | `entity_resolution.status=not_found/ambiguous`, các bước sau chạy với `order_id=None` → mọi domain trả `insufficient_evidence`/rỗng | `policy_decided` actor=entity-resolver, `decision_code=entity_not_found`/`entity_ambiguous` |
| Source conflict | không retry (deterministic rule) | Chọn `selected_source` theo domain precedence, giữ nguyên giá trị đo được (không sửa số), thêm bản ghi vào `data_conflicts` | `policy_decided` actor=conflict-resolver, `decision_code=<resolution_code>` (vd `PAYMENT_SOURCE_PREFERRED`) |
| Invalid specialist result (evidence không đủ để xác định verdict) | không retry | Verdict domain = `insufficient_evidence` (shipment/payment) thay vì đoán | không có event riêng — thể hiện qua field verdict + `verification_completed.attributes` |

Query budget/cache strategy: allow-list per actor giới hạn 10 tool có thể dùng cho toàn case (2–3 tool/actor), cache theo `(tool, args)` trong 1 case tránh gọi lặp `get_order` khi candidate trùng nhau, không quét toàn bộ dataset (mọi call đều tham chiếu `order_id`/`customer_unique_id` cụ thể lấy từ input case). Retry giới hạn 1 lần, idempotent (đọc dữ liệu, không side-effect), và không bao giờ thay thế evidence thiếu bằng dữ liệu bịa — chỉ hạ confidence/đánh dấu `insufficient_evidence`.

## 6. Verification invariants

`verify_output` (trước khi `solve_case` return) kiểm tra:

- Schema: `ctx.trace.contracts.validate_output(output, ...)` — dùng đúng `Contracts` instance mà `cli.py` truyền cho `TraceWriter`, raise nếu sai schema (fail fast thay vì trả output không hợp lệ).
- Entity scope & rejected candidates: `entity_resolution.rejected_candidates` luôn chứa mọi candidate không được chọn (kể cả candidate hợp lệ nhưng thua tie-break).
- Evidence ownership: `output["evidence_refs"]` và từng `claim_assessments[].evidence_refs` bị lọc lại, chỉ giữ ref có trong `ctx.evidence_refs` (ref thực sự trả về từ gateway trong case này).
- Timeline: `shipment_analysis.timeline_complete` chỉ `true` khi cả `delivered` và `estimated` đều có giá trị đọc được.
- Payment/refund totals: nếu `refunded_total_brl > captured_total_brl + 0.01` → thêm `data_conflicts` entry `REFUND_EXCEEDS_CAPTURE`, `decision_code=conflict_flagged`.
- Responsibility vs actions: `root_cause_analysis.responsible_parties` suy từ `shipment_analysis.verdict`/`payment_analysis.verdict`; `resolution_actions` map 1-1 theo `assessment.primary_issue` (bảng `RESOLUTION_ACTIONS` trong `workflow.py`).
- Confidence bounds: mọi confidence (`assessment`, `entity_resolution`, từng `claim_assessments[]`) được clamp về `[0, 1]` trước khi trả.

## 7. Reproducibility

- Model/config: không dùng LLM trong `solve_case` — toàn bộ là Python logic tất định (rule-based), không có seed ngẫu nhiên.
- Dependency: pin trong `pyproject.toml` (`httpx2>=2,<3`, `jsonschema[format]>=4.25,<5`, `mcp>=2,<3`, `python-dotenv>=1.1,<2`); môi trường dev cài `mcp==2.2.0` — đã điều chỉnh `mcp_gateway.py` để khớp field name của version này (`is_error`, `structured_content`).
- Concurrency: `solve_case` chạy tuần tự (không `asyncio.gather`) trong 1 case; `cli.py run` xử lý case tuần tự theo thứ tự `case_set.case_ids`.
- Lệnh chạy: `day09 mcp-tools` (discover tool), `day09 run` (chạy toàn bộ 100 case — không chạy trong phiên implement này, xem báo cáo), `day09 validate`, `pytest -q`.
- Giới hạn tài nguyên: mỗi case tối đa ~10 tool call (bị chặn cứng bởi allow-list + cache dedupe); không giới hạn thời gian ngoài timeout mặc định của `httpx2.Timeout(300.0, ...)` trong `mcp_gateway.py`.
