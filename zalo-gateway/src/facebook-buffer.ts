import type { IncomingFacebookPost } from "./bridge.js";
import type { MediaItem } from "./types.js";

const WINDOW_MS = 8000;
const RETRY_MS = 15000;
/** Trần thời gian chờ giữa 2 lần thử lại (backoff nhân đôi từ RETRY_MS). */
const MAX_RETRY_DELAY_MS = 5 * 60 * 1000;
/** Lỗi tạm (5xx/mạng/timeout): thử tối đa chừng này lần (~18 phút) rồi bỏ bài. */
const MAX_TRANSIENT_ATTEMPTS = 8;
/** Lỗi do chính bài (4xx như 400/413): thử lại 1 lần rồi bỏ. */
const MAX_CLIENT_ERROR_ATTEMPTS = 2;
const MAX_MESSAGES_PER_POST = 50;
const MAX_MEDIA_PER_POST = 10;
const MAX_TEXT_CHARS = 20000;
const MAX_BUFFERS = 64;
/** Tổng media giữ trong RAM cho TẤT CẢ bài đang gom (container 512 MB). */
const MAX_BUFFERED_MEDIA_BYTES = 16 * 1024 * 1024;

type PendingPost = {
  groupId: string;
  senderId: string;
  senderName: string;
  messageIds: string[];
  texts: string[];
  media: MediaItem[];
  /** Tin đầu tiên của bài là ảnh hay chữ: cho biết người gửi đăng "ảnh trước" hay "caption trước". */
  firstKind: "media" | "text";
  /** Số lần gửi sang Python đã lỗi. */
  attempts: number;
  timer: NodeJS.Timeout;
};

const SHOPEE_LINK_RE = /(?:shopee\.vn|shope\.ee)\//i;

export type FacebookBufferDeps = {
  store: (post: IncomingFacebookPost) => Promise<void>;
  /** Hàng đợi tuần tự dùng chung với tin nhắn nhóm. */
  enqueue: (work: () => Promise<unknown>) => Promise<void>;
  windowMs?: number;
  retryMs?: number;
  maxRetryDelayMs?: number;
  maxAttempts?: number;
};

const mediaSize = (items: { bytes: Uint8Array }[]) =>
  items.reduce((sum, item) => sum + item.bytes.length, 0);

/**
 * Lỗi do chính nội dung bài (4xx: 400 sai dữ liệu, 413 quá lớn...) - gửi lại y
 * nguyên cũng lỗi tiếp. 408/429 là lỗi tạm thời.
 */
export function isPermanentPostError(error: unknown): boolean {
  const status = (error as { status?: unknown } | null)?.status;
  return (
    typeof status === "number" && status >= 400 && status < 500 && status !== 408 && status !== 429
  );
}

/**
 * Tin mới có mở đầu một bài KHÁC không (thay vì bổ sung cho bài đang gom)?
 * Cần cho cửa sổ gộp dài (30s): người bán hay đăng liên tục nhiều sản phẩm.
 * - Bài đang gom đã đủ ảnh + chữ:
 *   - tin mới có chữ -> caption của bài mới;
 *   - tin mới chỉ có ảnh -> bài mới nếu người gửi theo kiểu "ảnh trước rồi
 *     caption" (bài hiện tại mở đầu bằng ảnh); kiểu "caption trước" thì là ảnh
 *     bổ sung cho bài hiện tại.
 * - Bài đang gom chỉ có chữ chứa link Shopee, tin mới cũng là chữ có link Shopee
 *   -> hai sản phẩm khác nhau.
 */
export function startsNewPost(
  current: { texts: string[]; media: unknown[]; firstKind: "media" | "text" },
  text: string,
  hasMedia: boolean,
): boolean {
  const hasText = current.texts.some((t) => t.trim());
  if (hasText && current.media.length) {
    if (text.trim()) return true;
    return hasMedia && current.firstKind === "media";
  }
  if (hasText && !current.media.length && text.trim() && !hasMedia) {
    return current.texts.some((t) => SHOPEE_LINK_RE.test(t)) && SHOPEE_LINK_RE.test(text);
  }
  return false;
}

/**
 * Gom các tin nhắn liên tiếp của cùng người trong cùng nhóm (trong ~8 giây)
 * thành MỘT bài Facebook chờ duyệt. Lỗi gửi sang Python -> giữ buffer và thử
 * lại có giới hạn: lỗi 4xx thử 1 lần nữa rồi bỏ, lỗi tạm thì backoff tới
 * MAX_TRANSIENT_ATTEMPTS lần. Bộ đệm đầy thì bỏ bài cũ nhất, KHÔNG bao giờ làm
 * mất tin mới vì bài cũ gửi lỗi.
 */
export class FacebookPostBuffer {
  private readonly buffers = new Map<string, PendingPost>();
  private readonly windowMs: number;
  private readonly retryMs: number;
  private readonly maxRetryDelayMs: number;
  private readonly maxAttempts: number;
  private sequence = 0;

  constructor(private readonly deps: FacebookBufferDeps) {
    this.windowMs = deps.windowMs ?? WINDOW_MS;
    this.retryMs = deps.retryMs ?? RETRY_MS;
    this.maxRetryDelayMs = deps.maxRetryDelayMs ?? MAX_RETRY_DELAY_MS;
    this.maxAttempts = Math.max(1, deps.maxAttempts ?? MAX_TRANSIENT_ATTEMPTS);
  }

