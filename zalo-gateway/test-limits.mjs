import test from "node:test";
import assert from "node:assert/strict";
import {
  BoundedSerialQueue,
  KeyedMutex,
  KeyedRateLimiter,
  KeyedSerialQueue,
  QueueFullError,
  SlidingWindowBudget,
  readLimitedBody,
} from "./dist/limits.js";
test("queue bounds waiting events, preserves order, and recovers after failures", async () => {
  const q = new BoundedSerialQueue(2),
    order = [];
  let release;
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  const first = q.add(async () => {
    await gate;
    order.push(1);
    throw new Error("first failed");
  });
  const second = q.add(async () => {
    order.push(2);
  });
  await assert.rejects(
    q.add(async () => {
      order.push(3);
    }),
    /overloaded/,
  );
  release();
  await assert.rejects(first, /first failed/);
  await second;
  await q.add(async () => {
    order.push(4);
  });
  assert.deepEqual(order, [1, 2, 4]);
});
test("streamed image limit cancels download even without Content-Length", async () => {
  let cancelled = false;
  const stream = new ReadableStream({
    start(controller) {
      controller.enqueue(new Uint8Array(3));
      controller.enqueue(new Uint8Array(4));
    },
    cancel() {
      cancelled = true;
    },
  });
  await assert.rejects(readLimitedBody(new Response(stream), 5), /exceeds/);
  assert.equal(cancelled, true);
});
test("streamed image chunks assemble without truncation", async () => {
  const stream = new ReadableStream({
    start(controller) {
      controller.enqueue(new Uint8Array([1, 2]));
      controller.enqueue(new Uint8Array([3]));
      controller.close();
    },
  });
  assert.deepEqual(await readLimitedBody(new Response(stream), 3), new Uint8Array([1, 2, 3]));
});

function deferred() {
  let resolve;
  const promise = new Promise((r) => {
    resolve = r;
  });
  return { promise, resolve };
}
const tick = () => new Promise((r) => setTimeout(r, 0));

test("KeyedSerialQueue: cùng người tuần tự đúng thứ tự, khác người chạy song song có trần", async () => {
  const q = new KeyedSerialQueue({ concurrency: 2, perKeyCapacity: 5, capacity: 10 });
  const gates = { a1: deferred(), b1: deferred(), c1: deferred() };
  const started = [];
  const job = (name) => async () => {
    started.push(name);
    if (gates[name]) await gates[name].promise;
  };
  const done = [
    q.add("a", job("a1")),
    q.add("a", job("a2")),
    q.add("b", job("b1")),
    q.add("c", job("c1")),
  ];
  await tick();
  // a1 và b1 chạy (trần 2); a2 đợi a1, c1 đợi slot.
  assert.deepEqual(started, ["a1", "b1"]);
  assert.equal(q.running, 2);
  gates.b1.resolve();
  await tick();
  // Slot của b nhường cho khoá đang chờ lâu nhất (c), a2 vẫn đợi a1.
  assert.deepEqual(started, ["a1", "b1", "c1"]);
  gates.a1.resolve();
  await tick();
  assert.deepEqual(started, ["a1", "b1", "c1", "a2"]);
  gates.c1.resolve();
  await Promise.all(done);
  assert.equal(q.keys, 0, "khoá rảnh phải được xoá khỏi Map");
  assert.equal(q.pending, 0);
  assert.equal(q.running, 0);
});

test("KeyedSerialQueue: trần backlog mỗi người và tổng, lỗi 1 việc không chặn việc sau", async () => {
  const q = new KeyedSerialQueue({ concurrency: 1, perKeyCapacity: 2, capacity: 3 });
  const gate = deferred();
  const first = q.add("a", async () => {
    await gate.promise;
    throw new Error("boom");
  });
  const second = q.add("a", async () => undefined);
  await assert.rejects(
    q.add("a", async () => undefined),
    (error) => {
      assert.ok(error instanceof QueueFullError);
      assert.equal(error.scope, "key");
      return true;
    },
  );
  // Tổng việc CHỜ (chưa chạy): a2 + b1 + c1 = 3 -> việc thứ 4 bị từ chối.
  const b = q.add("b", async () => undefined);
  const c = q.add("c", async () => undefined);
  await assert.rejects(
    q.add("d", async () => undefined),
    (error) => error.scope === "global",
  );
  gate.resolve();
  await assert.rejects(first, /boom/);
  await Promise.all([second, b, c]);
  assert.equal(q.keys, 0);
  await q.add("a", async () => undefined);
});

