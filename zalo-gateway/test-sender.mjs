import test from "node:test";
import assert from "node:assert/strict";
import { isZaloRejection, jitterMs, sanitizeStyles, sendChunk, sendChunks } from "./dist/sender.js";
import { TypingKeepalive, fireAndForget, withTimeout } from "./dist/feedback.js";

// Giống ZaloApiError của zca-js 2.1.2 (dist/Errors/ZaloApiError.js).
class ZcaApiError extends Error {
  constructor(message, code) {
    super(message);
    this.name = "ZcaApiError";
    this.code = code || null;
  }
}

function fakeClient(failures = []) {
  const calls = [];
  return {
    calls,
    async sendMessage(content, target, type) {
      calls.push({ content, target, type });
      const failure = failures.shift();
      if (failure) throw failure;
      return { message: { msgId: calls.length }, attachment: [] };
    },
  };
}

const base = {
  threadType: 0,
  isCurrent: () => true,
  sleep: async () => undefined,
  retryDelaysMs: [1, 1, 1],
  log: () => undefined,
};

test("isZaloRejection: chỉ lỗi Zalo có error_code số", () => {
  assert.equal(isZaloRejection(new ZcaApiError("Tham số không hợp lệ", 114)), true);
  assert.equal(isZaloRejection(new ZcaApiError("Request failed with status code 502")), false);
  assert.equal(isZaloRejection(Object.assign(new Error("fetch failed"), { code: 1 })), false);
  assert.equal(isZaloRejection(null), false);
});

test("sanitizeStyles: bỏ style lạ/toạ độ hỏng, cắt phần vượt độ dài đoạn", () => {
  const chunk = "Lãi +2,5% 📈";
  assert.equal(chunk.length, 12); // emoji ngoài BMP = 2 đơn vị UTF-16
  assert.deepEqual(
    sanitizeStyles(chunk, [
      { start: 0, len: 3, st: "b" },
      { start: 4, len: 50, st: "c_15a85f" },
      { start: 1, len: 2, st: "lst_1" },
      { start: -1, len: 2, st: "i" },
      { start: 1.5, len: 2, st: "i" },
      { start: 99, len: 1, st: "u" },
    ]),
    [
      { start: 0, len: 3, st: "b" },
      { start: 4, len: 8, st: "c_15a85f" },
    ],
  );
  assert.deepEqual(sanitizeStyles(chunk, null), []);
});

test("gửi kèm styles của đúng đoạn; đoạn rỗng bỏ qua nhưng không lệch chỉ số", async () => {
  const client = fakeClient();
  const sent = await sendChunks(
    client,
    "u1",
    ["Mở đầu", "  ", "Tiêu đề", "chữ thường"],
    [null, null, [{ start: 0, len: 7, st: "b" }], null],
    base,
  );
  assert.equal(sent, 3);
  assert.deepEqual(
    client.calls.map((c) => c.content),
    [
      { msg: "Mở đầu" },
      { msg: "Tiêu đề", styles: [{ start: 0, len: 7, st: "b" }] },
      { msg: "chữ thường" },
    ],
  );
});

test("Zalo từ chối tin có định dạng: gửi lại ĐÚNG 1 lần chữ thuần, không vòng thử lại", async () => {
  const client = fakeClient([new ZcaApiError("Định dạng không hợp lệ", 114)]);
  let fallbacks = 0;
  await sendChunk(client, "u1", "Tiêu đề", [{ start: 0, len: 7, st: "b" }], {
    ...base,
    onPlainFallback: () => fallbacks++,
  });
  assert.deepEqual(
    client.calls.map((c) => c.content),
    [{ msg: "Tiêu đề", styles: [{ start: 0, len: 7, st: "b" }] }, { msg: "Tiêu đề" }],
  );
  assert.equal(fallbacks, 1);
});

test("lỗi mạng khi gửi có định dạng: thử lại bản có định dạng, không rơi về chữ thuần", async () => {
  const network = Object.assign(new Error("fetch failed"), { cause: { code: "ECONNRESET" } });
  const client = fakeClient([network, network]);
  await sendChunk(client, "u1", "Tiêu đề", [{ start: 0, len: 7, st: "b" }], base);
  assert.equal(client.calls.length, 3);
  assert.ok(client.calls.every((c) => c.content.styles));
  // Hết lượt thử lại vì lỗi mạng: ném lỗi, không gửi bản chữ thuần.
  const dead = fakeClient([network, network, network, network]);
  await assert.rejects(
    sendChunk(dead, "u1", "Tiêu đề", [{ start: 0, len: 7, st: "b" }], base),
    /fetch failed/,
  );
  assert.equal(dead.calls.length, 4);
  assert.ok(dead.calls.every((c) => c.content.styles));
});

