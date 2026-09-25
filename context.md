# Hướng Dẫn & Quy Chuẩn Hệ Thống L3B Multi-Agent MCP

## 1. Khóa Public Contracts (`contracts/schemas/`)

Hệ thống bắt buộc tuân thủ 100% các JSON Schema:
* `l3b-output-v2.schema.json`: Cấu trúc output bắt buộc cho từng case.
* `trace-event-v1.schema.json`: Schema cho trace log observable.
* `submission-manifest-v2.schema.json`: Schema cho manifest khi đóng gói nộp bài.
* `mcp-evidence-response-v1.schema.json`: Cấu trúc phong bì (envelope) trả về từ MCP Gateway.

> **Nguyên tắc cốt lõi:** Tuyệt đối không thêm bất kỳ trường nào ngoài schema (`additionalProperties: false`). JSON Schema là chân lý chuẩn mực tối cao.

---

## 2. Điểm Triển Khai Multi-Agent

* **Vị trí chính:** `src/student_agent/workflow.py`
* **Entrypoint:**
  ```python
  async def solve_case(
      case: dict[str, Any], 
      gateway: EvidenceGateway, 
      trace: TraceWriter
  ) -> dict[str, Any]:
      """Triển khai coordinator và các specialist agents tại đây."""
      ...
  ```
* **Linh hoạt Framework:** Tự do sử dụng thuần Python async state-machine, LangGraph, CrewAI... Hệ thống chỉ đánh giá kết quả nghiệp vụ, tính hợp lệ của evidence MCP và trace log.
* **Tài liệu kiến trúc:** Hoàn thiện `ARCHITECTURE.md` mô tả luồng handoff, quyền hạn tool (`tool permissions`) của từng agent và cơ chế fallback/retry.

---

## 3. Quy Tắc Gọi MCP Gateway

Mọi agent chuyên trách truy vấn bằng chứng qua MCP Gateway theo đúng phạm vi case:

| # | Quy tắc | Hệ quả nếu vi phạm |
| :-: | :--- | :--- |
| **1** | Truyền đúng `case_id` cho mọi call MCP | Bị từ chối truy cập (403 Forbidden) |
| **2** | **KHÔNG** tự sinh hoặc sửa đổi `evidence_ref` | **Hard Gate: 0 điểm toàn bài** |
| **3** | Chỉ trích dẫn evidence thực sự hỗ trợ kết luận | Trừ điểm *Evidence Relevance* |
| **4** | Ghi nhận event `tool_result_consumed` vào trace | Không được công nhận tính xác thực |
| **5** | Server lưu Audit độc lập (Hash, Latency, Status) | Bị phát hiện nếu giả mạo trace client |

### Mẫu gọi Tool & Ghi Trace:

```python
# 1. Gọi tool lấy bằng chứng từ MCP
evidence = await gateway.call(
    "get_order",
    case_id=case["case_id"],
    order_id=order_id,
)
evidence_ref = evidence["evidence_ref"]
order_data = evidence["data"]

# 2. Ghi nhận sự kiện tiêu thụ bằng chứng vào trace audit
trace.emit(
    case_id=case["case_id"],
    event_type="tool_result_consumed",
    actor="order-agent",
    tool_name="get_order",
    evidence_refs=[evidence_ref],
)
```

---

## 4. Trách Nhiệm Của Các Agent Chuyên Trách

### Policy Agent
Ra quyết định dựa trên chính sách (`contracts/scoring/scoring-policy-v2.json`):
* **Primary Issue:** Xác định lỗi cốt lõi (`canceled_order_paid`, `late_delivery_seller`, `late_delivery_logistics`, `payment_mismatch`,...).
* **Responsible Party:** Phân định rõ bên chịu trách nhiệm (`seller`, `platform`, `logistics_provider`, `payment_provider`, `customer`).
* **Financial Resolution:** Tính toán số tiền hoàn trả (`recommended_refund_brl`, `refund_lines`).
* **Resolution Actions:** Đề xuất các hành động xử lý cụ thể.

### Verifier Agent & Hiệu Chuẩn Tin Cậy (Calibration)
* **Cross-field Consistency:** Đảm bảo `primary_issue`, `responsible_parties` và `financial_resolution` logic và nhất quán (ví dụ: lỗi do seller thì đơn vị logistics không thể chịu trách nhiệm hoàn tiền).
* **Confidence Calibration:** Tính toán độ tin cậy `confidence` $\in [0.0, 1.0]$ dựa trên chất lượng và độ đầy đủ của evidence (tránh tự tin 1.0 nếu có mâu thuẫn dữ liệu).
* **Lifecycle Events:** Đảm bảo `traces/trace.jsonl` ghi nhận đầy đủ chuỗi vòng đời sự kiện:
  $$\text{case\_received} \rightarrow \text{task\_assigned} \rightarrow \text{tool\_result\_consumed} \rightarrow \text{handoff} \rightarrow \text{policy\_decided} \rightarrow \text{verification\_completed} \rightarrow \text{case\_finalized}$$