test("KeyedRateLimiter cho phép 1 lần mỗi khoảng và giới hạn số khoá nhớ", () => {
  const limiter = new KeyedRateLimiter(1000, 2);
  assert.equal(limiter.allow("a", 0), true);
  assert.equal(limiter.allow("a", 500), false);
  assert.equal(limiter.allow("a", 1500), true);
  limiter.allow("b", 1500);
  limiter.allow("c", 1500);
  assert.equal(limiter.size, 2);
  assert.equal(limiter.allow("a", 1600), true, "khoá cũ nhất đã bị đẩy ra");
});

test("KeyedSerialQueue.isRunning: chỉ true khi khoá đang chạy việc", async () => {
  const q = new KeyedSerialQueue({ concurrency: 1, perKeyCapacity: 4, capacity: 8 });
  let release;
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  assert.equal(q.isRunning("a"), false);
  const job = q.add("a", () => gate);
  const waiting = q.add("b", async () => undefined);
  assert.equal(q.isRunning("a"), true);
  assert.equal(q.isRunning("b"), false, "b đang chờ slot, chưa chạy");
  release();
  await job;
  await waiting;
  assert.equal(q.isRunning("a"), false);
});

test("SlidingWindowBudget: trần theo cửa sổ trượt, 0 = không giới hạn", () => {
  const budget = new SlidingWindowBudget(2, 1000);
  assert.equal(budget.tryTake(0), true);
  assert.equal(budget.tryTake(100), true);
  assert.equal(budget.tryTake(200), false);
  assert.equal(budget.remaining(500), 0);
  assert.equal(budget.tryTake(1000), true, "mốc 0 đã ra khỏi cửa sổ");
  assert.equal(budget.remaining(1050), 0);
  assert.equal(budget.remaining(1100), 1, "mốc 100 cũng hết hạn");
  assert.equal(budget.remaining(2200), 2);
  const unlimited = new SlidingWindowBudget(0, 1000);
  for (let i = 0; i < 100; i++) assert.equal(unlimited.tryTake(i), true);
  assert.equal(unlimited.remaining(), Infinity);
});

const sleepMs = (ms) => new Promise((r) => setTimeout(r, ms));

test("KeyedMutex: 2 câu trả lời cho cùng người nhận không xen đoạn, người khác chạy song song", async () => {
  const lock = new KeyedMutex(5000);
  const log = [];
  const reply = (key, name, parts, gap) =>
    lock.run(key, async () => {
      for (let i = 1; i <= parts; i++) {
        log.push(`${name}${i}`);
        await sleepMs(gap);
      }
      return name;
    });
  // Câu trả lời 4 đoạn có nghỉ giữa đoạn; /help tới giữa chừng phải chờ hết.
  const answer = reply("S", "answer#", 4, 5);
  await sleepMs(2);
  const help = reply("S", "help#", 2, 1);
  const other = reply("T", "other#", 1, 0);
  assert.deepEqual(await Promise.all([answer, help, other]), ["answer#", "help#", "other#"]);
  const forS = log.filter((x) => !x.startsWith("other"));
  assert.deepEqual(forS, ["answer#1", "answer#2", "answer#3", "answer#4", "help#1", "help#2"]);
  assert.ok(log.indexOf("other#1") < log.indexOf("answer#4"), "người nhận khác không phải chờ");
  assert.equal(lock.size, 0, "khoá rảnh bị xoá: Map không phình theo người nhận");
});

test("KeyedMutex: việc lỗi vẫn nhả khoá, lỗi trả về đúng nơi gọi", async () => {
  const lock = new KeyedMutex(5000);
  const failed = lock.run("S", async () => {
    throw new Error("gửi lỗi");
  });
  const next = lock.run("S", async () => "ok");
  await assert.rejects(failed, /gửi lỗi/);
  assert.equal(await next, "ok");
  await sleepMs(0);
  assert.equal(lock.size, 0);
});

test("KeyedMutex: việc trước treo thì việc sau chỉ chờ tối đa maxWaitMs", async () => {
  const lock = new KeyedMutex(20);
  void lock.run("S", () => new Promise(() => undefined)); // zca-js treo, không bao giờ xong
  const started = Date.now();
  assert.equal(await lock.run("S", async () => "vẫn gửi"), "vẫn gửi");
  assert.ok(Date.now() - started < 1000);
  await sleepMs(0);
  assert.equal(lock.size, 0, "việc treo không giữ khoá mãi");
});
