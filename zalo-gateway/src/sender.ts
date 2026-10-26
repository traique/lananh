import { SEND_RETRY_DELAYS_MS, retry } from "./retry.js";
import { errorMessage, sleep as defaultSleep } from "./text.js";
import { ZALO_TEXT_STYLES, type ZaloSendContent, type ZaloTextStyle } from "./types.js";

/** Khoảng nghỉ ngẫu nhiên trong [minMs, maxMs] (min/max <= 0 = không nghỉ). */
export function jitterMs(minMs: number, maxMs: number, random: () => number = Math.random): number {
  const lo = Math.max(0, Math.min(minMs, maxMs));
  const hi = Math.max(0, Math.max(minMs, maxMs));
  if (hi <= 0) return 0;
  return Math.round(lo + (hi - lo) * random());
}

/**
 * Zalo đã nhận request và TỪ CHỐI nội dung (ZaloApiError của zca-js mang
 * error_code số từ server, name "ZcaApiError"). Khác lỗi mạng/HTTP/timeout -
 * những lỗi đó tin có thể đã tới nơi, gửi lại bản khác sẽ thành 2 tin.
 */
export function isZaloRejection(error: unknown): boolean {
  const e = error as { name?: unknown; code?: unknown } | null;
  return (
    (e?.name === "ZcaApiError" || e?.name === "ZaloApiError") &&
    typeof e?.code === "number" &&
    Number.isFinite(e.code)
  );
}

const STYLE_NAMES = new Set<string>(ZALO_TEXT_STYLES);

/**
 * Lọc styles từ Python cho đúng 1 đoạn: chỉ giữ TextStyle gateway hỗ trợ, toạ
 * độ nguyên không âm, cắt phần vượt độ dài đoạn (UTF-16 như String.length).
 */
export function sanitizeStyles(chunk: string, styles: unknown): ZaloTextStyle[] {
  if (!Array.isArray(styles)) return [];
  const out: ZaloTextStyle[] = [];
  for (const raw of styles.slice(0, 200)) {
    const item = raw as { start?: unknown; len?: unknown; st?: unknown } | null;
    if (!item || typeof item.st !== "string" || !STYLE_NAMES.has(item.st)) continue;
    const start = Number(item.start);
    const len = Number(item.len);
    if (!Number.isInteger(start) || !Number.isInteger(len) || start < 0 || len < 1) continue;
    if (start >= chunk.length) continue;
    out.push({
      start,
      len: Math.min(len, chunk.length - start),
      st: item.st as ZaloTextStyle["st"],
    });
  }
  return out;
}

export type ChunkSender = {
  sendMessage(message: ZaloSendContent, threadId: string, type: number): Promise<unknown>;
};

export type SendChunksOptions = {
  /** ThreadType.User/Group của zca-js. */
  threadType: number;
  /** false = phiên Zalo đã đổi/mất giữa chừng: dừng thử lại. */
  isCurrent: () => boolean;
  /** Khoảng nghỉ trước mỗi đoạn từ đoạn thứ 2 (ms); mặc định không nghỉ. */
  gapMs?: () => number;
  sleep?: (ms: number) => Promise<void>;
  retryDelaysMs?: readonly number[];
  onPlainFallback?: (error: unknown) => void;
  onChunkSent?: () => void;
  log?: (message: string) => void;
};

/**
 * Gửi 1 đoạn: có styles thì gửi kèm styles; Zalo TỪ CHỐI (isZaloRejection) thì
 * gửi lại ĐÚNG 1 lần bản chữ thuần - ngoài vòng thử lại chung, vì thử lại y
 * nguyên bản có styles chỉ bị từ chối tiếp. Lỗi mạng/timeout giữ cách thử lại
 * cũ (1s, 3s, 8s) và KHÔNG rơi về chữ thuần (tin có styles có thể đã tới nơi).
 */
export async function sendChunk(
  client: ChunkSender,
  target: string,
  chunk: string,
  styles: ZaloTextStyle[],
  options: SendChunksOptions,
): Promise<void> {
  const delaysMs = options.retryDelaysMs ?? SEND_RETRY_DELAYS_MS;
  const attempt = (content: ZaloSendContent, rejectionIsFinal: boolean) =>
    retry(() => client.sendMessage(content, target, options.threadType), {
      delaysMs,
      sleep: options.sleep,
      shouldRetry: (error) => options.isCurrent() && !(rejectionIsFinal && isZaloRejection(error)),
      onRetry: (error, n, delayMs) =>
        (options.log ?? console.warn)(
          `[zalo] gửi tin tới ${target} lỗi (lần ${n}/${delaysMs.length + 1}); ` +
            `thử lại sau ${delayMs}ms: ${errorMessage(error, String(error))}`,
        ),
    });
  if (styles.length) {
    try {
      await attempt({ msg: chunk, styles }, true);
      options.onChunkSent?.();
      return;
    } catch (error) {
      if (!isZaloRejection(error) || !options.isCurrent()) throw error;
      options.onPlainFallback?.(error);
      (options.log ?? console.warn)(
        `[zalo] Zalo từ chối tin có định dạng tới ${target} ` +
          `(${errorMessage(error, String(error))}); gửi lại bản chữ thuần`,
      );
    }
  }
  await attempt({ msg: chunk }, false);
  options.onChunkSent?.();
}

/**
 * Gửi lần lượt các đoạn (bỏ đoạn rỗng), nghỉ `gapMs()` giữa các đoạn để nhịp
 * gửi không giống máy. `styles[i]` là định dạng của `chunks[i]` (cùng chỉ số với
 * messages của Python); thiếu = chữ thuần. Lỗi đoạn nào ném lỗi đoạn đó (các
 * đoạn trước đã gửi).
 */
export async function sendChunks(
  client: ChunkSender,
  target: string,
  chunks: string[],
  styles: unknown,
  options: SendChunksOptions,
): Promise<number> {
  const perChunk = Array.isArray(styles) ? styles : [];
  const pause = options.sleep ?? defaultSleep;
  let sent = 0;
  for (let i = 0; i < chunks.length; i++) {
    const chunk = chunks[i];
    if (!chunk || !chunk.trim()) continue;
    if (sent > 0) {
      const gap = options.gapMs?.() ?? 0;
      if (gap > 0) await pause(gap);
    }
    await sendChunk(client, target, chunk, sanitizeStyles(chunk, perChunk[i]), options);
    sent++;
  }
  return sent;
}