test("tin chữ thuần bị từ chối vẫn thử lại như cũ (không có bản nào khác để gửi)", async () => {
  const client = fakeClient([new ZcaApiError("busy", 1), new ZcaApiError("busy", 1)]);
  await sendChunk(client, "u1", "xin chào", [], base);
  assert.equal(client.calls.length, 3);
});

test("phiên Zalo đổi giữa chừng: không thử lại, không gửi chữ thuần", async () => {
  const client = fakeClient([new ZcaApiError("x", 114)]);
  await assert.rejects(
    sendChunk(client, "u1", "a", [{ start: 0, len: 1, st: "b" }], {
      ...base,
      isCurrent: () => false,
    }),
  );
  assert.equal(client.calls.length, 1);
});

test("nghỉ ngẫu nhiên giữa các đoạn (không nghỉ trước đoạn đầu)", async () => {
  const client = fakeClient();
  const slept = [];
  await sendChunks(client, "u1", ["a", "b", "c"], null, {
    ...base,
    sleep: async (ms) => void slept.push(ms),
    gapMs: () => 900,
  });
  assert.deepEqual(slept, [900, 900]);
});

test("jitterMs nằm trong khoảng, 0/0 là tắt", () => {
  assert.equal(
    jitterMs(600, 1500, () => 0),
    600,
  );
  assert.equal(
    jitterMs(600, 1500, () => 1),
    1500,
  );
  assert.equal(
    jitterMs(1500, 600, () => 0.5),
    1050,
  );
  assert.equal(jitterMs(0, 0), 0);
  for (let i = 0; i < 50; i++) {
    const ms = jitterMs(600, 1500);
    assert.ok(ms >= 600 && ms <= 1500);
  }
});

test("TypingKeepalive: gửi ngay, làm mới theo chu kỳ, dừng ở trần số lần", async () => {
  let sent = 0;
  const typing = new TypingKeepalive(async () => void sent++, {
    intervalMs: 1000,
    maxEvents: 3,
  });
  // intervalMs tối thiểu 1000ms: thử nhanh bằng cách gọi tick qua timer giả là quá phức
  // tạp, nên kiểm lần đầu + trạng thái rồi dừng.
  typing.start();
  await new Promise((r) => setImmediate(r));
  assert.equal(sent, 1);
  assert.equal(typing.active, true);
  typing.stop();
  assert.equal(typing.active, false);
  typing.start(); // đã chạy 1 lần: không khởi động lại
  assert.equal(typing.count, 1);
});

test("TypingKeepalive: lỗi chỉ báo 1 lần, maxEvents=1 không đặt hẹn giờ", async () => {
  const errors = [];
  const typing = new TypingKeepalive(
    async () => {
      throw new Error("blocked");
    },
    { maxEvents: 1, onError: (e) => errors.push(e.message) },
  ).start();
  await new Promise((r) => setTimeout(r, 10));
  assert.equal(typing.active, false);
  assert.deepEqual(errors, ["blocked"]);
});

test("TypingKeepalive làm mới thật sau intervalMs", async () => {
  let sent = 0;
  const typing = new TypingKeepalive(async () => void sent++, {
    intervalMs: 1000,
    maxEvents: 2,
  }).start();
  await new Promise((r) => setTimeout(r, 1100));
  assert.equal(sent, 2);
  assert.equal(typing.active, false, "đủ trần thì tự dừng");
});

test("fireAndForget/withTimeout: treo thì hết hạn, không ném ra ngoài", async () => {
  const errors = [];
  fireAndForget(
    () => new Promise(() => undefined),
    20,
    (e) => errors.push(e.message),
  );
  fireAndForget(
    () => {
      throw new Error("sync boom");
    },
    20,
    (e) => errors.push(e.message),
  );
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(errors.length, 2);
  assert.match(errors[0], /sync boom|quá 20ms/);
  assert.equal(await withTimeout(Promise.resolve(7), 10), 7);
});
