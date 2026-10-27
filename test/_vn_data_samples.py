"""Dữ liệu mẫu ghi lại từ nguồn thật (Vietcombank, SJC, xskt.com.vn, vang.today,
giaxanghomnay.com) cho test - không gọi mạng."""

# Rút gọn từ portal.vietcombank.com.vn/.../pXML.aspx (26/10/2026).
FX_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<!--For reference only. Only one request every 5 minutes!-->
<ExrateList>
  <DateTime>10/26/2026 8:03:40 AM</DateTime>
  <Exrate CurrencyCode="AUD" CurrencyName="AUSTRALIAN DOLLAR   " Buy="16,123.45" Transfer="16,286.31" Sell="16,808.60" />
  <Exrate CurrencyCode="EUR" CurrencyName="EURO                " Buy="28,235.03" Transfer="28,520.23" Sell="29,724.39" />
  <Exrate CurrencyCode="JPY" CurrencyName="YEN                 " Buy="164.12" Transfer="165.78" Sell="173.65" />
  <Exrate CurrencyCode="KWD" CurrencyName="KUWAITI DINAR       " Buy="-" Transfer="83,512.10" Sell="87,103.33" />
  <Exrate CurrencyCode="USD" CurrencyName="US DOLLAR           " Buy="25,620.00" Transfer="25,650.00" Sell="26,030.00" />
  <Source>Joint Stock Commercial Bank for Foreign Trade of Vietnam - Vietcombank</Source>
</ExrateList>"""

# Dạng JSON PriceService.ashx (method=GetSJCGoldPriceByDate) - các khoá vnstock đọc.
SJC_JSON = (
    b'{"success":true,"latestDate":"08:30 26/10/2026","data":['
    b'{"Id":1,"TypeName":"V\xc3\xa0ng SJC 1L, 10L, 1KG","BranchName":"H\xe1\xbb\x93 Ch\xc3\xad Minh",'
    b'"Buy":"148,000","BuyValue":148000000.0,"Sell":"150,000","SellValue":150000000.0},'
    b'{"Id":2,"TypeName":"V\xc3\xa0ng nh\xe1\xba\xabn SJC 99,99 1 ch\xe1\xbb\x89, 2 ch\xe1\xbb\x89, 5 ch\xe1\xbb\x89",'
    b'"BranchName":"H\xe1\xbb\x93 Ch\xc3\xad Minh","BuyValue":145500000.0,"SellValue":148000000.0},'
    b'{"Id":3,"TypeName":"V\xc3\xa0ng SJC 1L, 10L, 1KG","BranchName":"H\xc3\xa0 N\xe1\xbb\x99i",'
    b'"BuyValue":148000000.0,"SellValue":150020000.0}]}'
)

# Đúng định dạng xskt.com.vn RSS: mô tả plain text, "7: 2228: 37" dính dòng.
XSMN_RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>XSMN</title>
<item>
<title>KẾT QUẢ XỔ SỐ MIỀN NAM NGÀY 25/10 (Chủ Nhật)</title>
<link>https://xskt.com.vn/xsmn/ngay-25-10-2026</link>
<description>[Kiên Giang]
ĐB: 729510
1: 50113
2: 60224
3: 52751 - 93988
4: 93779 - 44343 - 13988 - 64590 - 13155 - 53655 - 21803
5: 7120
6: 1846 - 5018 - 3374
7: 2228: 37
[Tiền Giang]
ĐB: 083816
1: 11111
2: 22222
3: 33333 - 44444
4: 55555 - 66666 - 77777 - 88888 - 99999 - 12121 - 34343
5: 5656
6: 7878 - 9090 - 1212
7: 345
8: 16</description>
<pubDate>25/10/2026</pubDate>
</item>
<item>
<title>KẾT QUẢ XỔ SỐ MIỀN NAM NGÀY 24/10 (Thứ Bảy)</title>
<link>https://xskt.com.vn/xsmn/ngay-24-10-2026</link>
<description>[Hồ Chí Minh]
ĐB: 698440
1: 12345
7: 111
8: 22</description>
<pubDate>25/10/2026</pubDate>
</item>
</channel></rss>""".encode()

