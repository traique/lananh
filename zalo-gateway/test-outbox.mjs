import test from "node:test";
import assert from "node:assert/strict";
import {
  OUTBOX_KICK_DEBOUNCE_MS,
  OutboxDelivery,
  OutboxPoller,
  SHUTDOWN_OUTBOX_REASON,
} from "./dist/outbox.js";
import { splitForZalo } from "./dist/text.js";

function harness(items, { sendFails = () => null, ackFails = () => false, pace, takeBudget } = {}) {
  const calls = { sent: [], acked: [], failed: [] };
  let connected = true;
  const deps = {
    fetch: async () => items.filter((item) => !calls.acked.includes(item.id)),
    sendChunk: async (recipient, chunk) => {
      const error = sendFails(recipient, chunk);
      if (error) throw error;
      calls.sent.push([recipient, chunk]);
    },
    ack: async (id) => {
      if (ackFails(id)) throw new Error("python down");
      calls.acked.push(id);
    },
    fail: async (id, error, chunksSent) => {
      calls.failed.push({ id, error, chunksSent });
    },
    connected: () => connected,
    log: () => undefined,
    pace,
    takeBudget,
  };
  return {
    calls,
    delivery: new OutboxDelivery(deps),
    disconnect: () => {
      connected = false;
    },
  };
}

test("1 tin lỗi không chặn tin của người nhận khác; tin sau cùng người nhận chờ lượt sau", async () => {
  const { calls, delivery } = harness(
    [
      { id: 1, recipient_id: "blocked", content: "tin 1" },
      { id: 2, recipient_id: "ok", content: "tin 2" },
      { id: 3, recipient_id: "blocked", content: "tin 3" },
      { id: 4, recipient_id: "ok", content: "tin 4" },
    ],
    {
      sendFails: (recipient) => (recipient === "blocked" ? new Error("user blocked bot") : null),
    },
  );
  const result = await delivery.runOnce();
  assert.deepEqual(result, { sent: 2, failed: 1 });
  assert.deepEqual(calls.acked, [2, 4]);
  assert.deepEqual(calls.failed, [{ id: 1, error: "user blocked bot", chunksSent: 0 }]);
  assert.deepEqual(
    calls.sent.map(([recipient]) => recipient),
    ["ok", "ok"],
  );
});

test("tin dài gửi dở: báo số đoạn đã gửi và lần sau gửi tiếp từ đoạn kế", async () => {
  const content = Array.from({ length: 400 }, (_, i) => `dòng số ${i} của bản tóm tắt`).join("\n");
  const chunks = splitForZalo(content);
  assert.ok(chunks.length >= 3);
  let failOn = chunks[1];
  const item = { id: 9, recipient_id: "u", content };
  const { calls, delivery } = harness([item], {
    sendFails: (_recipient, chunk) => (chunk === failOn ? new Error("rate limited") : null),
  });
  await delivery.runOnce();
  assert.deepEqual(calls.failed, [{ id: 9, error: "rate limited", chunksSent: 1 }]);
  // Python lưu chunks_sent và trả lại ở lượt sau.
  item.chunks_sent = 1;
  failOn = null;
  await delivery.runOnce();
  assert.deepEqual(calls.acked, [9]);
  assert.deepEqual(
    calls.sent.map(([, chunk]) => chunk),
    chunks,
    "mỗi đoạn gửi đúng 1 lần",
  );
});

test("ack lỗi: lượt sau chỉ ack lại, không gửi trùng", async () => {
  let pythonDown = true;
  const { calls, delivery } = harness([{ id: 5, recipient_id: "u", content: "nhắc việc" }], {
    ackFails: () => pythonDown,
  });
  await delivery.runOnce();
  assert.equal(calls.sent.length, 1);
  assert.deepEqual(calls.acked, []);
  assert.equal(delivery.tracked, 1);
  pythonDown = false;
  await delivery.runOnce();
  assert.equal(calls.sent.length, 1);
  assert.deepEqual(calls.acked, [5]);
  assert.equal(delivery.tracked, 0);
});

test("mất kết nối Zalo giữa chừng: không báo lỗi cho tin, dừng lượt", async () => {
  const state = {};
  const { calls, delivery, disconnect } = harness(
    [
      { id: 1, recipient_id: "a", content: "x" },
      { id: 2, recipient_id: "b", content: "y" },
    ],
    {
      sendFails: () => {
        state.disconnect();
        return new Error("Zalo is not connected");
      },
    },
  );
  state.disconnect = disconnect;
  const result = await delivery.runOnce();
  assert.deepEqual(result, { sent: 0, failed: 0 });
  assert.deepEqual(calls.failed, []);
});

