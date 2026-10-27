/**
 * Bộ đếm hoạt động của gateway từ lúc khởi động (trả qua control server
 * GET /status -> "stats", Python hiện trên /admin). Tập khoá cố định: không
 * phình RAM theo người gửi/tin nhắn.
 */
export const STAT_KEYS = [
  /** Tin riêng nhận được (đã lọc trùng, bỏ sticker). */
  "direct_messages",
  /** Lượt chữ gửi sang Python qua hàng đợi theo người gửi. */
  "text_turns",
  /** Tin chữ được gộp vào lượt của tin khác (debounce + gộp lúc tới lượt). */
  "merged_messages",
  /** Lệnh đi làn nhanh (/huy, lệnh chỉ đọc) ngoài hàng đợi. */
  "fast_lane",
  /** /huy không gửi Python: người đó đã có /huy đang chờ, hoặc quá trần làn nhanh. */
  "fast_lane_dropped",
  "image_turns",
  "voice_turns",
  "file_turns",
  /** Câu báo "chưa hỗ trợ"/không tải được media gửi người dùng. */
  "notices",
  /** Tin bị từ chối vì hàng đợi đầy (đã báo bận nếu là người quen). */
  "busy_rejections",
  "chunks_sent",
  /** Zalo từ chối tin có định dạng -> gửi lại chữ thuần. */
  "plain_fallbacks",
  "send_failures",
  "typing_events",
  "seen_events",
  "reactions",
  /** Lỗi typing/seen/reaction (đã bỏ qua). */
  "presence_errors",
  "outbox_sent",
  "outbox_failed",
  /** Lần outbox hết ngân sách gửi theo giờ và hoãn tin sang lượt sau. */
  "outbox_deferred",
  /** Tin thoại trả lời /noi đã gửi (voice.ts) / gửi không được. */
  "voice_replies",
  "voice_reply_failures",
] as const;

export type StatKey = (typeof STAT_KEYS)[number];

export class GatewayStats {
  private readonly counts = new Map<StatKey, number>();
  readonly startedAt = Date.now();

  inc(key: StatKey, by = 1): void {
    this.counts.set(key, (this.counts.get(key) ?? 0) + by);
  }

  get(key: StatKey): number {
    return this.counts.get(key) ?? 0;
  }

  snapshot(): Record<StatKey, number> & { since: string } {
    const out = { since: new Date(this.startedAt).toISOString() } as Record<StatKey, number> & {
      since: string;
    };
    for (const key of STAT_KEYS) out[key] = this.get(key);
    return out;
  }
}
