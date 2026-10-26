"""Dữ liệu mẫu ghi lại từ nguồn thật (Vietcombank, SJC, xskt.com.vn) cho test."""

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
