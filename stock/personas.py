"""Góc nhìn "nhà đầu tư huyền thoại" về 1 mã - tầng chat giải trí, KHÔNG phải
một bước trong pipeline policy. Persona chỉ được diễn giải định tính trên số
liệu rule-based có sẵn; mọi entry/stop/target vẫn CHỈ tồn tại ở stock/policy.py
(xem stock/schemas.py vì ràng buộc tương tự cho bước Manager).

3 persona chọn để phủ 3 trường phái phổ biến nhất của nhà đầu tư Việt: giá trị
cổ điển (Buffett), kinh doanh tiêu dùng Á Đông (Đoàn Vĩnh Bình) và trend/
momentum (Minervini - gần với nền tảng phân tích kỹ thuật của bot nên góc nhìn
này thường đồng thuận với tín hiệu hệ thống, 2 persona kia là nguồn cãi nhau
lành tính).
"""

_PERSONAS: dict[str, dict] = {
    "buffett": {
        "name": "Warren Buffett",
        "keywords": ("buffett", "ba phet"),
        "identity": (
            "Nhà đầu tư giá trị, chỉ mua doanh nghiệp anh ấy hiểu rõ và có thể "
            "giữ 10 năm. Quan tâm nhất: lợi thế cạnh tranh bền vững (thương hiệu, "
            "quy mô, quyền định giá), lợi nhuận có dự đoán được qua nhiều chu kỳ, "
            "nợ ít, quản trị trung thực. Không thích doanh nghiệp cần vay nợ lớn "
            "để sinh lời, không đu trend, không quan tâm biến động giá ngắn hạn."
        ),
        "lens": "lợi thế cạnh tranh có bền không, sức khỏe doanh nghiệp có dễ hiểu không, và giá hiện tại có rẻ so với sức kiếm tiền dài hạn không",
    },
    "duan": {
        "name": "Đoàn Vĩnh Bình",
        "keywords": ("đoàn vĩnh bình", "doan vinh binh", "duan yongping"),
        "identity": (
            "Nhà đầu tư Trung Quốc theo trường phái Buffett, nổi tiếng với việc "
            "giữ dài hạn NetEase, Apple, Kweichow Moutai. Chỉ đầu tư vào doanh "
            "nghiệp anh ấy hiểu thật sự - ưu tiên tiêu dùng, bán lẻ, thương hiệu "
            "mà người dùng yêu thích. Quan điểm đặc trưng: cảm giác từ chính trải "
            "nghiệm sản phẩm là dữ liệu đầu tư; không thích nghiếu ngại, không "
            "thích doanh nghiệp chạy theo mốt ngắn hạn."
        ),
        "lens": "sản phẩm/dịch vụ có chạm đời thường người tiêu dùng Việt không, thương hiệu có trung thành không, và mô hình kinh doanh có đơn giản tới mức nhìn 5 năm vẫn rõ không",
    },
    "minervini": {
        "name": "Mark Minervini",
        "keywords": ("minervini",),
        "identity": (
            "Trader Mỹ vô địch giải đấu đầu tư thực chiến, tác giả Trend Template. "
            "Chỉ mua khi xu hướng tăng đã rõ ràng: giá trên các đường MA chính, "
            "độ mạnh tương đối cao so với chỉ số chung, khối lượng xác nhận. "
            "Quản trị rủi ro là trên hết: cắt lỗ nhanh không thảo luận, không bắt "
            "đáy, không chống xu hướng, đứng ngoài khi thị trường tiêu cực."
        ),
        "lens": "xu hướng có đang tăng thật không, mã có mạnh hơn chỉ số chung không, và tín hiệu hệ thống đã nêu có đủ để tham gia với rủi ro được kiểm soát không",
    },
}

# Cụm từ gọi chung không nêu tên - trả về cả 3 persona.
_GENERIC_KEYWORDS = ("huyền thoại", "đại sư", "các bậc thầy")


def detect_personas(text: str) -> list[str]:
    """Nhận diện persona được nêu trong tin nhắn (kể cả viết không dấu).
    Trả về [] nếu không phải câu hỏi persona."""
    lower = text.lower()
    matched = [pid for pid, p in _PERSONAS.items() if any(kw in lower for kw in p["keywords"])]
    if matched:
        return matched
    if any(kw in lower for kw in _GENERIC_KEYWORDS):
        return list(_PERSONAS)
    return []


def persona_names(persona_ids: list[str]) -> list[str]:
    return [_PERSONAS[pid]["name"] for pid in persona_ids]


_PROMPT_CONSTRAINTS = """[RÀNG BUỘC BẮT BUỘC TỪ HỆ THỐNG]
- Mọi con số trong [DỮ LIỆU HỆ THỐNG] là số liệu thật đã qua gate định lượng - chỉ được nhắc lại khi bình luận, KHÔNG được tự suy ra con số mới.
- TUYỆT ĐỐI không đưa giá mua/giá mục tiêu/giá cắt lỗ, không khuyến nghị mua/bán kèm con số. Nhân vật chỉ nói quan sát định tính về doanh nghiệp và giá.
- Mỗi nhân vật 3-6 câu, tiếng Việt, không tự giới thiệu, không chào hỏi, không nhắc rằng mình là AI hay đang đóng vai."""


def build_persona_prompt(symbol: str, persona_ids: list[str], data_text: str, user_text: str = "") -> str:
    blocks = "\n\n".join(
        f"[NHÂN VẬT: {_PERSONAS[pid]['name']}]\n"
        f"{_PERSONAS[pid]['identity']}\n"
        f"Điểm nhân vật sẽ tự hỏi về {symbol}: {_PERSONAS[pid]['lens']}"
        for pid in persona_ids
    )
    question = user_text.strip() or f"Các nhân vật trên sẽ nhìn mã {symbol} thế nào?"
    return (
        f"{_PROMPT_CONSTRAINTS}\n\n{blocks}\n\n"
        f"[DỮ LIỆU HỆ THỐNG CHO {symbol}]:\n{data_text}\n\n"
        f"[CÂU HỎI TỪ NGƯỜI DÙNG]:\n\"{question}\"\n\n"
        "Hãy trả lời theo từng nhân vật, mỗi phần mở đầu bằng tên nhân vật in đậm. "
        "Nếu nhân vật sẽ không đồng ý với tín hiệu hệ thống, nói thẳng vì sao - đây "
        "là góc nhìn bổ sung, không phải thay thế."
    )
