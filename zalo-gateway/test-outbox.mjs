import test from "node:test";
import assert from "node:assert/strict";
import { OutboxDelivery } from "./dist/outbox.js";
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
