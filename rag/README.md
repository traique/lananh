# Kho kiến thức cho lệnh /rag

Đặt các file `.md` kiến thức của bạn vào thư mục này (kể cả thư mục con —
bot quét đệ quy). Lệnh `/rag <câu hỏi>` sẽ tìm đoạn khớp nhất rồi nhờ AI
diễn giải, có ghi nguồn.

File gốc của bạn là PDF → OCR sang md nên bẩn? Dùng lệnh
`/ragxuly <tên-file>`: bot gộp dòng ngắt cứng, bỏ số trang, nhờ AI thêm
heading + sửa lỗi OCR rồi ghi đè file (bản gốc được backup vào `_goc/`).
File dài được bot tự chia thành nhiều phần vừa ngữ cảnh và xử lý tuần tự;
người dùng không cần tự cắt file. Chỉ khi mọi phần đều đạt kiểm định bot mới
ghép kết quả và ghi đè file chính.

## Cách viết file để tra cứu tốt

- **Mỗi chủ đề một heading** (`#`, `##`, `###`...): bot chia từng đoạn dưới
  một heading thành một "mẩu kiến thức" độc lập, heading là tên chủ đề.
- **Heading nêu rõ từ khóa**: "## Chiến lược DCA" dễ tra hơn "## Cách của tôi".
- **Viết đặc tả, đừng viết truyện**: gạch đầu dòng, số liệu cụ thể.
- **Một mẩu ~1200 ký tự**: mục dài hơn sẽ được tự chia theo đoạn trống
  (dòng trắng giữa các đoạn), kèm nhãn "(phần i/N)"; đoạn đơn lẻ quá dài
  mới bị cắt cứng. **Nên xuống dòng trống giữa các ý** để chỗ chia tự nhiên.
- **File thô không heading vẫn dùng được**: toàn bộ file được coi là một mục
  và vẫn chia mẩu theo đoạn — nhưng có heading thì tra sẽ chuẩn hơn.
- File `README.md` (file này) bị bỏ qua khi tra cứu — ghi chú thoải mái.

## Ví dụ cấu trúc file

```markdown
# Đầu tư

## Chiến lược DCA
- Vào tiền 30% khi VNINDEX dưới MA200
- Chốt lời từng phần khi lãi 15%...

## Nguyên tắc quản trị rủi ro
- Không All-in, tối đa 10%/mã...
```
