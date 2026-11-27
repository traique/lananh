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
/** Lý do báo Python khi gateway tắt (SIGTERM) lúc tin dài mới gửi được 1 phần. */
export const SHUTDOWN_OUTBOX_REASON = "gateway tắt giữa chừng (SIGTERM), gửi tiếp phần còn lại";

/** Số đoạn đã gửi / tổng số đoạn của 1 tin mà Python có thể chưa ghi nhận. */
type Progress = { done: number; total: number };

/**
 * Gửi outbox (tóm tắt nhóm, nhắc việc, bản xem trước bài Facebook...) từng tin
 * một, lỗi tin nào xử lý tin đó:
 * - tin lỗi được báo về Python (tăng số lần thử + backoff, quá N lần thì bỏ),
 *   các tin của người nhận KHÁC vẫn gửi tiếp - không còn chặn đầu hàng đợi;
 * - tin sau của CÙNG người nhận chờ lượt sau để giữ đúng thứ tự;
 * - tin dài gửi dở: lần sau gửi tiếp từ đoạn kế (Python lưu `chunks_sent`, gateway
 *   nhớ thêm trong RAM phòng khi không báo/ack được về Python);
 * - tắt gateway (stop()): lượt đang chạy dừng ở ranh giới đoạn, flushProgress() báo
 *   Python số đoạn đã gửi để lần sau không gửi lại từ đoạn 0.
 */
export class OutboxDelivery {
  /** id tin -> tiến độ gửi mà Python có thể chưa ghi nhận. Có trần. */
  private readonly progress = new Map<number, Progress>();
  private stopped = false;

  constructor(
    private readonly deps: OutboxDeps,
    private readonly maxTracked = MAX_TRACKED_ITEMS,
  ) {}

  get tracked(): number {
    return this.progress.size;
  }

  /** Lúc tắt: không gửi thêm đoạn/tin nào (lượt đang chạy dừng trước đoạn kế). */
  stop(): void {
    this.stopped = true;
  }

