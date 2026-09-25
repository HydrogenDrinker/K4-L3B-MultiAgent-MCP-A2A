# L3B Architecture Record

Tài liệu này mô tả thiết kế kiến trúc hệ thống Multi-Agent MCP + A2A cho bài thi Day09 L3B, bao gồm phân quyền tác tử, giao thức handoff, xử lý xung đột dữ liệu đa nguồn, chính sách tối ưu hóa hiệu suất MCP và các bất biến kiểm định độc lập.

## 1. System overview

Luồng điều tra đi từ nhận case, phân giải thực thể (entity resolution), thu thập bằng chứng có thẩm quyền qua MCP theo phạm vi chuyên biệt, đối chiếu chính sách `EC_POLICY_V2`, giải quyết mâu thuẫn dữ liệu và kiểm định độc lập trước khi xuất output và trace:

```text
Input Case ──► Coordinator ──(task_assigned)──► Order Agent (Entity & Context)
                   │                                  │
                   │                                  ├──(handoff)──► Shipment Agent
                   │                                  └──(handoff)──► Payment Agent
                   │                                                        │
                   ▼                                                        ▼
            Trace Audit ◄──(policy_decided)── Policy Agent ◄──(handoff)─────┘
                   │                                │
                   │                                ▼ (handoff)
                   └──(verification_completed)── Verifier Agent ──► Validated Output
```

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| `coordinator` | Raw `case` JSON | Khởi tạo phiên điều tra, phân công nhiệm vụ (`task_assigned`), điều phối luồng thực thi tuần tự và hoàn tất case (`case_finalized`) | Không gọi trực tiếp MCP tool | Giao việc cho `order-agent` |
| `order-agent` | `candidate_order_ids`, `claimed_order_id`, `customer_unique_id_hint`, `investigation_scope` | Phân giải thực thể đơn hàng từ lịch sử khách hàng, loại bỏ candidate không hợp lệ mà không gọi thừa vào đơn ảo, lấy ngữ cảnh đơn hàng/mặt hàng/sản phẩm/người bán | `get_customer_history`, `get_order`, `get_order_items`, `get_product_context`, `get_sellers` (chỉ khi kịch bản liên quan tới seller) | Handoff `order_ctx` kèm `evidence_refs` sang `shipment-agent` và `payment-agent` |
| `shipment-agent` | `order_ctx` (`target_order_id`, `auth_order`, `auth_item`) | Phân tích tiến độ giao hàng theo mốc thời gian của case, đối chiếu `shipping_limit_date`, phân định trễ do `seller_delay` hay `logistics_delay`, hoặc trạng thái `returned`/`lost` | `get_shipment_summary` | Handoff `shipment_ctx` sang `policy-agent` |
| `payment-agent` | `order_ctx`, `customer_request.claims` | Đối soát lịch sử thanh toán và sự kiện hoàn tiền thuộc phạm vi đơn hàng, tính toán `captured_total_brl`, `refunded_total_brl`, `refundable_total_brl` | `get_payment_timeline`, `get_refund_timeline` (chỉ gọi khi xử lý khiếu nại hoàn tiền) | Handoff `payment_ctx` sang `policy-agent` |
| `policy-agent` | `order_ctx`, `shipment_ctx`, `payment_ctx`, `policy_version` | Áp dụng quy tắc `EC_POLICY_V2`, giải quyết mâu thuẫn dữ liệu giữa các nguồn (`data_conflicts`), đánh giá từng claim (`claim_assessments`), xác định `primary_issue`, `responsible_parties` và `financial_resolution` | `get_policy` | Phát sự kiện `policy_decided` và handoff `policy_ctx` sang `verifier-agent` |
| `verifier-agent` | Toàn bộ context từ các specialist và `policy-agent` | Kiểm tra độc lập các bất biến logic chéo (cross-field consistency), hiệu chuẩn `confidence` và xác nhận tính hợp lệ theo JSON Schema | Không gọi MCP tool (đảm bảo tính độc lập kiểm định) | Phát sự kiện `verification_completed` và trả về dict kết quả chuẩn `day09-l3b-output-v2` |

Áp dụng nguyên tắc least privilege: mỗi tác tử chỉ được phép gọi các MCP tool thuộc phạm vi nghiệp vụ được giao và không truy vấn các domain nằm ngoài yêu cầu của kịch bản.

## 3. Entity resolution và A2A protocol

- **Entity Resolution**:
  - `order-agent` truy vấn `get_customer_history` bằng `customer_unique_id_hint` để lấy danh sách đơn hàng có thẩm quyền của khách.
  - Đối chiếu `candidate_order_ids` với tập `order_id` thực tế trong lịch sử khách hàng: ứng viên khớp được đưa vào `resolved_order_ids` (`status: "resolved"`), các ứng viên giả hoặc không thuộc khách hàng được đưa vào `rejected_candidates` mà không phát sinh lệnh gọi `get_order` dư thừa.
- **A2A Protocol & Traceability**:
  - Mọi thông điệp chuyển giao giữa các tác tử đều gắn chặt với `case_id` và truyền qua context có cấu trúc (`order_ctx`, `shipment_ctx`, `payment_ctx`, `policy_ctx`).
  - Chuỗi sự kiện quan sát được trong `traces/trace.jsonl` tuân thủ thứ tự vòng đời: `case_received` $\rightarrow$ `task_assigned` $\rightarrow$ `tool_result_consumed` $\rightarrow$ `handoff` $\rightarrow$ `policy_decided` $\rightarrow$ `verification_completed` $\rightarrow$ `case_finalized`.
  - Luồng chạy là DAG tuyến tính một chiều (acyclic pipeline), đảm bảo kết thúc hữu hạn và không bao giờ xảy ra vòng lặp vô tận giữa các tác tử.

