# Mã và dữ liệu bên thứ ba

## Nhật Nguyệt (nhatnguyet.org)

Nguồn: https://github.com/Nhat-Nguyet/nhatnguyet-lunar-ziwei-bazi-numerology

### Mã nguồn (MIT)

Các phần dưới đây chuyển ngữ (có sửa cho hợp code của repo) từ gói Python
`python/nhatnguyet/` của Nhật Nguyệt; đầu mỗi module/đoạn code có ghi nguồn:

- `services/tuvi.py`: phần hạn năm (lưu niên, tiểu hạn, lưu Tứ Hóa, sao lưu), từ `tu_vi_han.py`.
- `services/bat_tu.py`: tàng can, thập thần, trường sinh, mệnh cung, thai nguyên, đại vận,
  tiểu vận, từ `bat_tu.py` (tứ trụ dùng lại code Kỳ Môn sẵn có của repo).
- `services/phong_thuy.py`: bảng ba thước Lỗ Ban và quy tắc cung phi Bát Trạch, từ
  `_thuoc_lo_ban.py`, `fengshui.py`.
- `core/lunar_math.py`: cách sửa bước lùi tháng ("ngày 0" ở 07/05/2054, 09/04/2062) theo
  phân tích trong `docs/paper/hnd_compare.py`.

```
MIT License

Copyright (c) 2026 Nhat Nguyet (nhatnguyet.org)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### Dữ liệu (CC BY 4.0)

Dữ liệu: Nhật Nguyệt (nhatnguyet.org), giấy phép
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Đã sửa đổi như ghi dưới đây.

- `assets/le_hoi_viet_nam.json`: bộ `le-hoi-viet-nam` (phiên bản 1.7.0) và danh sách 34
  tỉnh `tinh-2025`; chỉ giữ cột tiếng Việt bot dùng, đổi tên cột
  (`scripts/make_le_hoi_data.py`). Hiện ở `/lich` và trang `/lich/le-hoi` kèm dòng ghi nguồn.
- `test/data/nhatnguyet/*.json`: trích một phần ca kiểm `ca-kiem/` (engine 2.3.0), bỏ khoá
  tiếng Anh/Trung, diễn giải dài, cách cục, thần sát, nguyệt hạn
  (`scripts/make_golden_nhatnguyet.py`). Chỉ dùng làm test hồi quy.

### Không dùng

Bộ tri thức và bộ lời luận của Nhật Nguyệt (`du-lieu/knowledge/`, `*-readings`,
`*-knowledge` trên Hugging Face) theo CC BY-NC 4.0 từ 20/11/2026 (cấm dùng thương mại):
repo này không chép chữ nào từ đó; mọi câu ý nghĩa, lời giải thích là tự viết. Tên và biểu
trưng Nhật Nguyệt là thương hiệu, không dùng.