  async runOnce(): Promise<OutboxRunResult> {
    const result: OutboxRunResult = { sent: 0, failed: 0 };
    if (this.stopped || !this.deps.connected()) return result;
    const items = await this.deps.fetch();
    const blocked = new Set<string>();
    let sentAny = false;
    for (const [index, item] of items.entries()) {
      if (this.stopped || !this.deps.connected()) break;
      if (blocked.has(item.recipient_id)) continue;
      const chunks = splitForZalo(item.content);
      let done = Math.max(Number(item.chunks_sent) || 0, this.progress.get(item.id)?.done ?? 0);
      if (done < chunks.length && this.deps.takeBudget && !this.deps.takeBudget()) {
        // Hết ngân sách: tin này và các tin sau chờ lượt poll kế (không ack, không báo lỗi).
        result.deferred = items.length - index;
        break;
      }
      try {
        for (const chunk of chunks.slice(done)) {
          if (this.stopped) break;
          if (chunk.trim()) {
            if (sentAny && this.deps.pace) await this.deps.pace();
            if (this.stopped) break;
            await this.deps.sendChunk(item.recipient_id, chunk);
            sentAny = true;
          }
          done++;
          this.remember(item.id, done, chunks.length);
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
      // Tắt giữa chừng: tiến độ nằm trong `progress`, flushProgress() báo Python.
      if (done < chunks.length) break;
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

  /**
   * Lúc tắt (SIGTERM): báo Python mọi tiến độ Python chưa biết - tin mới gửi được 1
   * phần báo /outbox/{id}/failed kèm chunks_sent (lần sau gửi tiếp từ đoạn kế), tin
   * đã gửi đủ mà chưa ack được thì ack lại. Trả số tin đã báo được; lỗi chỉ log.
   */
  async flushProgress(reason = SHUTDOWN_OUTBOX_REASON): Promise<number> {
    let reported = 0;
    for (const [id, { done, total }] of [...this.progress]) {
      try {
        if (done >= total) await this.deps.ack(id);
        else await this.deps.fail(id, reason, done);
        this.progress.delete(id);
        reported++;
      } catch (error) {
        this.log(`[zalo] không báo được tiến độ outbox #${id} (${done}/${total} đoạn)`, error);
      }
    }
    return reported;
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

  private remember(id: number, done: number, total: number): void {
    this.progress.delete(id);
    this.progress.set(id, { done, total });
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

/** Nhiều lần kick trong chừng này gộp thành 1 lượt poll (Python thêm cả loạt tin cùng lúc). */
export const OUTBOX_KICK_DEBOUNCE_MS = 500;

export type OutboxPollerOptions = {
  /** 1 lượt gửi outbox (OutboxDelivery.runOnce + đếm/log); lỗi chỉ log. */
  run(): Promise<void>;
  /** Chu kỳ poll dự phòng (ZALO_OUTBOX_POLL_MS). */
  intervalMs: number;
  debounceMs?: number;
  /** Cho test: thay setTimeout/clearTimeout (timer mặc định unref, không giữ tiến trình). */
  setTimer?: (fn: () => void, ms: number) => unknown;
  clearTimer?: (handle: unknown) => void;
  log?: (message: string, error?: unknown) => void;
};

/**
 * Lịch gửi outbox: poll định kỳ `intervalMs` (dự phòng) + kick() khi Python vừa thêm
 * tin (control server POST /outbox/kick) - tin tới ngay mà không phải poll DB mỗi 15s.
 * - Chỉ 1 lượt chạy tại 1 thời điểm; kick lúc đang chạy thì chạy thêm ĐÚNG 1 lượt ngay
 *   sau (tin có thể được thêm sau lúc lượt này đọc DB).
 * - Kick đầu tiên hẹn 1 lượt sau `debounceMs`; kick tới trước lúc đó gộp vào lượt ấy
 *   (không lùi hẹn, nên kick liên tục không làm lượt bị hoãn mãi).
 * - stop(): huỷ mọi hẹn, không nhận kick nữa, trả promise của lượt đang chạy.
 * RAM: 2 timer và 1 promise.
 */
export class OutboxPoller {
  private inflight: Promise<void> | null = null;
  private again = false;
  private kickTimer: unknown = null;
  private tickTimer: unknown = null;
  private stopped = false;
  private readonly debounceMs: number;
  private readonly setTimer: (fn: () => void, ms: number) => unknown;
  private readonly clearTimer: (handle: unknown) => void;

  constructor(private readonly options: OutboxPollerOptions) {
    this.debounceMs = Math.max(0, options.debounceMs ?? OUTBOX_KICK_DEBOUNCE_MS);
    this.setTimer =
      options.setTimer ??
      ((fn, ms) => {
        const handle = setTimeout(fn, ms);
        handle.unref();
        return handle;
      });
    this.clearTimer =
      options.clearTimer ?? ((handle) => clearTimeout(handle as NodeJS.Timeout | undefined));
  }

  /** Đang có lượt gửi chạy. */
  get busy(): boolean {
    return this.inflight !== null;
  }

  /** Bắt đầu poll định kỳ (lượt đầu sau `intervalMs`). */
  start(): void {
    if (this.stopped || this.tickTimer !== null) return;
    this.scheduleTick();
  }

  /** Python vừa thêm tin: poll sớm. false nếu đã dừng (đang tắt). */
  kick(): boolean {
    if (this.stopped) return false;
    if (this.kickTimer !== null) return true;
    this.kickTimer = this.setTimer(() => {
      this.kickTimer = null;
      this.trigger();
    }, this.debounceMs);
    return true;
  }

  /** Dừng hẳn: huỷ hẹn, bỏ lượt "chạy thêm", trả promise lượt đang chạy (nếu có). */
  stop(): Promise<void> {
    this.stopped = true;
    this.again = false;
    for (const handle of [this.kickTimer, this.tickTimer]) {
      if (handle !== null) this.clearTimer(handle);
    }
    this.kickTimer = null;
    this.tickTimer = null;
    return this.inflight ?? Promise.resolve();
  }

  private scheduleTick(): void {
    this.tickTimer = this.setTimer(() => {
      this.tickTimer = null;
      if (this.stopped) return;
      this.trigger();
      this.scheduleTick();
    }, this.options.intervalMs);
  }

  private trigger(): void {
    if (this.stopped) return;
    if (this.inflight) {
      this.again = true;
      return;
    }
    this.inflight = this.runLoop().finally(() => {
      this.inflight = null;
    });
  }

  private async runLoop(): Promise<void> {
    do {
      this.again = false;
      try {
        await this.options.run();
      } catch (error) {
        (this.options.log ?? console.error)("[zalo] outbox poll lỗi", error);
      }
    } while (this.again && !this.stopped);
  }
}
