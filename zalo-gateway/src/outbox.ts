import type { OutboxItem } from "./bridge.js";
import { errorMessage, isTransientNetworkError, splitForZalo } from "./text.js";

export type OutboxDeps = {
  fetch(): Promise<OutboxItem[]>;
  sendChunk(recipientId: string, chunk: string): Promise<void>;
  ack(id: number): Promise<void>;
  /** Báo Python tin gửi lỗi sau khi đã gửi được `chunksSent` đoạn đầu. */
  fail(id: number, error: string, chunksSent: number): Promise<void>;
  /** Zalo còn đăng nhập không (mất kết nối giữa chừng không phải lỗi của tin). */
  connected(): boolean;
  log?(message: string, error?: unknown): void;
  /** Nghỉ giữa 2 lần gửi liên tiếp (giữa các đoạn và giữa các tin) - nhịp không giống máy. */
  pace?(): Promise<void>;
  /**
   * Lấy 1 lượt từ ngân sách gửi của tài khoản trước khi gửi 1 tin; false = hết
   * ngân sách: tin còn lại HOÃN sang lượt poll sau (vẫn chờ trong DB), không bỏ.
   */
  takeBudget?(): boolean;
};

/** `deferred` chỉ có khi hết ngân sách gửi: số tin để dành cho lượt sau. */
export type OutboxRunResult = { sent: number; failed: number; deferred?: number };

const MAX_TRACKED_ITEMS = 256;
const MAX_ERROR_CHARS = 300;

/**
 * Gửi outbox (tóm tắt nhóm, nhắc việc, bản xem trước bài Facebook...) từng tin
 * một, lỗi tin nào xử lý tin đó:
 * - tin lỗi được báo về Python (tăng số lần thử + backoff, quá N lần thì bỏ),
 *   các tin của người nhận KHÁC vẫn gửi tiếp - không còn chặn đầu hàng đợi;
 * - tin sau của CÙNG người nhận chờ lượt sau để giữ đúng thứ tự;
 * - tin dài gửi dở: lần sau gửi tiếp từ đoạn kế (Python lưu `chunks_sent`, gateway
 *   nhớ thêm trong RAM phòng khi không báo/ack được về Python).
 */
export class OutboxDelivery {
  /** id tin -> số đoạn đã gửi mà Python có thể chưa ghi nhận. Có trần. */
  private readonly progress = new Map<number, number>();

  constructor(
    private readonly deps: OutboxDeps,
    private readonly maxTracked = MAX_TRACKED_ITEMS,
  ) {}

  get tracked(): number {
    return this.progress.size;
  }

  async runOnce(): Promise<OutboxRunResult> {
    const result: OutboxRunResult = { sent: 0, failed: 0 };
    if (!this.deps.connected()) return result;
    const items = await this.deps.fetch();
    const blocked = new Set<string>();
    let sentAny = false;
    for (let index = 0; index < items.length; index++) {
      const item = items[index];
      if (!this.deps.connected()) break;
      if (blocked.has(item.recipient_id)) continue;
      const chunks = splitForZalo(item.content);
      let done = Math.max(Number(item.chunks_sent) || 0, this.progress.get(item.id) ?? 0);
      if (done < chunks.length && this.deps.takeBudget && !this.deps.takeBudget()) {
        // Hết ngân sách: tin này và các tin sau chờ lượt poll kế (không ack, không báo lỗi).
        result.deferred = items.length - index;
        break;
      }
      try {
        while (done < chunks.length) {
          const chunk = chunks[done];
          if (chunk.trim()) {
            if (sentAny && this.deps.pace) await this.deps.pace();
            await this.deps.sendChunk(item.recipient_id, chunk);
            sentAny = true;
          }
          done++;
          this.remember(item.id, done);
        }
      } catch (error) {
        blocked.add(item.recipient_id);
        // Mất đăng nhập Zalo giữa chừng: không tính lượt thử cho tin, chờ kết nối lại.
        if (!this.deps.connected()) break;
        result.failed++;
        await this.reportFailure(item.id, error, done);
        // Mạng tới Zalo hỏng: dừng lượt này, khỏi đốt lượt thử của mọi tin còn lại.
        if (isTransientNetworkError(error)) break;
        continue;
      }
      try {
        await this.deps.ack(item.id);
        this.progress.delete(item.id);
        result.sent++;
      } catch (error) {
        // Python không nhận ack (đang khởi động lại?): dừng lượt. Lần sau tin quay
        // lại, `progress` cho biết đã gửi đủ nên chỉ ack lại, không gửi trùng.
        this.log(`[zalo] outbox ack #${item.id} failed; sẽ ack lại lượt sau`, error);
        break;
      }
    }
    return result;
  }

  private async reportFailure(id: number, error: unknown, chunksSent: number): Promise<void> {
    const message = errorMessage(error, String(error)).slice(0, MAX_ERROR_CHARS);
    this.log(`[zalo] outbox #${id} gửi lỗi (đã gửi ${chunksSent} đoạn)`, error);
    try {
      await this.deps.fail(id, message, chunksSent);
      // Python đã lưu chunks_sent: không cần nhớ trong RAM nữa.
      this.progress.delete(id);
    } catch (reportError) {
      this.log(`[zalo] không báo được lỗi outbox #${id} về Python`, reportError);
    }
  }

  private remember(id: number, done: number): void {
    this.progress.delete(id);
    this.progress.set(id, done);
    while (this.progress.size > this.maxTracked) {
      const oldest = this.progress.keys().next().value;
      if (oldest === undefined) break;
      this.progress.delete(oldest);
    }
  }

  private log(message: string, error?: unknown): void {
    if (this.deps.log) this.deps.log(message, error);
    else console.error(message, error);
  }
}
