/**
 * Tắt êm khi supervisord dừng gateway (SIGTERM, stopwaitsecs=15 rồi SIGKILL) hoặc Ctrl+C.
 * Trước đây tiến trình chết ngay: lượt tin riêng đang chờ Python mất trả lời, và tin
 * outbox dài gửi dở bị gửi lại từ đoạn 0 sau khi khởi động lại.
 *
 * 1. Thôi nhận việc mới: dừng listener Zalo, đóng dispatcher (tin chữ đang chờ gộp vào
 *    hàng đợi ngay), dừng lịch poll outbox / nạp nhóm.
 * 2. Chờ lượt outbox đang chạy dừng ở ranh giới đoạn, rồi chờ hàng đợi tin riêng, làn
 *    nhanh và việc gửi đang dở xong - tổng tối đa SHUTDOWN_DRAIN_MS.
 * 3. Báo Python tiến độ outbox gửi dở (POST /outbox/{id}/failed kèm chunks_sent), tối
 *    đa SHUTDOWN_REPORT_MS.
 * 4. Đóng control server; nơi gọi thoát tiến trình.
 */
import { withTimeout } from "./feedback.js";
import { errorMessage, sleep as defaultSleep } from "./text.js";

/** Chờ việc đang dở tối đa chừng này (supervisord SIGKILL sau 15s). */
export const SHUTDOWN_DRAIN_MS = 10000;
/** Trần thời gian báo tiến độ outbox về Python sau khi chờ xong. */
export const SHUTDOWN_REPORT_MS = 3000;
const POLL_MS = 100;

export type ShutdownDeps = {
  /** Thôi nhận tin mới (listener, dispatcher, timer nạp nhóm). Không ném. */
  stopIntake(): void;
  /** Dừng lịch outbox; resolve khi lượt đang chạy (nếu có) đã dừng. */
  stopOutbox(): Promise<void>;
  /** Không còn việc tin riêng/tin nhóm/gửi tin nào đang chờ hay đang chạy. */
  idle(): boolean;
  /** Báo Python tiến độ outbox gửi dở; trả số tin đã báo. */
  flushOutboxProgress(): Promise<number>;
  closeServer?(): Promise<void>;
  drainMs?: number;
  reportMs?: number;
  pollMs?: number;
  sleep?(ms: number): Promise<void>;
  now?(): number;
  log?(message: string): void;
};

export type ShutdownResult = {
  /** Mọi việc đang dở đã xong trước hạn. */
  drained: boolean;
  waitedMs: number;
  /** Số tin outbox đã báo tiến độ về Python. */
  reported: number;
};

export async function gracefulShutdown(deps: ShutdownDeps): Promise<ShutdownResult> {
  const now = deps.now ?? Date.now;
  const sleep = deps.sleep ?? defaultSleep;
  const log = deps.log ?? ((message: string) => console.warn(message));
  const started = now();
  const deadline = started + (deps.drainMs ?? SHUTDOWN_DRAIN_MS);
  deps.stopIntake();
  try {
    await withTimeout(deps.stopOutbox(), Math.max(1, deadline - now()), "dừng outbox");
  } catch (error) {
    log(`[zalo] tắt: lượt outbox chưa dừng kịp (${errorMessage(error, String(error))})`);
  }
  while (!deps.idle() && now() < deadline) await sleep(deps.pollMs ?? POLL_MS);
  const drained = deps.idle();
  if (!drained) log("[zalo] tắt: hết thời gian chờ, còn việc đang dở (bỏ)");
  let reported = 0;
  try {
    reported = await withTimeout(
      deps.flushOutboxProgress(),
      deps.reportMs ?? SHUTDOWN_REPORT_MS,
      "báo tiến độ outbox",
    );
  } catch (error) {
    log(`[zalo] tắt: chưa báo được tiến độ outbox (${errorMessage(error, String(error))})`);
  }
  try {
    await deps.closeServer?.();
  } catch {
    // Server đã đóng: bỏ qua.
  }
  return { drained, waitedMs: now() - started, reported };
}

export type SignalTarget = Pick<NodeJS.Process, "on">;

/**
 * SIGTERM/SIGINT lần đầu: chạy `shutdown` rồi thoát mã 0; tín hiệu lần hai trong lúc
 * đang tắt thì thoát ngay (mã 1). `exit` và `target` tiêm được cho test.
 */
export function installShutdownHandlers(
  shutdown: () => Promise<unknown>,
  exit: (code: number) => void = (code) => process.exit(code),
  target: SignalTarget = process,
): void {
  let stopping = false;
  const onSignal = (signal: NodeJS.Signals) => {
    if (stopping) {
      console.warn(`[zalo] nhận ${signal} lần nữa: thoát ngay`);
      exit(1);
      return;
    }
    stopping = true;
    console.log(`[zalo] nhận ${signal}: dừng nhận tin, chờ việc đang dở rồi thoát`);
    shutdown()
      .catch((error) => console.error("[zalo] lỗi lúc tắt", error))
      .finally(() => exit(0));
  };
  target.on("SIGTERM", onSignal);
  target.on("SIGINT", onSignal);
}