  get size(): number {
    return this.buffers.size;
  }

  private schedule(key: string, delay: number): NodeJS.Timeout {
    const timer = setTimeout(() => {
      this.deps
        .enqueue(() => this.flush(key))
        .catch((error) => {
          // Hàng đợi nhóm đầy nên chưa gửi được: hẹn lại, không để bài nằm im mãi.
          console.error("[zalo] Facebook buffer flush not queued; retry later", error);
          const pending = this.buffers.get(key);
          if (pending && pending.timer === timer) pending.timer = this.schedule(key, this.retryMs);
        });
    }, delay);
    timer.unref();
    return timer;
  }

  /** Chờ bao lâu trước lần thử kế; null = bỏ bài. */
  private retryDelay(error: unknown, attempts: number): number | null {
    if (isPermanentPostError(error)) {
      return attempts < MAX_CLIENT_ERROR_ATTEMPTS ? this.retryMs : null;
    }
    if (attempts >= this.maxAttempts) return null;
    return Math.min(this.maxRetryDelayMs, this.retryMs * 2 ** (attempts - 1));
  }

  private drop(key: string, reason: string, error?: unknown): void {
    const pending = this.buffers.get(key);
    if (!pending) return;
    clearTimeout(pending.timer);
    this.buffers.delete(key);
    console.error(
      `[zalo] Bỏ bài Facebook nhóm=${pending.groupId} người gửi=${pending.senderId} ` +
        `(${pending.messageIds.length} tin, ${pending.media.length} ảnh): ${reason}`,
      error ?? "",
    );
  }

  /**
   * Gửi bài đang gom sang Python. Không bao giờ ném lỗi: lỗi -> hẹn thử lại
   * hoặc bỏ bài theo loại lỗi (`evicting`: bỏ luôn để lấy chỗ cho tin mới).
   * Trả true nếu bài đã gửi xong (hoặc không còn).
   */
  async flush(key: string, evicting = false): Promise<boolean> {
    const pending = this.buffers.get(key);
    if (!pending) return true;
    clearTimeout(pending.timer);
    try {
      await this.deps.store({
        groupId: pending.groupId,
        messageIds: pending.messageIds,
        senderId: pending.senderId,
        senderName: pending.senderName,
        text: pending.texts.join("\n").trim(),
        media: pending.media,
      });
      if (this.buffers.get(key) === pending) this.buffers.delete(key);
      return true;
    } catch (error) {
      pending.attempts++;
      const delay = this.retryDelay(error, pending.attempts);
      if (evicting) {
        this.drop(key, "bộ đệm đầy và gửi lỗi", error);
      } else if (delay === null) {
        this.drop(key, `gửi lỗi ${pending.attempts} lần`, error);
      } else {
        console.error(
          `[zalo] Facebook post flush failed (lần ${pending.attempts}); thử lại sau ${delay}ms`,
          error,
        );
        pending.timer = this.schedule(key, delay);
      }
      return false;
    }
  }

  private totalBufferedBytes(): number {
    let total = 0;
    for (const pending of this.buffers.values()) total += mediaSize(pending.media);
    return total;
  }

  async add(
    groupId: string,
    senderId: string,
    senderName: string,
    messageId: string,
    text: string,
    media: MediaItem[],
  ): Promise<void> {
    const key = `${groupId}:${senderId}`;
    const current = this.buffers.get(key);
    if (
      current &&
      (startsNewPost(current, text, media.length > 0) ||
        current.messageIds.length >= MAX_MESSAGES_PER_POST ||
        current.media.length + media.length > MAX_MEDIA_PER_POST ||
        mediaSize(current.media) + mediaSize(media) > MAX_BUFFERED_MEDIA_BYTES ||
        current.texts.join("\n").length + text.length + 1 > MAX_TEXT_CHARS)
    ) {
      // Tách bài cũ sang khoá riêng rồi mới gửi: nếu gửi lỗi, bài cũ vẫn được
      // giữ và thử lại, còn tin mới vẫn mở bài mới thay vì bị mất.
      const detachedKey = `${key}#${++this.sequence}`;
      this.buffers.delete(key);
      this.buffers.set(detachedKey, current);
      await this.flush(detachedKey);
    }
    // Giữ RAM trong giới hạn: đẩy bài cũ nhất đi trước khi nhận thêm. Gửi lỗi
    // thì bỏ luôn bài cũ đó (đã log) - tin mới không bao giờ bị mất vì bài cũ.
    while (
      this.buffers.size >= MAX_BUFFERS ||
      this.totalBufferedBytes() + mediaSize(media) > MAX_BUFFERED_MEDIA_BYTES
    ) {
      const oldest = this.buffers.keys().next().value;
      if (oldest === undefined) break;
      await this.flush(oldest, true);
    }
    const existing = this.buffers.get(key);
    const clippedText = text.slice(0, MAX_TEXT_CHARS);
    if (existing) {
      clearTimeout(existing.timer);
      existing.messageIds.push(messageId);
      if (text) existing.texts.push(clippedText);
      existing.media.push(...media);
      existing.timer = this.schedule(key, this.windowMs);
      return;
    }
    this.buffers.set(key, {
      groupId,
      senderId,
      senderName,
      messageIds: [messageId],
      texts: text ? [clippedText] : [],
      media: [...media],
      firstKind: media.length && !text.trim() ? "media" : "text",
      attempts: 0,
      timer: this.schedule(key, this.windowMs),
    });
  }
}
