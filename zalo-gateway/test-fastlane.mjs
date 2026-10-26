import test from "node:test";
import assert from "node:assert/strict";
import { DirectTextBatcher } from "./dist/burst.js";
import { FastLane } from "./dist/fast-lane.js";
import { KeyedSerialQueue } from "./dist/limits.js";

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

function gate() {
  let open;
  const promise = new Promise((resolve) => {
    open = resolve;
  });
  return { promise, open };
}

/**
 * Harness: FastLane + DirectTextBatcher + KeyedSerialQueue thật. `turns` ghi thứ
 * tự các lượt chạy trong hàng đợi theo người gửi; `calls` ghi lệnh làn nhanh đã
 * gửi Python. `holdCalls()` giữ mọi lệnh làn nhanh đang chờ Python.
 */
function harness(options = {}) {
  const turns = [];
  const calls = [];
  const logs = [];
  let callGate = null;
  let laneGate = null;
  const queue = new KeyedSerialQueue({ concurrency: 2, perKeyCapacity: 8, capacity: 64 });
  const batcher = new DirectTextBatcher({
    debounceMs: 1000,
    maxWaitMs: 5000,
    maxBufferItems: 4,
    maxMergeItems: 8,
    maxMergeChars: 4000,
    charsOf: (item) => item.text.length,
    enqueue: (sender, work) => queue.add(sender, work),
    run: async (_sender, items) => {
      turns.push(items.map((item) => item.text).join(" | "));
    },
  });
  const lane = new FastLane({
    commandLimit: 4,
    cancelLimit: 4,
    isCancel: (msg) => msg.text === "/huy",
    batcher,
    call: async (msg) => {
      calls.push(`${msg.sender}:${msg.text}`);
      if (callGate) await callGate.promise;
      if (msg.fail) throw new Error("bridge down");
    },
    enqueue: (msg) => {
      queue.add(msg.sender, async () => void turns.push(msg.text)).catch(() => undefined);
    },
    log: (message) => logs.push(message),
    ...options,
  });
  return {
    turns,
    calls,
    logs,
    queue,
    batcher,
    lane,
    holdCalls() {
      callGate = gate();
      const current = callGate;
      return () => {
        callGate = null;
        current.open();
      };
    },
    /** Giữ 1 lượt đang CHẠY của người gửi (vd lượt AI dài). */
    holdLane(sender) {
      laneGate = gate();
      const current = laneGate;
      queue.add(sender, () => current.promise);
      return () => current.open();
    },
  };
}

const msg = (sender, text, extra = {}) => ({ sender, text, ...extra });

test("làn nhanh đầy: lệnh chỉ đọc vào hàng đợi SAU lô chữ trước nó, chữ gõ sau mở lô mới phía sau", async () => {
  const h = harness({ commandLimit: 1 });
  const releaseCalls = h.holdCalls();
  // Làn nhanh đã đầy vì lệnh của người khác đang chờ Python.
  assert.equal(h.lane.submit(msg("T", "/help")), "sent");
  const releaseLane = h.holdLane("S");
  h.batcher.add("S", msg("S", "em ơi"));
  assert.equal(h.lane.submit(msg("S", "/notes")), "queued");
  h.batcher.add("S", msg("S", "thêm ghi chú mua sữa"));
  h.batcher.flush("S");
  releaseLane();
  await wait(20);
  // Trước đây "thêm ghi chú..." gộp vào lô "em ơi" và chạy TRƯỚC /notes.
  assert.deepEqual(h.turns, ["em ơi", "/notes", "thêm ghi chú mua sữa"]);
  assert.deepEqual(h.calls, ["T:/help"]);
  releaseCalls();
});

test("làn nhanh còn chỗ: lệnh chỉ đọc trả lời ngay, tin chữ đang chờ vẫn nhận thêm", async () => {
  const h = harness();
  const releaseLane = h.holdLane("S");
  h.batcher.add("S", msg("S", "em ơi"));
  assert.equal(h.lane.submit(msg("S", "/notes")), "sent");
  h.batcher.add("S", msg("S", "mai trời mưa ko"));
  releaseLane();
  await wait(20);
  assert.deepEqual(h.calls, ["S:/notes"]);
  assert.deepEqual(h.turns, ["em ơi | mai trời mưa ko"]);
});

test("/huy: mỗi người tối đa 1 lần chờ Python, gửi dồn dập không mở thêm request", async () => {
  const h = harness();
  const releaseCalls = h.holdCalls();
  const results = [];
  for (let i = 0; i < 50; i++) {
    h.batcher.add("S", msg("S", `gõ nhầm ${i}`));
    results.push(h.lane.submit(msg("S", "/huy")));
  }
  assert.equal(results[0], "sent");
  assert.ok(results.slice(1).every((r) => r === "coalesced"));
  assert.deepEqual(h.calls, ["S:/huy"]);
  assert.equal(h.lane.active, 1);
  // Tin chữ chưa gửi vẫn bị bỏ ngay tại gateway dù không gọi lại Python.
  assert.equal(h.batcher.size, 0);
  releaseCalls();
  await wait(5);
  assert.equal(h.lane.active, 0);
  assert.equal(h.lane.submit(msg("S", "/huy")), "sent");
  assert.equal(h.calls.length, 2);
  await wait(5);
  assert.deepEqual(h.turns, [], "/huy không bao giờ vào hàng đợi");
});

test("/huy: trần chung số người đang chờ huỷ, vượt thì bỏ (không xếp hàng)", async () => {
  const h = harness({ cancelLimit: 2 });
  const releaseCalls = h.holdCalls();
  assert.equal(h.lane.submit(msg("a", "/huy")), "sent");
  assert.equal(h.lane.submit(msg("b", "/huy")), "sent");
  assert.equal(h.lane.submit(msg("c", "/huy")), "dropped");
  assert.equal(h.lane.active, 2);
  assert.ok(h.logs.some((line) => line.includes("/huy của c bị bỏ")));
  // Lệnh chỉ đọc có trần riêng: /huy dồn dập không chặn được lệnh chỉ đọc.
  assert.equal(h.lane.submit(msg("d", "/help")), "sent");
  releaseCalls();
  await wait(5);
  assert.equal(h.lane.active, 0);
  assert.equal(h.lane.submit(msg("c", "/huy")), "sent");
  await wait(5);
  assert.deepEqual(h.turns, []);
});

test("làn nhanh: lệnh lỗi vẫn trả chỗ", async () => {
  const h = harness({ commandLimit: 1, cancelLimit: 1 });
  assert.equal(h.lane.submit(msg("S", "/help", { fail: true })), "sent");
  assert.equal(h.lane.submit(msg("S", "/huy", { fail: true })), "sent");
  await wait(5);
  assert.equal(h.lane.active, 0);
  assert.equal(h.lane.submit(msg("S", "/help")), "sent");
  assert.equal(h.lane.submit(msg("S", "/huy")), "sent");
  assert.equal(h.logs.filter((line) => line.includes("bridge down")).length, 2);
});
