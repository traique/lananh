export const ZALO_CHUNK_LIMIT = 1800;

/** Cắt text dài thành nhiều tin nhắn Zalo, ưu tiên cắt ở đoạn/dòng/khoảng trắng. */
export function splitForZalo(text: string, limit = ZALO_CHUNK_LIMIT): string[] {
  const out: string[] = [];
  let rest = (text || "").trim();
  while (rest.length > limit) {
    let cut = rest.lastIndexOf("\n\n", limit);
    if (cut < limit / 2) cut = rest.lastIndexOf("\n", limit);
    if (cut < limit / 2) cut = rest.lastIndexOf(" ", limit);
    if (cut <= 0) cut = limit;
    out.push(rest.slice(0, cut).trim());
    rest = rest.slice(cut).trim();
  }
  if (rest) out.push(rest);
  return out.length ? out : [text];
}

export function errorCode(error: unknown): string | undefined {
  const e = error as { code?: unknown; cause?: { code?: unknown } } | null;
  const code = e?.cause?.code ?? e?.code;
  return typeof code === "string" ? code : undefined;
}

export function isTransientNetworkError(error: unknown): boolean {
  const code = errorCode(error);
  const message = String((error as { message?: unknown } | null)?.message ?? error);
  return (
    code === "ECONNRESET" ||
    code === "ETIMEDOUT" ||
    code === "EPIPE" ||
    code === "ECONNREFUSED" ||
    message.includes("fetch failed")
  );
}

export function isConnectionRefused(error: unknown): boolean {
  const cause = (error as { cause?: unknown } | null)?.cause;
  return errorCode(error) === "ECONNREFUSED" || String(cause ?? error).includes("ECONNREFUSED");
}

export function errorMessage(error: unknown, fallback = "lỗi không xác định"): string {
  return error instanceof Error ? error.message : fallback;
}

/**
 * Câu báo lỗi gửi người dùng Zalo. Chỉ chủ bot (controller) thấy thêm chi tiết
 * kỹ thuật để khỏi mò log; người khác nhận đúng câu cố định - thông điệp lỗi có
 * thể mang thân response của Python, chữ timeout tiếng Anh hay host CDN Zalo.
 */
export function userErrorReply(
  error: unknown,
  recipient: string,
  controllerId: string,
  publicText: string,
): string {
  if (!controllerId || recipient !== controllerId) return publicText;
  return `${publicText}\n🔎 ${errorMessage(error, String(error))}`;
}

export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Bộ nhớ msgId đã xử lý, giới hạn kích thước để không phình RAM. */
/**
 * Người gửi đã được Python trả lời (đã pair) - LRU có trần. Chỉ những người này
 * mới nhận câu "bot đang bận/gặp sự cố" từ gateway; người lạ chưa pair vẫn bị
 * im lặng như phía Python (không lộ ra đây là bot). Sau khi gateway khởi động
 * lại, danh sách trống cho tới khi Python trả lời lại người đó lần đầu.
 */
export class KnownSenders {
  private readonly ids = new Set<string>();
  constructor(private readonly capacity = 2000) {}

  mark(id: string): void {
    if (!id) return;
    // Xoá rồi thêm lại để id vừa dùng nằm cuối (LRU theo thứ tự chèn của Set).
    this.ids.delete(id);
    this.ids.add(id);
    if (this.ids.size > this.capacity) {
      const oldest = this.ids.values().next().value;
      if (oldest !== undefined) this.ids.delete(oldest);
    }
  }

  has(id: string): boolean {
    return this.ids.has(id);
  }

  get size(): number {
    return this.ids.size;
  }
}

export class SeenIds {
  private readonly ids = new Set<string>();
  constructor(private readonly capacity = 5000) {}

  /** Trả về true nếu id MỚI (lần đầu thấy). */
  remember(id: string): boolean {
    if (this.ids.has(id)) return false;
    this.ids.add(id);
    if (this.ids.size > this.capacity) {
      const oldest = this.ids.values().next().value;
      if (oldest !== undefined) this.ids.delete(oldest);
    }
    return true;
  }
}
