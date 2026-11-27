import test from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { SHUTDOWN_DRAIN_MS, gracefulShutdown, installShutdownHandlers } from "./dist/shutdown.js";

/** Đồng hồ giả: sleep() chỉ cộng thời gian, không chờ thật. */
function fakeTime() {
  let now = 0;
  return {
    now: () => now,
    sleep: async (ms) => {
      now += ms;
      await new Promise((r) => setImmediate(r));
    },
  };
}

test("tắt êm: thôi nhận tin, dừng outbox, chờ hàng đợi xong rồi báo tiến độ outbox, đóng server", async () => {
  assert.equal(SHUTDOWN_DRAIN_MS, 10000);
  const time = fakeTime();
  const steps = [];
  const result = await gracefulShutdown({
    stopIntake: () => steps.push("intake"),
    stopOutbox: async () => void steps.push("outbox"),
    // Lượt tin riêng đang chạy xong sau 3s.
    idle: () => time.now() >= 3000,
    flushOutboxProgress: async () => {
      steps.push("flush");
      return 2;
    },
    closeServer: async () => void steps.push("close"),
    now: time.now,
    sleep: time.sleep,
    log: () => undefined,
  });
  assert.deepEqual(steps, ["intake", "outbox", "flush", "close"]);
  assert.deepEqual(result, { drained: true, waitedMs: 3000, reported: 2 });
});

test("tắt êm: việc dở quá hạn drain thì bỏ, vẫn báo tiến độ outbox", async () => {
  const time = fakeTime();
  const logs = [];
  let flushed = false;
  const result = await gracefulShutdown({
    stopIntake: () => undefined,
    stopOutbox: async () => undefined,
    idle: () => false,
    flushOutboxProgress: async () => {
      flushed = true;
      return 1;
    },
    drainMs: 500,
    pollMs: 100,
    now: time.now,
    sleep: time.sleep,
    log: (line) => logs.push(line),
  });
  assert.equal(result.drained, false);
  assert.equal(result.waitedMs, 500);
  assert.equal(flushed, true);
  assert.match(logs.join("\n"), /hết thời gian chờ/);
});

test("tắt êm: lượt outbox treo hoặc Python không trả lời lúc báo tiến độ thì không treo theo", async () => {
  const logs = [];
  const started = Date.now();
  // withTimeout dùng timer.unref(): tự giữ event loop kẻo Node thoát giữa test.
  const keepAlive = setInterval(() => undefined, 10);
  try {
    const result = await gracefulShutdown({
      stopIntake: () => undefined,
      stopOutbox: () => new Promise(() => undefined),
      idle: () => true,
      flushOutboxProgress: () => new Promise(() => undefined),
      drainMs: 50,
      reportMs: 50,
      log: (line) => logs.push(line),
    });
    assert.ok(Date.now() - started < 2000);
    assert.equal(result.drained, true);
    assert.equal(result.reported, 0);
    assert.match(logs[0], /lượt outbox chưa dừng kịp/);
    assert.match(logs[1], /chưa báo được tiến độ outbox/);
  } finally {
    clearInterval(keepAlive);
  }
});

test("SIGTERM lần đầu chạy tắt êm rồi thoát 0; tín hiệu lần hai thoát ngay mã 1", async () => {
  const signals = new EventEmitter();
  const exits = [];
  let finish;
  let runs = 0;
  const originalLog = console.log;
  const originalWarn = console.warn;
  console.log = () => undefined;
  console.warn = () => undefined;
  try {
    installShutdownHandlers(
      () => {
        runs++;
        return new Promise((resolve) => {
          finish = resolve;
        });
      },
      (code) => exits.push(code),
      signals,
    );
    signals.emit("SIGTERM", "SIGTERM");
    assert.equal(runs, 1);
    assert.deepEqual(exits, []);
    signals.emit("SIGINT", "SIGINT");
    assert.deepEqual(exits, [1]);
    finish();
    await new Promise((r) => setImmediate(r));
    assert.deepEqual(exits, [1, 0]);
    assert.equal(runs, 1);
  } finally {
    console.log = originalLog;
    console.warn = originalWarn;
  }
});
