"""Nhận diện bài Zalo trùng lặp trước khi đưa vào hàng chờ Facebook.

So trên TOÀN BỘ nhóm nguồn Facebook của cùng tài khoản Zalo, không chỉ trong một
nhóm: một deal được chia sẻ ở nhiều nhóm chỉ vào hàng chờ một lần, các lần sau được
ghi nhận là "đăng lại" kèm nhóm (facebook_post_fingerprints.dup_groups).

Mỗi bài có một "dấu vân tay" nhỏ (vài trăm byte) lưu ở bảng
``facebook_post_fingerprints``. Bảng này giữ lại kể cả khi bài/ảnh đã bị dọn để
tiết kiệm dung lượng Supabase, nên bài cũ đã xoá vẫn chặn được bản đăng lại.

Một bài bị coi là trùng với bài trước (trong ``FACEBOOK_DEDUP_DAYS`` ngày) khi:
1. cùng nội dung chữ sau chuẩn hoá (bỏ dấu, bỏ link, bỏ ký tự đặc biệt);
2. cùng sản phẩm Shopee (cùng shop/item id, hoặc cùng short-link);
3. chữ gần giống (Jaccard cặp từ liên tiếp >= 0,75) VÀ cùng giá;
4. bài này "nằm gọn" trong bài kia hoặc ngược lại (>= 85% cặp từ của bài ngắn có
   trong bài dài - kiểu đăng lại ở nhóm khác thêm/bớt vài dòng chào hỏi, hashtag,
   lời mời vào nhóm) VÀ giá của bài này nằm trong giá của bài kia;
5. ảnh giống (dHash lệch <= 8 bit, chịu được ảnh bị Zalo nén lại khi chuyển tiếp)
   VÀ chữ khá giống (Jaccard >= 0,4 hoặc nằm gọn >= 60%);
6. cùng người gửi đăng lại (thường ở nhóm khác): ảnh trùng, cùng giá, chữ hơi giống;
7. mọi ảnh (từ 2 ảnh trở lên) đều trùng ảnh của cùng một bài cũ.
Ảnh giống một mình không đủ: người bán hay dùng chung banner/logo cho nhiều bài; cùng
mẫu chữ nhưng khác giá là deal mới.

"Giá" là các số tiền đọc được trong bài ("159k", "1tr2", "1.299.000", "199.000đ");
hai bài đều không có số tiền nào thì so toàn bộ con số như trước.

Tốc độ: đặc trưng của bài mới tính 1 lần; với mỗi dòng cũ chỉ dò cặp từ của nó trong
tập của bài mới (không dựng tập riêng), con số/số tiền chỉ tính khi một luật cần tới -
nhanh gấp ~4 lần bản trước với cùng số dòng. Code thuần Python, không I/O:
channels/facebook_repository.py gọi qua ``asyncio.to_thread`` để không chặn event loop.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Container, Iterable, Mapping
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any
from urllib.parse import urlparse

from services.facebook_caption import _fold, _numbers, _without_links, find_shopee_urls

_ITEM_RE = re.compile(r"-i\.(\d+)\.(\d+)|/product/(\d+)/(\d+)", re.IGNORECASE)
_NON_WORD_RE = re.compile(r"[^a-z0-9]+")
_MIN_HASHED_CHARS = 20
_MIN_SHINGLE_WORDS = 8
# Luật "nằm gọn" chỉ xét khi bài ngắn hơn có từ chừng này cặp từ: câu chào ngắn
# chung chung ("hàng mới về nha cả nhà") nằm gọn trong rất nhiều bài khác nhau.
_MIN_CONTAINMENT_SHINGLES = 12
NEAR_TEXT_THRESHOLD = 0.75
CONTAINMENT_THRESHOLD = 0.85
IMAGE_TEXT_THRESHOLD = 0.4
IMAGE_CONTAINMENT_THRESHOLD = 0.6
SAME_SENDER_TEXT_THRESHOLD = 0.2
SAME_SENDER_CONTAINMENT_THRESHOLD = 0.4
# Ảnh trùng hẳn (luật 6, 7) và ảnh gần giống đi kèm chữ giống (luật 5).
IMAGE_HAMMING_MAX = 6
IMAGE_HAMMING_LOOSE = 8
_STORED_TEXT_CHARS = 1500

REASON_TEXT = "trùng nội dung"
REASON_PRODUCT = "trùng sản phẩm/link Shopee"
REASON_NEAR = "nội dung gần giống"
REASON_CONTAINED = "đăng lại có thêm/bớt vài dòng"
REASON_IMAGE_TEXT = "ảnh trùng và nội dung giống"
REASON_SAME_SENDER = "cùng người đăng lại (ảnh + giá trùng)"
REASON_ALL_IMAGES = "toàn bộ ảnh trùng"

# Số tiền trên chữ đã chuẩn hoá (normalize_text: không dấu, chỉ còn chữ/số và
# khoảng trắng - "1.299.000đ" thành "1 299 000d", "159k" giữ nguyên).
# "1tr2" = 1.200.000, "2tr500" = 2.500.000; "1 triệu 2 màu" chỉ lấy 1.000.000.
_PRICE_TR_RE = re.compile(r"\b(\d{1,3}) ?(?:tr|trieu)(\d{1,3})?\b")
_PRICE_K_RE = re.compile(r"\b(\d{1,4}) ?k\b")
_PRICE_GROUPED_RE = re.compile(r"\b(\d{1,3}(?: \d{3})+) ?(?:d|vnd|dong)?\b")
_PRICE_UNIT_RE = re.compile(r"\b(\d{4,9}) ?(?:d|vnd|dong)\b")


def dedup_days() -> int:
    try:
        return max(1, int(os.getenv("FACEBOOK_DEDUP_DAYS", "7")))
    except ValueError:
        return 7


def scan_limit() -> int:
    """Số dấu vân tay gần nhất được so "gần giống" (luật 3-7). Luật 1-2 (trùng chính
    xác) tra bằng index trên toàn cửa sổ FACEBOOK_DEDUP_DAYS, không bị giới hạn này."""
    try:
        return min(5000, max(50, int(os.getenv("FACEBOOK_DEDUP_SCAN_LIMIT", "800"))))
    except ValueError:
        return 800


@dataclass(frozen=True)
class Fingerprint:
    text_hash: str | None
    product_keys: list[str] = field(default_factory=list)
    image_hashes: list[int] = field(default_factory=list)
    folded_text: str = ""
    group_id: str = ""
    sender_id: str = ""


@dataclass(frozen=True)
class DuplicateMatch:
    post_id: int
    reason: str
    # Nhóm Zalo của bài gốc (None: dấu vân tay cũ, lưu trước khi ghi nhóm).
    group_id: str | None = None
    # Bài gốc ở nhóm KHÁC nhóm của bài mới.
    cross_group: bool = False


def normalize_text(text: str) -> str:
    """Chữ thường, không dấu, không link, chỉ còn chữ/số cách nhau 1 khoảng trắng."""
    return " ".join(_NON_WORD_RE.sub(" ", _fold(_without_links(text))).split())


def product_keys(text: str) -> list[str]:
    keys: list[str] = []
    for url in find_shopee_urls(text):
        match = _ITEM_RE.search(url)
        if match:
            shop, item = (match.group(1), match.group(2)) if match.group(1) else match.group(3, 4)
            keys.append(f"item:{shop}:{item}")
            continue
        try:
            parsed = urlparse(url)
        except ValueError:
            continue
        path = parsed.path.rstrip("/")
        if path:
            # Short-link phân biệt hoa/thường (s.shopee.vn/LnrWhdCWK) nên giữ nguyên path.
            keys.append(f"url:{(parsed.hostname or '').lower()}{path}")
    return sorted(set(keys))


def build_fingerprint(
    text: str,
    image_hashes: list[int] | None = None,
    *,
    group_id: str = "",
    sender_id: str = "",
) -> Fingerprint:
    folded = normalize_text(text)
    text_hash = (
        hashlib.sha256(folded.encode()).hexdigest() if len(folded) >= _MIN_HASHED_CHARS else None
    )
    return Fingerprint(
        text_hash=text_hash,
        product_keys=product_keys(text),
        image_hashes=list(image_hashes or []),
        folded_text=folded[:_STORED_TEXT_CHARS],
        group_id=group_id,
        sender_id=sender_id,
    )


def price_keys(folded: str) -> frozenset[int]:
    """Các số tiền (đồng) trong chữ đã chuẩn hoá: "159k" -> 159000, "1tr2" -> 1200000,
    "1 299 000" -> 1299000, "199000d" -> 199000. Bỏ 0 ("đơn 0đ")."""
    found: set[int] = set()
    for match in _PRICE_TR_RE.finditer(folded):
        rest = match.group(2) or ""
        found.add(int(match.group(1)) * 1_000_000 + int(rest.ljust(3, "0")) * 1_000)
    for match in _PRICE_K_RE.finditer(folded):
        found.add(int(match.group(1)) * 1_000)
    for match in _PRICE_GROUPED_RE.finditer(folded):
        found.add(int(match.group(1).replace(" ", "")))
    for match in _PRICE_UNIT_RE.finditer(folded):
        found.add(int(match.group(1)))
    found.discard(0)
    return frozenset(found)


class _Text:
    """Chữ đã chuẩn hoá của một bài kèm các đặc trưng tính lười (đa số dòng cũ bị loại
    trước khi cần tới con số/số tiền)."""

    __slots__ = ("folded", "_words", "_shingles", "_unique", "_numbers", "_prices")

    def __init__(self, folded: str):
        self.folded = folded
        self._words: list[str] | None = None
        self._shingles: frozenset[tuple[str, str]] | None = None
        self._unique: int | None = None
        self._numbers: frozenset[str] | None = None
        self._prices: frozenset[int] | None = None

    @property
    def words(self) -> list[str]:
        if self._words is None:
            self._words = self.folded.split()
        return self._words

    def pairs(self):
        """Cặp từ liên tiếp (rỗng khi dưới _MIN_SHINGLE_WORDS từ)."""
        words = self.words
        return pairwise(words) if len(words) >= _MIN_SHINGLE_WORDS else iter(())

    @property
    def shingles(self) -> frozenset[tuple[str, str]]:
        if self._shingles is None:
            self._shingles = frozenset(self.pairs())
        return self._shingles

    @property
    def unique_pairs(self) -> int:
        """Số cặp từ KHÁC nhau - không dựng tập khi đã có sẵn."""
        if self._unique is None:
            self._unique = len(self._shingles if self._shingles is not None else set(self.pairs()))
        return self._unique

    @property
    def numbers(self) -> frozenset[str]:
        if self._numbers is None:
            self._numbers = frozenset(_numbers(self.folded))
        return self._numbers

    @property
    def prices(self) -> frozenset[int]:
        if self._prices is None:
            self._prices = price_keys(self.folded)
        return self._prices


# Ngưỡng thấp nhất của các luật: dòng cũ có ít cặp từ chung hơn mức này thì không luật
# nào qua được, khỏi đếm chính xác số cặp từ của nó (tiết kiệm ~1/2 thời gian quét).
_MIN_JACCARD = min(NEAR_TEXT_THRESHOLD, IMAGE_TEXT_THRESHOLD, SAME_SENDER_TEXT_THRESHOLD)
_MIN_CONTAINED = min(
    CONTAINMENT_THRESHOLD, IMAGE_CONTAINMENT_THRESHOLD, SAME_SENDER_CONTAINMENT_THRESHOLD
)


def _overlap(probe: _Text, other: _Text) -> tuple[float, float]:
    """(Jaccard, mức "nằm gọn") cặp từ của bài mới ``probe`` với bài ``other``.

    "Nằm gọn" = phần cặp từ của bài NGẮN hơn có trong bài kia; chỉ tính khi bài ngắn
    có đủ _MIN_CONTAINMENT_SHINGLES cặp. Đếm phần chung bằng cách dò cặp từ của
    ``other`` trong tập của ``probe`` (không dựng tập cho từng dòng cũ)."""
    mine = probe.shingles
    if not mine:
        return 0.0, 0.0
    common = len(mine.intersection(other.pairs()))
    if not common:
        return 0.0, 0.0
    if common < len(mine) * _MIN_JACCARD and common < _MIN_CONTAINED * _MIN_CONTAINMENT_SHINGLES:
        # Jaccard <= common/|mine| < ngưỡng thấp nhất; "nằm gọn" >= ngưỡng thấp nhất
        # cần bài ngắn < _MIN_CONTAINMENT_SHINGLES cặp - không đủ điều kiện xét.
        return 0.0, 0.0
    theirs = other.unique_pairs
    smaller = min(len(mine), theirs)
    contained = common / smaller if smaller >= _MIN_CONTAINMENT_SHINGLES else 0.0
    return common / (len(mine) + theirs - common), contained


def text_similarity(a: str, b: str) -> float:
    """Jaccard cặp từ liên tiếp của hai chữ đã chuẩn hoá (0 khi một bên dưới 8 từ)."""
    return _overlap(_Text(a), _Text(b))[0]


def _subset(a: frozenset, b: frozenset) -> bool:
    small, big = (a, b) if len(a) <= len(b) else (b, a)
    return small <= big


def _same_deal(a: _Text, b: _Text, *, allow_subset: bool) -> bool:
    """Cùng một deal: cùng giá (hoặc giá bài này nằm trong giá bài kia khi
    ``allow_subset``); hai bài đều không có số tiền thì so mọi con số."""
    if a.prices or b.prices:
        if a.prices == b.prices:
            return True
        return allow_subset and bool(a.prices) and bool(b.prices) and _subset(a.prices, b.prices)
    if a.numbers == b.numbers:
        return True
    return allow_subset and _subset(a.numbers, b.numbers)


def _hamming(a: int, b: int) -> int:
    return ((a ^ b) & 0xFFFFFFFFFFFFFFFF).bit_count()


def _matching_images(
    candidate: list[int], previous: list[int], max_bits: int = IMAGE_HAMMING_MAX
) -> int:
    return sum(1 for h in candidate if any(_hamming(h, p) <= max_bits for p in previous))


def _row_value(row: Mapping[str, Any], key: str) -> Any:
    """Cột có thể chưa có ở dòng cũ/bản giả trong test (asyncpg.Record và dict đều có get)."""
    getter = getattr(row, "get", None)
    return getter(key) if getter is not None else None


class DedupIndex:
    """Các dòng ``facebook_post_fingerprints`` (mới nhất trước) để so một hoặc nhiều
    bài mới. Chữ của từng dòng chỉ tách khi cần rồi nhớ lại trong index."""

    def __init__(self, rows: Iterable[Mapping[str, Any]]):
        self._rows = list(rows)
        self._texts: dict[int, _Text] = {}

    def __len__(self) -> int:
        return len(self._rows)

    def _text(self, index: int) -> _Text:
        text = self._texts.get(index)
        if text is None:
            text = self._texts[index] = _Text(self._rows[index]["folded_text"] or "")
        return text

    def find(
        self,
        candidate: Fingerprint,
        *,
        before_post_id: int | None = None,
        exclude: Container[int] = (),
    ) -> DuplicateMatch | None:
        """Bài cũ đầu tiên (theo thứ tự dòng) trùng ``candidate``; ``before_post_id``
        chỉ xét bài có id nhỏ hơn (so hàng chờ: bài sau trùng bài trước)."""
        probe = _Text(candidate.folded_text)
        products = set(candidate.product_keys)
        for index, row in enumerate(self._rows):
            post_id = int(row["post_id"])
            if before_post_id is not None and post_id >= before_post_id:
                continue
            if post_id in exclude:
                continue
            reason = self._reason(candidate, probe, products, index, row)
            if reason is None:
                continue
            group = _row_value(row, "group_id") or None
            return DuplicateMatch(
                post_id,
                reason,
                group,
                cross_group=bool(group and candidate.group_id and group != candidate.group_id),
            )
        return None

    def _reason(
        self,
        candidate: Fingerprint,
        probe: _Text,
        products: set[str],
        index: int,
        row: Mapping[str, Any],
    ) -> str | None:
        if candidate.text_hash and candidate.text_hash == row["text_hash"]:
            return REASON_TEXT
        if products and products.intersection(row["product_keys"] or ()):
            return REASON_PRODUCT
        previous = self._text(index)
        jaccard, contained = _overlap(probe, previous)
        if jaccard >= NEAR_TEXT_THRESHOLD and _same_deal(probe, previous, allow_subset=False):
            return REASON_NEAR
        if contained >= CONTAINMENT_THRESHOLD and _same_deal(probe, previous, allow_subset=True):
            return REASON_CONTAINED
        previous_images = list(row["image_hashes"] or [])
        if not candidate.image_hashes or not previous_images:
            return None
        if _matching_images(candidate.image_hashes, previous_images, IMAGE_HAMMING_LOOSE) and (
            jaccard >= IMAGE_TEXT_THRESHOLD or contained >= IMAGE_CONTAINMENT_THRESHOLD
        ):
            return REASON_IMAGE_TEXT
        matched = _matching_images(candidate.image_hashes, previous_images, IMAGE_HAMMING_MAX)
        if (
            matched
            and candidate.sender_id
            and _row_value(row, "sender_id") == candidate.sender_id
            and (
                jaccard >= SAME_SENDER_TEXT_THRESHOLD
                or contained >= SAME_SENDER_CONTAINMENT_THRESHOLD
            )
            and _same_deal(probe, previous, allow_subset=True)
        ):
            return REASON_SAME_SENDER
        if matched == len(candidate.image_hashes) >= 2:
            return REASON_ALL_IMAGES
        return None


def find_duplicate(
    candidate: Fingerprint, previous_rows: Iterable[Mapping[str, Any]]
) -> DuplicateMatch | None:
    """``previous_rows``: các dòng facebook_post_fingerprints (mới nhất trước)."""
    return DedupIndex(previous_rows).find(candidate)
