import { sleep as defaultSleep } from "./text.js";

/** Gửi trả lời Zalo: thử lại 3 lần (1s, 3s, 8s) rồi mới bỏ cuộc. */
export const SEND_RETRY_DELAYS_MS = [1000, 3000, 8000];
/** Lưu tin nhóm sang Python: thử lại 2 lần rồi log. */
export const STORE_RETRY_DELAYS_MS = [1000, 3000];

export type RetryOptions = {
  /** Thời gian chờ trước MỖI lần thử lại; số lần thử lại = độ dài mảng. */
  delaysMs: readonly number[];
  /** Trả false để dừng ngay (lỗi vĩnh viễn). Mặc định: thử lại mọi lỗi. */
  shouldRetry?: (error: unknown, attempt: number) => boolean;
  /** Gọi trước khi chờ thử lại (để log). `attempt` là số lần đã thất bại (1-based). */
  onRetry?: (error: unknown, attempt: number, delayMs: number) => void;
  sleep?: (ms: number) => Promise<void>;
};

/**
 * Chạy `fn` và thử lại theo `delaysMs` khi ném lỗi. Lần thử đầu chạy ngay;
 * hết lượt (hoặc `shouldRetry` trả false) thì ném lỗi cuối cùng.
 */
export async function retry<T>(
  fn: (attempt: number) => Promise<T>,
  options: RetryOptions,
): Promise<T> {
  const sleep = options.sleep ?? defaultSleep;
  const maxAttempts = options.delaysMs.length + 1;
  let attempt = 0;
  for (;;) {
    try {
      return await fn(attempt);
    } catch (error) {
      attempt++;
      if (attempt >= maxAttempts || (options.shouldRetry && !options.shouldRetry(error, attempt))) {
        throw error;
      }
      const delayMs = options.delaysMs[attempt - 1];
      options.onRetry?.(error, attempt, delayMs);
      await sleep(delayMs);
    }
  }
}
