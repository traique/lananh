import test from "node:test";
import assert from "node:assert/strict";
import { DirectTextBatcher } from "./dist/burst.js";
import { KeyedSerialQueue, QueueFullError } from "./dist/limits.js";

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

/** Harness: batcher + KeyedSerialQueue thật; mỗi "lượt" ghi lại tin của lô. */
function harness(options = {}, queueOptions = {}) {
  const turns = [];
  const rejected = [];
  let release = null;
  const queue = new KeyedSerialQueue({
    concurrency: 2,
    perKeyCapacity: 8,
    capacity: 64,
    ...queueOptions,
  });
  const batcher = new DirectTextBatcher({
    debounceMs: 30,
    maxWaitMs: 200,
    maxBufferItems: 4,
    maxMergeItems: 8,
    maxMergeChars: 4000,
    charsOf: (item) => item.length,
    enqueue: (sender, work) => queue.add(sender, work),
    run: async (sender, items) => {
      turns.push([sender, items.join(" | ")]);
      if (release) await release;
    },
    onRejected: (sender, items, error) => rejected.push([sender, items, error]),
    ...options,
  });
  return {
    turns,
    rejected,
    queue,
    batcher,
    hold() {
      let open;
      release = new Promise((resolve) => {
        open = resolve;
      });
      return () => {
        release = null;
        open();
      };
    },
  };
}

test("debounce: các dòng gõ liền nhau thành 1 lượt", async () => {
  const { turns, batcher } = harness();
  assert.equal(batcher.add("u", "em ơi"), "buffered");
  await wait(10);
  batcher.add("u", "mai trời mưa ko");
  await wait(10);
  batcher.add("u", "ở Đà Lạt");
  assert.equal(turns.length, 0, "chưa hết thời gian debounce");
  await wait(60);
  assert.deepEqual(turns, [["u", "em ơi | mai trời mưa ko | ở Đà Lạt"]]);
  assert.equal(batcher.size, 0);
});

test("đủ 4 tin trong lúc debounce thì gửi luôn", async () => {
  const { turns, batcher } = harness({ debounceMs: 1000 });
  for (const text of ["1", "2", "3"]) assert.equal(batcher.add("u", text), "buffered");
  assert.equal(batcher.add("u", "4"), "queued");
  await wait(5);
  assert.deepEqual(turns, [["u", "1 | 2 | 3 | 4"]]);
});

test("trần chờ tính từ tin đầu: gõ liên tục cũng không chờ quá maxWaitMs", async () => {
  const { turns, batcher } = harness({ debounceMs: 40, maxWaitMs: 70, maxBufferItems: 99 });
  const started = Date.now();
  for (let i = 0; i < 6; i++) {
    batcher.add("u", `t${i}`);
    await wait(15);
  }
  await wait(60);
  assert.ok(turns.length >= 1);
  assert.ok(turns[0][1].startsWith("t0"));
  assert.ok(Date.now() - started < 400);
});

test("gộp lúc tới lượt: tin gõ trong lúc lượt trước chạy thành 1 lượt kế tiếp", async () => {
  const { turns, batcher, hold, queue } = harness({ debounceMs: 0 });
  const releaseFirst = hold();
  assert.equal(batcher.add("u", "câu 1"), "queued");
  await wait(5);
  assert.equal(queue.isRunning("u"), true);
  assert.equal(batcher.add("u", "câu 2"), "queued");
  assert.equal(batcher.add("u", "câu 3"), "merged");
  assert.equal(batcher.add("u", "câu 4"), "merged");
  releaseFirst();
  await wait(10);
  assert.deepEqual(turns, [
    ["u", "câu 1"],
    ["u", "câu 2 | câu 3 | câu 4"],
  ]);
  assert.equal(queue.isRunning("u"), false);
});