XSMB_RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>XSMB</title>
<item>
<title>KẾT QUẢ XỔ SỐ MIỀN BẮC NGÀY 25/10 (Chủ Nhật)</title>
<link>https://xskt.com.vn/xsmb/ngay-25-10-2026</link>
<description>ĐB: 30328
1: 16643
2: 06745 - 79482
3: 00981 - 90959 - 09681 - 71865 - 84063 - 92739
4: 3022 - 4825 - 7901 - 2729
5: 3353 - 3776 - 2498 - 3324 - 9116 - 0964
6: 221 - 602 - 786
7: 52 - 94 - 82 - 67</description>
<pubDate>Mon, 26 Oct 2026 08:04:56 GMT</pubDate>
</item>
</channel></rss>""".encode()

# Feed RIÊNG 1 tỉnh xskt.com.vn (rss-feed/tay-ninh-xstn.rss, ghi 27/10/2026): cùng
# dạng feed miền Bắc - không có "[Tây Ninh]", giải 7 và 8 dính dòng "7: 1338: 59".
XSTN_RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>Kết quả xổ số Tây Ninh</title>
<item>
<title>KẾT QUẢ XỔ SỐ TÂY NINH NGÀY 22/10 (Thứ Năm)</title>
<link>https://xskt.com.vn/xstn/ngay-22-10-2026</link>
<description>
ĐB: 058734
1: 03697
2: 40234
3: 37652 - 14960
4: 80107 - 54749 - 64858 - 41700 - 35174 - 91786 - 79270
5: 8807
6: 0148 - 6332 - 3319
7: 1338: 59</description>
<pubDate>Tue, 27 Oct 2026 06:24:18 GMT</pubDate>
</item>
<item>
<title>KẾT QUẢ XỔ SỐ TÂY NINH NGÀY 15/10 (Thứ Năm)</title>
<link>https://xskt.com.vn/xstn/ngay-15-10-2026</link>
<description>
ĐB: 636638
1: 30501
2: 74151
3: 86013 - 07895
4: 41713 - 60034 - 90237 - 42768 - 61726 - 60797 - 78798
5: 5423
6: 9148 - 2440 - 2642
7: 7758: 77</description>
<pubDate>Tue, 27 Oct 2026 06:24:18 GMT</pubDate>
</item>
<item>
<title>KẾT QUẢ XỔ SỐ TÂY NINH NGÀY 26/01 (Thứ Sáu)</title>
<link>https://xskt.com.vn/xstn/ngay-26-1-2007</link>
<description>
ĐB: 111111
1: 22222</description>
<pubDate>Tue, 27 Oct 2026 06:24:18 GMT</pubDate>
</item>
</channel></rss>""".encode()

# https://www.vang.today/api/prices (07:30 27/10/2026): prices là object {mã: {...}}.
# Riêng change_* của SJL1L10 (+500.000) và SJ9999 (-300.000) sửa tay để test ▲▼.
VANG_TODAY_JSON = b"""{"success":true,"timestamp":1793061003,"time":"07:30","date":"2026-10-27",
"count":12,"prices":{
"XAUUSD":{"name":"World Gold (XAU/USD)","buy":4015.1,"sell":0,"change_buy":9,"change_sell":0,"currency":"USD"},
"VIETTINMSJC":{"name":"Viettin SJC","buy":138700000,"sell":141700000,"change_buy":0,"change_sell":0,"currency":"VND"},
"DOJINHTV":{"name":"DOJI Jewelry","buy":137100000,"sell":141100000,"change_buy":0,"change_sell":0,"currency":"VND"},
"DOHNL":{"name":"DOJI Hanoi","buy":138700000,"sell":141700000,"change_buy":0,"change_sell":0,"currency":"VND"},
"DOHCML":{"name":"DOJI HCM","buy":138700000,"sell":141700000,"change_buy":0,"change_sell":0,"currency":"VND"},
"SJL1L10":{"name":"SJC 9999","buy":138700000,"sell":141700000,"change_buy":500000,"change_sell":500000,"currency":"VND"},
"SJ9999":{"name":"SJC Ring","buy":135000000,"sell":139000000,"change_buy":-300000,"change_sell":-300000,"currency":"VND"},
"BTSJC":{"name":"Bao Tin SJC","buy":136800000,"sell":141700000,"change_buy":0,"change_sell":0,"currency":"VND"},
"BT9999NTT":{"name":"Bao Tin 9999","buy":136800000,"sell":140800000,"change_buy":0,"change_sell":0,"currency":"VND"},
"VNGSJC":{"name":"VN Gold SJC","buy":138700000,"sell":141700000,"change_buy":0,"change_sell":0,"currency":"VND"},
"PQHNVM":{"name":"PNJ Hanoi","buy":137700000,"sell":141700000,"change_buy":0,"change_sell":0,"currency":"VND"},
"PQHN24NTT":{"name":"PNJ 24K","buy":137700000,"sell":140700000,"change_buy":0,"change_sell":0,"currency":"VND"}}}"""