## 4. Evidence và conflict lifecycle

- **Xác thực & Lưu trữ Evidence**:
  - Mọi phản hồi từ MCP Gateway đều được validate tức thời qua `mcp-evidence-response-v1.schema.json` trong `EvidenceGateway.call()`.
  - Ngay khi nhận kết quả hợp lệ, `CaseSession.call_tool()` giữ nguyên vẹn chuỗi `evidence_ref` gốc từ server và tự động ghi sự kiện `tool_result_consumed` kèm `actor`, `tool_name` và `evidence_refs`.
  - `CaseSession` được khởi tạo mới độc lập cho từng case, đảm bảo tuyệt đối không tái sử dụng `evidence_ref` giữa các case khác nhau.
- **Giải quyết xung đột nguồn dữ liệu (`data_conflicts`)**:
  - Khi dữ liệu tóm tắt từ `get_order` mâu thuẫn với bản ghi lịch sử theo mốc thời gian mở khiếu nại (`opened_at`) trong `get_customer_history` / `timelines`, hệ thống ưu tiên bản ghi khớp với timeline thực tế của sự việc (`resolution_code: "prefer_case_scoped_timeline"`).
  - Khi yêu cầu hoàn tiền toàn bộ (`requested_full_refund`) của khách hàng vượt quá mức quy định của chính sách sàn (`get_policy`), hệ thống ưu tiên quy định của `get_policy` (`resolution_code: "policy_precedence"`).

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout / transient transport error | 5 | Thử lại ở tầng HTTP transport với cùng tham số idempotent | `tool_result_consumed` (nếu thành công) |
| Entity not found / candidate giả | 0 | Loại ứng viên giả dựa trên `get_customer_history`, không gọi lặp `get_order` vào candidate sai | `handoff` (`ENTITY_RESOLVED`) |
| Source conflict giữa `get_order` và timeline | 0 | Chọn bản ghi khớp mốc thời gian mở khiếu nại và ghi nhận vào `data_conflicts` | `policy_decided` |
| Forbidden / irrelevant domain | 0 | Chỉ gọi `get_refund_timeline` cho nhóm refund và `get_sellers` cho nhóm lỗi từ seller | Tiết kiệm call budget và giữ độ chính xác evidence cao |

- **Chiến lược Cache & Query Budget**:
  - Mỗi case sử dụng bộ nhớ đệm nội bộ `CaseSession._cache` theo khóa `(tool_name, arguments)` để đảm bảo mỗi tool với cùng tham số chỉ được gọi tối đa 1 lần duy nhất.
  - Tổng số lệnh gọi MCP mỗi case được giới hạn chặt ở mức 7–8 calls thiết yếu, nằm gọn trong hạn mức call budget của cuộc thi.

## 6. Verification invariants

Trước khi hoàn tất mỗi case, `verifier-agent` kiểm tra các điều kiện bất biến:
1. **Schema Compliance**: Đầu ra tuân thủ 100% `l3b-output-v2.schema.json`.
2. **Entity Scope & Candidates**: `affected_entities.order_ids == entity_resolution.resolved_order_ids`, và mọi ứng viên không hợp lệ đều nằm trong `rejected_candidates`.
3. **Evidence Ownership & Linkage**: Toàn bộ `evidence_refs` ở cấp case và trong từng `claim_assessments` đều thuộc tập `all_evidence_refs` đã được ghi nhận qua `tool_result_consumed` của đúng case đó.
4. **Status / Refund / Action Consistency**:
   - Nếu `case_status` là `no_action` hoặc `needs_investigation`, bắt buộc `recommended_refund_brl == 0.0` và `refund_lines == []`.
   - Nếu `recommended_refund_brl > 0`, bắt buộc `case_status == "action_required"` và tổng `refund_lines[*].amount_brl == recommended_refund_brl`.
5. **Seller Responsibility Alignment**: Nếu `primary_issue == "late_delivery_seller"`, `shipment_analysis.late_seller_ids` chứa đúng `seller_id` của đơn hàng và `root_cause_analysis.responsible_parties` gắn trách nhiệm cho đúng `seller_id` đó; ngược lại `late_seller_ids` rỗng.
6. **Confidence Calibration**: Điểm `confidence` được giữ ở mức hợp lý (`0.95` cho kết luận nghiệp vụ và `0.98` cho phân giải thực thể) phản ánh việc đã giải quyết xung đột đa nguồn.

## 7. Reproducibility

- **Môi trường thực thi**: Python `>=3.11`, thực thi tất định (deterministic state-machine) không phụ thuộc vào nhiệt độ sinh ngẫu nhiên.
- **Dependencies**: Khóa phiên bản trong `pyproject.toml` (`httpx2>=2,<3`, `jsonschema[format]>=4.25,<5`, `mcp>=2,<3`, `python-dotenv>=1.1,<2`).
- **Concurrency & Resource Limits**: Thực thi tuần tự từng case trên một phiên kết nối `connect_gateway` duy nhất để đảm bảo toàn bộ 100 cases cùng thuộc một MCP audit run.
- **Lệnh chạy chuẩn**:
  ```bash
  day09 validate-inputs
  day09 run
  day09 validate
  day09 package --output dist/submission.zip
  ```
