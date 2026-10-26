export type GatewayConfig = {
  cookie?: unknown;
  imei?: string;
  userAgent: string;
  controllerId: string;
  accountId: string;
  bridgeBaseUrl: string;
  bridgeSecret: string;
  groupRefreshMs: number;
  outboxPollMs: number;
  controlPort: number;
  facebookMergeWindowMs: number;
  directConcurrency: number;
  directPerSenderBacklog: number;
  /** Gộp tin chữ liên tiếp: chờ bao lâu sau tin cuối (0 = tắt debounce). */
  directDebounceMs: number;
  /** Trần chờ debounce tính từ tin đầu của lô. */
  directDebounceMaxMs: number;
  /** Đủ chừng này tin chữ trong lúc debounce thì gửi luôn. */
  directDebounceMaxMessages: number;
  /** Khoảng nghỉ ngẫu nhiên giữa các đoạn trả lời / tin outbox (0 và 0 = tắt). */
  sendGapMinMs: number;
  sendGapMaxMs: number;
  /** Ngân sách tin outbox mỗi giờ của tài khoản; vượt thì hoãn, không bỏ (0 = không giới hạn). */
  outboxMaxPerHour: number;
  /** Typing, "đã xem" và reaction khi tin phải chờ (false = tắt hết). */
  presenceEvents: boolean;
  /** Trần tin thoại tải từ Zalo (khớp VOICE_MAX_BYTES bên Python). */
  voiceMaxBytes: number;
  /** Trần file tài liệu tải từ Zalo (khớp DOCUMENT_MAX_BYTES bên Python). */
  documentMaxBytes: number;
};
function required(n: string) {
  const v = process.env[n]?.trim();
  if (!v) throw new Error(`Missing required environment variable: ${n}`);
  return v;
}
function number(n: string, f: number, min = 1000) {
  const v = Number(process.env[n] || f);
  return Number.isFinite(v) ? Math.max(min, v) : f;
}
/** Như number() nhưng 0 là giá trị hợp lệ (tắt tính năng); rỗng/hỏng -> mặc định. */
function zeroOrMore(n: string, f: number, max: number) {
  const raw = process.env[n]?.trim();
  const v = raw ? Number(raw) : f;
  return Number.isFinite(v) ? Math.floor(Math.min(max, Math.max(0, v))) : f;
}
function flag(n: string, f: boolean) {
  const raw = process.env[n]?.trim().toLowerCase();
  if (!raw) return f;
  return ["1", "true", "yes", "on"].includes(raw);
}
/** Trần byte media dùng chung biến với Python (64 KiB - 8 MiB, mặc định 5 MiB). */
function mediaBytes(n: string) {
  const v = Number(process.env[n]?.trim() || 5 * 1024 * 1024);
  return Number.isFinite(v)
    ? Math.floor(Math.min(8 * 1024 * 1024, Math.max(64 * 1024, v)))
    : 5 * 1024 * 1024;
}
export function loadConfig(): GatewayConfig {
  let cookie: unknown;
  const raw = process.env.ZALO_COOKIE_JSON?.trim();
  if (raw)
    try {
      cookie = JSON.parse(raw);
    } catch {
      throw new Error("ZALO_COOKIE_JSON must be valid JSON");
    }
  const base =
    process.env.ZALO_BRIDGE_BASE_URL?.trim() ||
    `http://127.0.0.1:${process.env.PORT || "8000"}/internal/zalo`;
  return {
    cookie,
    imei: process.env.ZALO_IMEI?.trim(),
    userAgent:
      process.env.ZALO_USER_AGENT?.trim() ||
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/145.0.0.0 Safari/537.36",
    controllerId: process.env.ZALO_CONTROLLER_ID?.trim() || "",
    accountId: process.env.ZALO_BOT_ACCOUNT_ID?.trim() || "zalo-bot",
    bridgeBaseUrl: base.replace(/\/$/, ""),
    bridgeSecret: required("ZALO_BRIDGE_SECRET"),
    groupRefreshMs: number("ZALO_GROUP_REFRESH_MS", 60000),
    outboxPollMs: number("ZALO_OUTBOX_POLL_MS", 15000),
    controlPort: number("ZALO_CONTROL_PORT", 9901, 1024),
    // Chờ tối đa bao lâu (tính từ tin cuối) để gộp ảnh + caption cùng người gửi.
    facebookMergeWindowMs: Math.min(120000, number("ZALO_FB_MERGE_WINDOW_MS", 30000, 3000)),
    // Số tin riêng xử lý song song (khác người gửi); nên bằng MAX_CONCURRENT_TURNS
    // bên Python. Cùng 1 người gửi luôn tuần tự đúng thứ tự.
    directConcurrency: Math.floor(Math.min(8, number("ZALO_DIRECT_CONCURRENCY", 1, 1))),
    // Số tin tối đa của 1 người đang chờ/đang xử lý; vượt thì báo bận thay vì im lặng bỏ.
    directPerSenderBacklog: Math.floor(Math.min(50, number("ZALO_DIRECT_SENDER_BACKLOG", 8, 1))),
    directDebounceMs: zeroOrMore("ZALO_DIRECT_DEBOUNCE_MS", 2000, 10000),
    directDebounceMaxMs: zeroOrMore("ZALO_DIRECT_DEBOUNCE_MAX_MS", 6000, 30000),
    directDebounceMaxMessages: Math.max(1, zeroOrMore("ZALO_DIRECT_DEBOUNCE_MAX_MESSAGES", 4, 20)),
    sendGapMinMs: zeroOrMore("ZALO_SEND_GAP_MIN_MS", 600, 10000),
    sendGapMaxMs: zeroOrMore("ZALO_SEND_GAP_MAX_MS", 1500, 10000),
    outboxMaxPerHour: zeroOrMore("ZALO_OUTBOX_MAX_PER_HOUR", 60, 10000),
    presenceEvents: flag("ZALO_PRESENCE_EVENTS", true),
    voiceMaxBytes: mediaBytes("VOICE_MAX_BYTES"),
    documentMaxBytes: mediaBytes("DOCUMENT_MAX_BYTES"),
  };
}