test("lỗi mạng tới Zalo: báo lỗi 1 tin rồi dừng lượt, không đốt lượt thử của tin khác", async () => {
  const network = Object.assign(new Error("fetch failed"), { cause: { code: "ECONNRESET" } });
  const { calls, delivery } = harness(
    [
      { id: 1, recipient_id: "a", content: "x" },
      { id: 2, recipient_id: "b", content: "y" },
    ],
    { sendFails: () => network },
  );
  const result = await delivery.runOnce();
  assert.deepEqual(result, { sent: 0, failed: 1 });
  assert.deepEqual(
    calls.failed.map((f) => f.id),
    [1],
  );
});

test("bộ nhớ tiến độ có trần", async () => {
  const items = Array.from({ length: 5 }, (_, i) => ({
    id: i + 1,
    recipient_id: `u${i}`,
    content: "x",
  }));
  const deps = {
    fetch: async () => items,
    sendChunk: async () => undefined,
    ack: async () => {
      throw new Error("python down");
    },
    fail: async () => undefined,
    connected: () => true,
    log: () => undefined,
  };
  const delivery = new OutboxDelivery(deps, 2);
  // Ack lỗi dừng lượt sau tin đầu; chạy nhiều lượt với id khác nhau.
  for (const item of items) {
    deps.fetch = async () => [item];
    await delivery.runOnce();
  }
  assert.equal(delivery.tracked, 2);
});

test("hết ngân sách gửi theo giờ: tin còn lại HOÃN (không ack, không báo lỗi), lượt sau gửi tiếp", async () => {
  let budget = 1;
  const items = [
    { id: 1, recipient_id: "a", content: "tin 1" },
    { id: 2, recipient_id: "b", content: "tin 2" },
    { id: 3, recipient_id: "c", content: "tin 3" },
  ];
  const { calls, delivery } = harness(items, {
    takeBudget: () => (budget > 0 ? (budget--, true) : false),
  });
  const first = await delivery.runOnce();
  assert.deepEqual(first, { sent: 1, failed: 0, deferred: 2 });
  assert.deepEqual(calls.acked, [1]);
  assert.deepEqual(calls.failed, []);
  budget = 5;
  const second = await delivery.runOnce();
  assert.deepEqual(second, { sent: 2, failed: 0 });
  assert.deepEqual(calls.acked, [1, 2, 3]);
});

test("tin đã gửi đủ ở lượt trước (chỉ chờ ack) không tốn ngân sách", async () => {
  const item = { id: 5, recipient_id: "a", content: "tin" };
  let acks = 0;
  const { delivery } = harness([item], {
    ackFails: () => acks++ === 0,
    takeBudget: (() => {
      let left = 1;
      return () => (left > 0 ? (left--, true) : false);
    })(),
  });
  await delivery.runOnce(); // gửi xong, ack lỗi
  const result = await delivery.runOnce(); // hết ngân sách nhưng chỉ cần ack lại
  assert.deepEqual(result, { sent: 1, failed: 0 });
});

test("nghỉ giữa các đoạn và giữa các tin, không nghỉ trước lần gửi đầu", async () => {
  const content = Array.from({ length: 300 }, (_, i) => `dòng ${i} của tin dài`).join("\n");
  const chunks = splitForZalo(content);
  let paces = 0;
  const { calls, delivery } = harness(
    [
      { id: 1, recipient_id: "a", content },
      { id: 2, recipient_id: "b", content: "ngắn" },
    ],
    { pace: async () => void paces++ },
  );
  await delivery.runOnce();
  assert.equal(calls.sent.length, chunks.length + 1);
  assert.equal(paces, calls.sent.length - 1);
});

const longContent = () =>
  Array.from({ length: 400 }, (_, i) => `dòng số ${i} của bản tóm tắt`).join("\n");

test("tắt giữa tin dài: dừng ở ranh giới đoạn, báo Python số đoạn đã gửi để lần sau gửi tiếp", async () => {
  const content = longContent();
  const chunks = splitForZalo(content);
  assert.ok(chunks.length >= 3);
  let delivery;
  const item = { id: 11, recipient_id: "u", content };
  const harnessed = harness([item, { id: 12, recipient_id: "v", content: "tin sau" }], {
    // SIGTERM tới đúng lúc đang gửi đoạn thứ 2: đoạn đó vẫn gửi xong, không gửi thêm.
    sendFails: (_recipient, chunk) => {
      if (chunk === chunks[1]) delivery.stop();
      return null;
    },
  });
  delivery = harnessed.delivery;
  const result = await delivery.runOnce();
  assert.deepEqual(result, { sent: 0, failed: 0 });
  assert.equal(harnessed.calls.sent.length, 2);
  assert.deepEqual(harnessed.calls.acked, [], "tin chưa gửi đủ thì không ack");
  assert.deepEqual(harnessed.calls.failed, []);
  assert.equal(await delivery.flushProgress(), 1);
  assert.deepEqual(harnessed.calls.failed, [
    { id: 11, error: SHUTDOWN_OUTBOX_REASON, chunksSent: 2 },
  ]);
  assert.equal(delivery.tracked, 0);
  assert.deepEqual(await delivery.runOnce(), { sent: 0, failed: 0 }, "đã dừng: không gửi nữa");
});

