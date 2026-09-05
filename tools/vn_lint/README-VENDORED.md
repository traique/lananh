# vn_lint — vendor từ vietnamese-language-skill

`scripts/validate_copy.py` + `references/` copy nguyên văn từ skill
`vietnamese-finance-copy` trong repo
[trussary/vietnamese-language-skill](https://github.com/trussary/vietnamese-language-skill)
(thư mục `skills/vietnamese-finance-copy/`).

Chọn bản `finance-copy` (thay vì `business-comms`/`tech-writing`) vì Lan Anh có
nội dung chứng khoán/tiền bạc thật, nên rule FIN001 (ngôn từ cam kết lợi
nhuận) và LAW001 (siêu cấp từ bị luật cấm) hữu ích nhất ở đây - còn 3 rule dùng
chung mọi skill (NFC001, TONE001, DIA001) thì bản nào cũng giống hệt nhau
(sync từ `shared/`).

Dùng trong `test/test_vn_lint.py`, chỉ chặn ở mức **lỗi** (không chặn cảnh
báo - xem docstring trong file test).

## Cập nhật

Script tự ghi rõ nguồn + hash ở dòng đầu file
(`scripts/validate_copy.py`):

```
# GENERATED FILE — do not edit. Source: shared/scripts/validate_copy.py (sha256 ...)
```

Khi cần bản mới: tải lại 2 thư mục `scripts/` và `references/` từ
`skills/vietnamese-finance-copy/` của repo skill, ghi đè nguyên thư mục này,
chạy lại `pytest test/test_vn_lint.py` để chắc chưa có lỗi mới phát sinh do
rule thay đổi.

Không sửa tay các file trong `scripts/`/`references/` - sửa ở repo skill gốc
rồi vendor lại, để không bị lệch khi đồng bộ lần sau.