test("lô chờ lượt đầy (maxMergeItems/maxMergeChars) thì mở lô mới phía sau", async () => {
  const { turns, batcher, hold } = harness({ debounceMs: 0, maxMergeItems: 2, maxMergeChars: 9 });
  const releaseFirst = hold();
  batcher.add("u", "a");
  await wait(5);
  batcher.add("u", "b");
  batcher.add("u", "c"); // lô thứ 2 đủ 2 tin
  batcher.add("u", "d"); // lô thứ 3
  batcher.add("u", "0123456789"); // quá maxMergeChars của lô 3 -> lô 4
  releaseFirst();
  await wait(20);
  assert.deepEqual(
    turns.map(([, text]) => text),
    ["a", "b | c", "d", "0123456789"],
  );
});

test("close(): tin khác loại xếp sau giữ đúng thứ tự, tin chữ sau đó mở lô mới", async () => {
  const { turns, batcher, queue } = harness({ debounceMs: 1000 });
  batcher.add("u", "trước ảnh");
  batcher.close("u");
  await queue.add("u", async () => void turns.push(["u", "[ẢNH]"]));
  batcher.add("u", "sau ảnh");
  batcher.flush("u");
  await wait(10);
  assert.deepEqual(
    turns.map(([, text]) => text),
    ["trước ảnh", "[ẢNH]", "sau ảnh"],
  );
});

test("lô đã close không nhận thêm tin dù còn chờ lượt", async () => {
  const { turns, batcher, hold, queue } = harness({ debounceMs: 0 });
  const releaseFirst = hold();
  batcher.add("u", "1");
  await wait(5);
  batcher.add("u", "2"); // lô chờ lượt
  batcher.close("u"); // vd ảnh tới
  const image = queue.add("u", async () => void turns.push(["u", "[ẢNH]"]));
  batcher.add("u", "3"); // phải đứng SAU ảnh
  releaseFirst();
  await image;
  await wait(10);
  assert.deepEqual(
    turns.map(([, text]) => text),
    ["1", "2", "[ẢNH]", "3"],
  );
});

test("discardBuffered (/huy): bỏ tin chưa gửi, không đụng lô đã vào hàng đợi", async () => {
  const { turns, batcher } = harness({ debounceMs: 1000 });
  batcher.add("u", "gõ nhầm");
  assert.equal(batcher.discardBuffered("u"), 1);
  assert.equal(batcher.size, 0);
  await wait(10);
  assert.deepEqual(turns, []);
  const h = harness({ debounceMs: 0 });
  const releaseFirst = h.hold();
  h.batcher.add("u", "1");
  await wait(5);
  h.batcher.add("u", "2");
  assert.equal(h.batcher.discardBuffered("u"), 0);
  releaseFirst();
  await wait(10);
  assert.equal(h.turns.length, 2);
});

test("người gửi khác nhau không gộp lẫn", async () => {
  const { turns, batcher } = harness();
  batcher.add("a", "a1");
  batcher.add("b", "b1");
  batcher.add("a", "a2");
  await wait(60);
  assert.deepEqual(turns.sort(), [
    ["a", "a1 | a2"],
    ["b", "b1"],
  ]);
});

test("hàng đợi đầy: lô bị từ chối được báo onRejected kèm tin", async () => {
  const { batcher, rejected, hold } = harness(
    { debounceMs: 0, maxMergeItems: 1 },
    { perKeyCapacity: 1 },
  );
  const releaseFirst = hold();
  batcher.add("u", "1");
  await wait(5);
  batcher.add("u", "2");
  await wait(5);
  assert.equal(rejected.length, 1);
  assert.deepEqual(rejected[0][1], ["2"]);
  assert.ok(rejected[0][2] instanceof QueueFullError);
  releaseFirst();
});

test("quá nhiều người đang debounce: lô mới vào hàng đợi ngay (RAM có trần)", async () => {
  const { batcher } = harness({ debounceMs: 1000, maxSenders: 2 });
  assert.equal(batcher.add("a", "x"), "buffered");
  assert.equal(batcher.add("b", "x"), "buffered");
  assert.equal(batcher.add("c", "x"), "queued");
  assert.equal(batcher.buffering, 2);
  batcher.flush("a");
  batcher.flush("b");
});