test("flushProgress: tin gửi đủ mà ack lỗi thì ack lại lúc tắt; Python vẫn lỗi thì giữ để báo sau", async () => {
  let pythonDown = true;
  const { calls, delivery } = harness([{ id: 5, recipient_id: "u", content: "nhắc việc" }], {
    ackFails: () => pythonDown,
  });
  await delivery.runOnce();
  assert.equal(delivery.tracked, 1);
  assert.equal(await delivery.flushProgress(), 0, "Python vẫn lỗi");
  assert.equal(delivery.tracked, 1);
  pythonDown = false;
  delivery.stop();
  assert.equal(await delivery.flushProgress(), 1);
  assert.deepEqual(calls.acked, [5]);
  assert.deepEqual(calls.failed, [], "đã gửi đủ: ack, không báo lỗi");
});

/** Timer giả: advance(ms) chạy mọi timer đã tới hạn. */
function fakeClock() {
  let now = 0;
  const timers = [];
  return {
    setTimer: (fn, ms) => {
      const handle = { fn, at: now + ms, done: false };
      timers.push(handle);
      return handle;
    },
    clearTimer: (handle) => {
      handle.done = true;
    },
    pending: () => timers.filter((t) => !t.done).map((t) => t.at - now),
    async advance(ms) {
      now += ms;
      for (const timer of [...timers]) {
        if (!timer.done && timer.at <= now) {
          timer.done = true;
          timer.fn();
        }
      }
      for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r));
    },
  };
}

test("OutboxPoller: kick gộp trong debounce thành 1 lượt; poll định kỳ vẫn chạy", async () => {
  assert.equal(OUTBOX_KICK_DEBOUNCE_MS, 500);
  const clock = fakeClock();
  let runs = 0;
  const poller = new OutboxPoller({
    run: async () => void runs++,
    intervalMs: 90000,
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
  });
  poller.start();
  assert.deepEqual(clock.pending(), [90000]);
  assert.equal(poller.kick(), true);
  assert.equal(poller.kick(), true);
  await clock.advance(100);
  assert.equal(poller.kick(), true, "kick sau không lùi hẹn");
  await clock.advance(400);
  assert.equal(runs, 1);
  await clock.advance(89500);
  assert.equal(runs, 2, "lượt định kỳ");
  assert.deepEqual(clock.pending(), [90000], "hẹn lượt định kỳ kế");
  await poller.stop();
});

test("OutboxPoller: kick lúc đang chạy -> chạy thêm đúng 1 lượt; stop trả lượt đang chạy", async () => {
  const clock = fakeClock();
  let runs = 0;
  let release;
  const poller = new OutboxPoller({
    run: () => {
      runs++;
      return new Promise((resolve) => {
        release = resolve;
      });
    },
    intervalMs: 90000,
    debounceMs: 0,
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
  });
  poller.kick();
  await clock.advance(0);
  assert.equal(runs, 1);
  assert.equal(poller.busy, true);
  poller.kick();
  await clock.advance(0);
  poller.kick();
  await clock.advance(0);
  assert.equal(runs, 1, "chưa chạy chồng");
  release();
  await clock.advance(0);
  assert.equal(runs, 2, "chạy thêm 1 lượt cho các kick trong lúc chạy");
  let stopped = false;
  const stopping = poller.stop().then(() => {
    stopped = true;
  });
  await clock.advance(0);
  assert.equal(stopped, false, "chờ lượt đang chạy");
  release();
  await stopping;
  assert.equal(runs, 2);
  assert.equal(poller.kick(), false, "đã dừng: không nhận kick");
  assert.deepEqual(clock.pending(), []);
});

test("OutboxPoller: lượt lỗi chỉ log, lượt sau vẫn chạy", async () => {
  const clock = fakeClock();
  const logged = [];
  let runs = 0;
  const poller = new OutboxPoller({
    run: async () => {
      runs++;
      throw new Error("python down");
    },
    intervalMs: 1000,
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
    log: (message, error) => logged.push(`${message}: ${error.message}`),
  });
  poller.start();
  await clock.advance(1000);
  await clock.advance(1000);
  assert.equal(runs, 2);
  assert.deepEqual(logged, [
    "[zalo] outbox poll lỗi: python down",
    "[zalo] outbox poll lỗi: python down",
  ]);
  await poller.stop();
});
