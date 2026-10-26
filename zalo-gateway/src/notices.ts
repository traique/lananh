import type { NoticeKind } from "./bridge.js";
import { errorMessage } from "./text.js";

/**
 * Câu báo dự phòng khi KHÔNG hỏi được Python (POST /notice lỗi/timeout) - chỉ gửi
 * cho người quen. Không gọi "anh"/"chị": gateway không biết cách gọi riêng của
 * từng thành viên (bình thường Python dựng câu theo đúng cách gọi đó).
 */
export const LOCAL_NOTICES: Record<NoticeKind, string> = {
  image: "🖼️ Em nhận được ảnh rồi, đợi em xem xíu nha…",
  voice_failed: "🎙️ Em chưa nghe được tin thoại này, gõ giúp em nha.",
  voice_too_large:
    "🎙️ Tin thoại này dài quá, em chưa nghe hết được. Nhắn ngắn hơn hoặc gõ giúp em nha.",
  file_failed: "📎 Em chưa tải được file này từ Zalo, gửi lại giúp em nha.",
  file_too_large: "📎 File này lớn quá, em chưa đọc được. Gửi file nhỏ hơn giúp em nha.",
  file_unsupported: "📎 Em mới đọc được file PDF, Word (.docx) và TXT thôi nha.",
  video: "🎬 Em chưa xem được video nha. Gửi ảnh chụp màn hình hoặc kể em nghe bằng chữ nhé.",
  location:
    '📍 Em chưa đọc được vị trí gửi qua Zalo nha. Nhắn tên nơi (vd "thời tiết Đà Lạt") là em xem giúp liền.',
};

export type NoticeDeps = {
  /** Python dựng câu (bridge.fetchNotice): null = người lạ/bị khoá; ném = không hỏi được. */
  fetch(sender: string, kind: NoticeKind): Promise<string | null>;
  /** Người gửi chắc chắn đã pair (Python từng trả lời / là chủ bot). */
  isKnown(sender: string): boolean;
  /** Python xác nhận người này đã pair (trả câu báo) - nhớ để lần sau khỏi hỏi. */
  onPaired?(sender: string): void;
  log?(message: string): void;
};

/**
 * Chữ câu báo cho người gửi, hoặc null = im lặng. Python quyết định trước (đúng
 * giọng + cách gọi riêng, người lạ chưa pair -> null); Python lỗi thì chỉ người
 * quen nhận câu trung tính LOCAL_NOTICES - người lạ vẫn không nhận gì.
 */
export async function noticeText(
  sender: string,
  kind: NoticeKind,
  deps: NoticeDeps,
): Promise<string | null> {
  try {
    const text = await deps.fetch(sender, kind);
    if (text) deps.onPaired?.(sender);
    return text;
  } catch (error) {
    (deps.log ?? console.warn)(
      `[zalo] không lấy được câu báo "${kind}" từ Python: ${errorMessage(error, String(error))}`,
    );
    return deps.isKnown(sender) ? LOCAL_NOTICES[kind] : null;
  }
}
