/** Khoảng chờ trước mỗi lần đăng nhập lại từ phiên đã lưu: 5s, 15s, 60s, rồi 5 phút. */
export const RECONNECT_DELAYS_MS = [5000, 15000, 60000, 300000];
/** Tổng số lần thử tối đa (~37 phút với bảng trên) rồi chuyển sang "cần đăng nhập lại". */
export const MAX_RECONNECT_ATTEMPTS = 10;

/**
 * Thời gian chờ trước lần thử thứ `attempt + 1` (`attempt` = số lần đã thất bại,
 * 0 = chưa thử). null = hết lượt, bỏ cuộc.
 */
export function reconnectDelayMs(
  attempt: number,
  delaysMs: readonly number[] = RECONNECT_DELAYS_MS,
  maxAttempts = MAX_RECONNECT_ATTEMPTS,
): number | null {
  if (attempt >= maxAttempts || !delaysMs.length) return null;
  return delaysMs[Math.min(attempt, delaysMs.length - 1)];
}

/**
 * Kết quả 1 lần thử:
 * - connected: đã đăng nhập lại (hoặc có phiên khác đang/đã đăng nhập) -> dừng;
 * - retry: lỗi tạm (Python chưa lên, mạng Zalo chập chờn) -> hẹn lần sau;
 * - give_up: phiên đã lưu không còn dùng được -> cần quét QR lại.
 */
export type ReconnectOutcome = "connected" | "retry" | "give_up";

export type ReconnectDeps = {
  /** Thử đăng nhập lại; ném lỗi được coi như "retry". `attempt` là số thứ tự lần thử (1-based). */
  run: (attempt: number) => Promise<ReconnectOutcome>;
  /** Hết lượt hoặc phiên hỏng. `attempts` = số lần đã thử. */
  onGiveUp: (attempts: number) => void;
  /** Đã kết nối lại sau `attempts` lần thử. */
  onConnected?: (attempts: number) => void;
  /** Sắp chờ `delayMs` rồi thử lần `attempt + 1` (để log / đổi trạng thái). */
  onSchedule?: (attempt: number, delayMs: number) => void;
  delaysMs?: readonly number[];
  maxAttempts?: number;
  /** Cho test: thay setTimeout/clearTimeout. */
  setTimer?: (fn: () => void, ms: number) => unknown;
  clearTimer?: (handle: unknown) => void;
};

/**
 * Lịch đăng nhập lại có backoff, không giữ gì ngoài 1 timer và 1 bộ đếm. Chỉ
 * có một chu kỳ chạy tại một thời điểm: `start()` khi đang chạy là no-op.
 * `cancel()` dùng khi người dùng chủ động quét QR / đăng xuất.
 */
export class ReconnectScheduler {
  private attempts = 0;
  private timer: unknown = null;
  private active = false;
  /** Phân biệt chu kỳ: callback của lần thử thuộc chu kỳ đã huỷ không được tác động. */
  private generation = 0;

  constructor(private readonly deps: ReconnectDeps) {}

  get running(): boolean {
    return this.active;
  }

  /** Số lần đã thử trong chu kỳ hiện tại. */
  get attempted(): number {
    return this.attempts;
  }

  /** Bắt đầu chu kỳ mới; trả false nếu đang chạy sẵn. */
  start(): boolean {
    if (this.active) return false;
    this.active = true;
    this.attempts = 0;
    this.generation++;
    this.schedule();
    return true;
  }

  cancel(): void {
    if (this.timer !== null) {
      (this.deps.clearTimer ?? clearTimeout)(this.timer as NodeJS.Timeout);
      this.timer = null;
    }
    this.active = false;
    this.generation++;
  }

  private schedule(): void {
    const delay = reconnectDelayMs(this.attempts, this.deps.delaysMs, this.deps.maxAttempts);
    if (delay === null) {
      this.finish();
      this.deps.onGiveUp(this.attempts);
      return;
    }
    this.deps.onSchedule?.(this.attempts, delay);
    const generation = this.generation;
    const setTimer =
      this.deps.setTimer ??
      ((fn: () => void, ms: number) => {
        const handle = setTimeout(fn, ms);
        handle.unref();
        return handle;
      });
    this.timer = setTimer(() => {
      this.timer = null;
      void this.fire(generation);
    }, delay);
  }

  private async fire(generation: number): Promise<void> {
    if (generation !== this.generation) return;
    this.attempts++;
    let outcome: ReconnectOutcome;
    try {
      outcome = await this.deps.run(this.attempts);
    } catch {
      outcome = "retry";
    }
    if (generation !== this.generation) return;
    if (outcome === "connected") {
      const attempts = this.attempts;
      this.finish();
      this.deps.onConnected?.(attempts);
    } else if (outcome === "give_up") {
      const attempts = this.attempts;
      this.finish();
      this.deps.onGiveUp(attempts);
    } else {
      this.schedule();
    }
  }

  private finish(): void {
    this.active = false;
    this.timer = null;
  }
}
