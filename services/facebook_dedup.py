"""Nhận diện bài Zalo trùng lặp trước khi đưa vào hàng chờ Facebook.

Mỗi bài có một "dấu vân tay" nhỏ (vài trăm byte) lưu ở bảng
``facebook_post_fingerprints``. Bảng này giữ lại kể cả khi bài/ảnh đã bị dọn
để tiết kiệm dung lượng Supabase, nên bài cũ đã xoá vẫn chặn được bản đăng lại.

Một bài bị coi là trùng với bài trước (trong ``FACEBOOK_DEDUP_DAYS`` ngày) khi:
- cùng nội dung chữ sau chuẩn hoá (bỏ dấu, bỏ link, bỏ ký tự đặc biệt), hoặc
- cùng sản phẩm Shopee (cùng shop/item id, hoặc cùng short-link), hoặc
- chữ gần giống (Jaccard cặp từ liên tiếp >= 0.75) VÀ cùng bộ con số (giá,
  %, số lượng) - cùng mẫu chữ nhưng khác giá được coi là deal mới, hoặc
- ảnh giống (dHash lệch <= 6 bit) VÀ chữ khá giống (>= 0.4), hoặc mọi ảnh
  (từ 2 ảnh trở lên) đều trùng ảnh của cùng một bài cũ.
Ảnh giống một mình không đủ: người bán hay dùng chung banner/logo cho nhiều bài.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from services.facebook_caption import _fold, _numbers, _without_links, find_shopee_urls

_ITEM_RE = re.compile(r"-i\.(\d+)\.(\d+)|/product/(\d+)/(\d+)", re.IGNORECASE)
_NON_WORD_RE = re.compile(r"[^a-z0-9]+")
_MIN_HASHED_CHARS = 20
_MIN_SHINGLE_WORDS = 8
NEAR_TEXT_THRESHOLD = 0.75
IMAGE_TEXT_THRESHOLD = 0.4
IMAGE_HAMMING_MAX = 6
_STORED_TEXT_CHARS = 1500


def dedup_days() -> int:
    try:
        return max(1, int(os.getenv("FACEBOOK_DEDUP_DAYS", "7")))
    except ValueError:
        return 7


@dataclass(frozen=True)
class Fingerprint:
    text_hash: str | None
    product_keys: list[str] = field(default_factory=list)
    image_hashes: list[int] = field(default_factory=list)
    folded_text: str = ""


@dataclass(frozen=True)
class DuplicateMatch:
    post_id: int
    reason: str


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


def build_fingerprint(text: str, image_hashes: list[int] | None = None) -> Fingerprint:
    folded = normalize_text(text)
    text_hash = (
        hashlib.sha256(folded.encode()).hexdigest() if len(folded) >= _MIN_HASHED_CHARS else None
    )
    return Fingerprint(
        text_hash=text_hash,
        product_keys=product_keys(text),
        image_hashes=list(image_hashes or []),
        folded_text=folded[:_STORED_TEXT_CHARS],
    )


def _shingles(folded: str) -> set[tuple[str, ...]]:
    words = folded.split()
    if len(words) < _MIN_SHINGLE_WORDS:
        return set()
    return {tuple(words[i : i + 2]) for i in range(len(words) - 1)}


def text_similarity(a: str, b: str) -> float:
    sa, sb = _shingles(a), _shingles(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _hamming(a: int, b: int) -> int:
    return ((a ^ b) & 0xFFFFFFFFFFFFFFFF).bit_count()


def _matching_images(candidate: list[int], previous: list[int]) -> int:
    return sum(1 for h in candidate if any(_hamming(h, p) <= IMAGE_HAMMING_MAX for p in previous))


def find_duplicate(candidate: Fingerprint, previous_rows) -> DuplicateMatch | None:
    """``previous_rows``: các dòng facebook_post_fingerprints (mới nhất trước)."""
    for row in previous_rows:
        post_id = int(row["post_id"])
        if candidate.text_hash and candidate.text_hash == row["text_hash"]:
            return DuplicateMatch(post_id, "trùng nội dung")
        if set(candidate.product_keys) & set(row["product_keys"] or []):
            return DuplicateMatch(post_id, "trùng sản phẩm/link Shopee")
        similarity = text_similarity(candidate.folded_text, row["folded_text"] or "")
        if similarity >= NEAR_TEXT_THRESHOLD and _numbers(candidate.folded_text) == _numbers(
            row["folded_text"] or ""
        ):
            return DuplicateMatch(post_id, "nội dung gần giống")
        previous_images = list(row["image_hashes"] or [])
        if candidate.image_hashes and previous_images:
            matched = _matching_images(candidate.image_hashes, previous_images)
            if matched and similarity >= IMAGE_TEXT_THRESHOLD:
                return DuplicateMatch(post_id, "ảnh trùng và nội dung giống")
            if matched == len(candidate.image_hashes) >= 2:
                return DuplicateMatch(post_id, "toàn bộ ảnh trùng")
    return None
