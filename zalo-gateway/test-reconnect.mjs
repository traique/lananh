import test from "node:test";
import assert from "node:assert/strict";
import {
  MAX_RECONNECT_ATTEMPTS,
  RECONNECT_DELAYS_MS,
  ReconnectScheduler,
  reconnectDelayMs,
} from "./dist/reconnect.js";

const tick = () => new Promise((r) => setTimeout(r, 0));

/** Timer giả: ghi lại delay, chạy khi gọi `fire()` - test không phải chờ thật. */
function fakeTimers() {
  const scheduled = [];
  return {
    delays: () => scheduled.filter((t) => !t.cancelled).map((t) => t.ms),
    cancelled: () => scheduled.filter((t) => t.cancelled).length,
    setTimer: (fn, ms) => {
      const handle = { fn, ms, cancelled: false, fired: false };
      scheduled.push(handle);
      return handle;
    },
    clearTimer: (handle) => {
      handle.cancelled = true;
    },
    async fire() {
      const next = scheduled.find((t) => !t.cancelled && !t.fired);
      assert.ok(next, "không có timer nào đang chờ");
      next.fired = true;
      next.fn();
      await tick();
      await tick();
    },
  };
}

test("reconnectDelayMs: 5s, 15s, 60s rồi giữ 5 phút; hết lượt trả null", () => {
  assert.deepEqual(RECONNECT_DELAYS_MS, [5000, 15000, 60000, 300000]);
  assert.equal(reconnectDelayMs(0), 5000);
  assert.equal(reconnectDelayMs(1), 15000);
  assert.equal(reconnectDelayMs(2), 60000);
  assert.equal(reconnectDelayMs(3), 300000);
  assert.equal(reconnectDelayMs(7), 300000);
  assert.equal(reconnectDelayMs(MAX_RECONNECT_ATTEMPTS), null);
  assert.equal(reconnectDelayMs(0, []), null);
  assert.equal(reconnectDelayMs(2, [1, 2], 2), null);
});

test("ReconnectScheduler: backoff đúng thứ tự, thành công thì báo connected và dừng", async () => {
  const timers = fakeTimers();
  const events = [];
  let outcomes = ["retry", "retry", "connected"];
  const scheduler = new ReconnectScheduler({
    run: async (attempt) => {
      events.push(`run${attempt}`);
      return outcomes.shift();
    },
    onSchedule: (attempt, delay) => events.push(`wait${attempt}:${delay}`),
    onConnected: (attempts) => events.push(`connected:${attempts}`),
    onGiveUp: (attempts) => events.push(`giveup:${attempts}`),
    delaysMs: [5, 15, 60],
    maxAttempts: 10,
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
  });
  assert.equal(scheduler.start(), true);
  assert.equal(scheduler.start(), false, "đang chạy thì start() là no-op");
  assert.equal(scheduler.running, true);
  await timers.fire();
  await timers.fire();
  await timers.fire();
  assert.deepEqual(events, [
    "wait0:5",
    "run1",
    "wait1:15",
    "run2",
    "wait2:60",
    "run3",
    "connected:3",
  ]);
  assert.deepEqual(timers.delays(), [5, 15, 60]);
  assert.equal(scheduler.running, false);
  assert.equal(scheduler.attempted, 3);
});

test("ReconnectScheduler: hết lượt thì onGiveUp với đúng số lần, không hẹn thêm", async () => {
  const timers = fakeTimers();
  let gaveUp = null;
  let runs = 0;
  const scheduler = new ReconnectScheduler({
    run: async () => {
      runs++;
      throw new Error("mạng hỏng"); // ném lỗi = retry
    },
    onGiveUp: (attempts) => {
      gaveUp = attempts;
    },
    delaysMs: [1, 2],
    maxAttempts: 3,
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
  });
  scheduler.start();
  await timers.fire();
  await timers.fire();
  await timers.fire();
  assert.equal(runs, 3);
  assert.equal(gaveUp, 3);
  assert.deepEqual(timers.delays(), [1, 2, 2], "delay cuối được giữ nguyên khi vượt bảng");
  assert.equal(scheduler.running, false);
});

test("ReconnectScheduler: run trả give_up (phiên hỏng) thì bỏ cuộc ngay", async () => {
  const timers = fakeTimers();
  let gaveUp = null;
  const scheduler = new ReconnectScheduler({
    run: async () => "give_up",
    onGiveUp: (attempts) => {
      gaveUp = attempts;
    },
    delaysMs: [1, 2, 3],
    maxAttempts: 10,
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
  });
  scheduler.start();
  await timers.fire();
  assert.equal(gaveUp, 1);
  assert.equal(timers.delays().length, 1);
  assert.equal(scheduler.running, false);
  // Có thể bắt đầu chu kỳ mới sau khi bỏ cuộc.
  assert.equal(scheduler.start(), true);
  assert.equal(scheduler.attempted, 0);
});

test("ReconnectScheduler: cancel() huỷ timer và bỏ qua kết quả của lần thử đang chạy", async () => {
  const timers = fakeTimers();
  const events = [];
  let release;
  const gate = new Promise((r) => {
    release = r;
  });
  const scheduler = new ReconnectScheduler({
    run: async () => {
      await gate;
      return "connected";
    },
    onConnected: () => events.push("connected"),
    onGiveUp: () => events.push("giveup"),
    delaysMs: [1],
    maxAttempts: 5,
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
  });
  scheduler.start();
  scheduler.cancel();
  assert.equal(timers.cancelled(), 1);
  assert.equal(scheduler.running, false);
  // Chu kỳ mới, lần thử bắt đầu rồi bị huỷ giữa chừng: kết quả muộn không được áp dụng.
  scheduler.start();
  await timers.fire();
  scheduler.cancel();
  release();
  await tick();
  await tick();
  assert.deepEqual(events, []);
  assert.equal(scheduler.running, false);
});

test("ReconnectScheduler dùng setTimeout thật khi không tiêm timer", async () => {
  let connected = 0;
  const scheduler = new ReconnectScheduler({
    run: async () => "connected",
    onConnected: () => connected++,
    onGiveUp: () => assert.fail("không được bỏ cuộc"),
    delaysMs: [5],
  });
  scheduler.start();
  await new Promise((r) => setTimeout(r, 40));
  assert.equal(connected, 1);
});
