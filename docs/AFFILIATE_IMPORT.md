# Import đối soát affiliate

Arya_Tool nhận CSV đã chuẩn hóa, validate toàn bộ batch trước khi ghi và dùng
`(source, external_event_id)` làm khóa chống nhập trùng.

## Định dạng

Ba cột bắt buộc:

| Cột | Nội dung |
|---|---|
| `external_event_id` | ID ổn định của click/order/event từ mạng affiliate |
| `event_type` | `view`, `click` hoặc `commission` |
| `occurred_at` | ISO-8601 có múi giờ, ví dụ `2026-07-28T08:00:00+07:00` |

Các cột tùy chọn:

| Cột | Nội dung |
|---|---|
| `amount` | Bắt buộc lớn hơn 0 cho commission; view/click phải bằng 0 |
| `currency` | Phải khớp `LAPLACE_SOCIAL_CURRENCY`, mặc định `VND` |
| `source` | Nguồn riêng của dòng; nếu trống dùng `--source` |
| `sub_id` | `job:<id>`, `post:<id>` hoặc idempotency key của publish job |
| `social_post_id` | ID nội dung, phải thuộc owner đang import |
| `affiliate_product_id` | ID sản phẩm, phải thuộc owner đang import |
| `social_account_id` | ID tài khoản, phải thuộc owner đang import |
| `publish_job_id` | ID publish job; tự suy ra post, product và account |
| `metadata_json` | JSON object nhỏ, không chứa token/cookie/secret |

Ví dụ:

```csv
external_event_id,event_type,occurred_at,amount,currency,sub_id
click-1001,click,2026-07-28T08:00:00+07:00,0,VND,job:42
order-9001,commission,2026-07-28T08:05:00+07:00,12500,VND,job:42
```

## Chạy import

Luôn chạy dry-run trước:

```bash
python -m laplace.social.affiliate_import reports/network.csv \
  --user-id 1 \
  --source shopee \
  --dry-run
```

Khi report có `"ok": true`, bỏ `--dry-run` để ghi dữ liệu:

```bash
python -m laplace.social.affiliate_import reports/network.csv \
  --user-id 1 \
  --source shopee
```

Import lại cùng file sẽ bỏ qua event đã có. Nếu một dòng sai schema, currency,
owner hoặc mapping `sub_id`, toàn bộ file bị từ chối và không ghi batch dở dang.
Không đưa file đối soát có dữ liệu nhạy cảm vào Git.