# Dựng theo cấu trúc trang https://giaxanghomnay.com/ (27/10/2026, số liệu đúng bảng
# trang công bố): bảng Petrolimex 2 vùng (tiêu đề <th>), bảng PVOIL (tiêu đề ở hàng
# <td> đầu), bảng lịch sử điều chỉnh và bảng giá thế giới phải bị bỏ qua.
FUEL_HTML = """<!doctype html><html lang="vi"><head><meta charset="utf-8"><title>Giá xăng hôm nay</title></head>
<body><h1>Giá xăng dầu hôm nay</h1>
<p><img alt="Bảng giá theo Petrolimex" src="/p.png"> giá theo Petrolimex</p>
<table class="table">
<thead><tr><th>Xăng/dầu</th><th>Tăng giảm</th><th>Tăng giảm kỳ trước*</th><th>Giá vùng 1</th><th>Giá vùng 2</th></tr></thead>
<tbody>
<tr><td>Xăng E5 RON 92</td><td>0</td><td>-1,610</td><td>25,060</td><td>25,560</td></tr>
<tr><td>Xăng E10 RON 95 Mức 5</td><td>0</td><td>-1,810</td><td>25,710</td><td>26,220</td></tr>
<tr><td>Xăng E10 RON 95 Mức 3</td><td>0</td><td>-1,810</td><td>24,910</td><td>25,400</td></tr>
<tr><td>Dầu hỏa 2-K</td><td>0</td><td>-360</td><td>30,050</td><td>30,650</td></tr>
<tr><td>DO 0,05S Mức 2</td><td>0</td><td>+450</td><td>29,250</td><td>29,830</td></tr>
<tr><td>DO 0,001S Mức 5</td><td>0</td><td>+450</td><td>30,650</td><td>31,260</td></tr>
</tbody></table>
<p>Giá của Petrolimex cập nhật 2026-10-27 00:00:04</p>
<p><img alt="Bảng giá theo PVOIL" src="/v.png"> giá theo PVOIL</p>
<table>
<tr><td>Xăng/dầu</td><td>Tăng giảm</td><td>Tăng giảm kỳ trước*</td><td>Giá</td></tr>
<tr><td>Xăng E10 RON 95 Mức 3</td><td>0</td><td>-1,810</td><td>24,910</td></tr>
<tr><td>Dầu DO 0,05S Mức 2</td><td>0</td><td>+450</td><td>29,250</td></tr>
<tr><td>Dầu DO 0,001S Mức 5</td><td>0</td><td>+450</td><td>30,650</td></tr>
</table>
<a href="/lich-su">Lịch sử thay đổi giá xăng dầu</a><h3>📅 Ngày 22/10/2026</h3>
<table><tr><th>Xăng/dầu</th><th>Giá cũ</th><th>Giá mới</th><th>Thay đổi</th><th>Thời gian</th></tr>
<tr><td>Xăng E5 RON 92</td><td>26,670</td><td>25,060</td><td>-1,610 ▼</td><td>15:00</td></tr></table>
<h2>Giá xăng dầu thế giới hôm nay</h2>
<table><tr><th>Xăng/dầu</th><th>Tăng giảm</th><th>Giá</th></tr>
<tr><td><img alt="USA"> WTI Crude</td><td>-1.940▼</td><td>$101.010</td></tr></table>
</body></html>""".encode()
