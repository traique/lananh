/**
 * Phản hồi trong lúc chờ Python trả lời 1 lượt dài (Zalo không có gì hiện ra
 * suốt 20-240s): "đang soạn tin" làm mới định kỳ, có trần số lần mỗi lượt. Mọi
 * lệnh gọi zca-js ở đây đều có timeout ngắn và nuốt lỗi - không bao giờ được
 * chặn hàng đợi tin riêng (KeyedSerialQueue).
 */

/** Chờ `promise` tối đa `ms`; quá hạn thì reject (promise gốc vẫn chạy nền). */
export function withTimeout<T>(promise: Promise<T>, ms: number, label = "zalo"): Promise<T> {
  let timer: NodeJS.Timeout | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label}: quá ${ms}ms`)), ms);
    timer.unref?.();
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

/** Gọi `fn` không chờ: timeout ngắn, lỗi chỉ báo qua `onError` (không ném). */
export function fireAndForget(
  fn: () => Promise<unknown>,
  timeoutMs: number,
  onError: (error: unknown) => void,
): void {
  let started: Promise<unknown>;
  try {
    started = fn();
  } catch (error) {
    onError(error);
    return;
  }
  withTimeout(started, timeoutMs).catch(onError);
}

export type TypingOptions = {
  /** Khoảng làm mới (Zalo tự tắt "đang soạn tin" sau vài giây). */
  intervalMs?: number;
  /** Số lần gửi tối đa mỗi lượt (kể cả lần đầu) - ~30 lần x 6s = 3 phút. */
  maxEvents?: number;
  timeoutMs?: number;
  onError?: (error: unknown) => void;
  onSent?: () => void;
};

/**
 * "Đang soạn tin" cho 1 lượt: gửi ngay khi bắt đầu rồi làm mới mỗi `intervalMs`
 * tới khi stop() hoặc hết `maxEvents`. Lỗi lần đầu được báo, các lần sau im lặng
 * (tránh log ồ ạt khi Zalo từ chối sự kiện này).
 */
export class TypingKeepalive {
  private timer: NodeJS.Timeout | null = null;
  private sent = 0;
  private failed = false;
  private readonly intervalMs: number;
  private readonly maxEvents: number;
  private readonly timeoutMs: number;

  constructor(
    private readonly send: () => Promise<unknown>,
    private readonly options: TypingOptions = {},
  ) {
    this.intervalMs = Math.max(1000, options.intervalMs ?? 6000);
    this.maxEvents = Math.max(1, options.maxEvents ?? 30);
    this.timeoutMs = Math.max(100, options.timeoutMs ?? 5000);
  }

  get count(): number {
    return this.sent;
  }

  get active(): boolean {
    return this.timer !== null;
  }

  start(): this {
    if (this.timer || this.sent) return this;
    this.tick();
    if (this.sent < this.maxEvents) {
      this.timer = setInterval(() => this.tick(), this.intervalMs);
      this.timer.unref?.();
    }
    return this;
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }

  private tick(): void {
    if (this.sent >= this.maxEvents) {
      this.stop();
      return;
    }
    this.sent++;
    if (this.sent >= this.maxEvents) this.stop();
    fireAndForget(
      async () => {
        await this.send();
        this.options.onSent?.();
      },
      this.timeoutMs,
      (error) => {
        if (this.failed) return;
        this.failed = true;
        this.options.onError?.(error);
      },
    );
  }
}
