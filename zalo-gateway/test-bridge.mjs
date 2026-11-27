import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import {
  BRIDGE_ATTEMPTS,
  BRIDGE_RETRY_MAX_DELAY_MS,
  BridgeHttpError,
  MAX_KNOWN_SENDERS,
  callBridge,
  callImageBridge,
  fetchBridge,
  fetchKnownSenders,
  retryAfterMs,
} from "./dist/bridge.js";

const refused = () =>
  Object.assign(new TypeError("fetch failed"), {
    cause: Object.assign(new Error("connect ECONNREFUSED 127.0.0.1:8000"), {
      code: "ECONNREFUSED",
    }),
  });

/** fetch giả: trả lần lượt theo `script` (Response hoặc lỗi để ném). */
function scripted(script) {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, init });
    const next = script.shift();
    if (next instanceof Error) throw next;
    return next;
  };
  return { calls, fetchImpl };
}

const busy = (retryAfter) =>
  new Response("busy", {
    status: 503,
    headers: retryAfter === undefined ? {} : { "retry-after": retryAfter },
  });
const json = (body, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });

/** Máy chủ thật trên loopback cho các test cần fetch thật (Blob, header). */
async function withServer(handler, run) {
  const server = createServer(handler);
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  try {
    return await run(`http://127.0.0.1:${server.address().port}`);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
}

test("retryAfterMs: số giây, HTTP-date, rỗng/hỏng", () => {
  assert.equal(retryAfterMs("5"), 5000);
  assert.equal(retryAfterMs(" 0 "), 0);
  const now = Date.parse("2026-11-27T10:00:00Z");
  assert.equal(retryAfterMs("Fri, 27 Nov 2026 10:00:03 GMT", now), 3000);
  assert.equal(retryAfterMs("Fri, 27 Nov 2026 09:00:00 GMT", now), 0, "mốc đã qua");
  assert.equal(retryAfterMs(null), null);
  assert.equal(retryAfterMs("sớm thôi"), null);
});

test("fetchBridge: 503 có Retry-After thì chờ đúng rồi thử lại, thành công thì trả ngay", async () => {
  const { calls, fetchImpl } = scripted([busy("2"), json({ messages: ["ok"] })]);
  const slept = [];
  const logs = [];
  const response = await fetchBridge(
    "http://bridge/message",
    { method: "POST" },
    240000,
    "Bridge",
    {
      fetchImpl,
      sleep: async (ms) => void slept.push(ms),
      log: (line) => logs.push(line),
    },
  );
  assert.equal(response.status, 200);
  assert.equal(calls.length, 2);
  assert.deepEqual(slept, [2000]);
  assert.ok(calls.every((call) => call.init.signal instanceof AbortSignal));
  assert.equal(calls[0].init.signal, calls[1].init.signal, "chung 1 hạn chót cho mọi lần thử");
  assert.match(logs[0], /bận \(503\).*lần 2\/3/);
});

test("fetchBridge: Python từ chối kết nối (đang khởi động lại) -> chờ 2s, 4s; Retry-After có trần", async () => {
  const { calls, fetchImpl } = scripted([refused(), busy("120"), json({ messages: [] })]);
  const slept = [];
  const response = await fetchBridge("http://bridge/voice", {}, 240000, "Voice bridge", {
    fetchImpl,
    sleep: async (ms) => void slept.push(ms),
    log: () => undefined,
  });
  assert.equal(response.status, 200);
  assert.equal(calls.length, 3);
  assert.deepEqual(slept, [2000, BRIDGE_RETRY_MAX_DELAY_MS]);
});

test("fetchBridge: hết lượt thử thì trả 503 cuối / ném ECONNREFUSED cuối", async () => {
  assert.equal(BRIDGE_ATTEMPTS, 3);
  const quiet = { sleep: async () => undefined, log: () => undefined };
  const busyAll = scripted([busy("1"), busy("1"), busy("1"), json({})]);
  const last = await fetchBridge("http://b", {}, 60000, "Bridge", {
    ...quiet,
    fetchImpl: busyAll.fetchImpl,
  });
  assert.equal(last.status, 503);
  assert.equal(busyAll.calls.length, 3);
  const down = scripted([refused(), refused(), refused()]);
  await assert.rejects(
    fetchBridge("http://b", {}, 60000, "Bridge", { ...quiet, fetchImpl: down.fetchImpl }),
    (error) => error.cause?.code === "ECONNREFUSED",
  );
  assert.equal(down.calls.length, 3);
});

test("fetchBridge: lỗi khác 503/ECONNREFUSED không thử lại", async () => {
  const quiet = { sleep: async () => assert.fail("không được chờ"), log: () => undefined };
  const reset = Object.assign(new TypeError("fetch failed"), { cause: { code: "ECONNRESET" } });
  const failing = scripted([reset]);
  await assert.rejects(
    fetchBridge("http://b", {}, 60000, "Bridge", { ...quiet, fetchImpl: failing.fetchImpl }),
    /fetch failed/,
  );
  assert.equal(failing.calls.length, 1);
  const serverError = scripted([json({ detail: "boom" }, 500)]);
  const response = await fetchBridge("http://b", {}, 60000, "Bridge", {
    ...quiet,
    fetchImpl: serverError.fetchImpl,
  });
  assert.equal(response.status, 500);
  assert.equal(serverError.calls.length, 1);
});

test("fetchBridge: không thử lại nếu lần chờ vượt hạn chót của lượt", async () => {
  let clock = 0;
  const { calls, fetchImpl } = scripted([busy("5"), json({})]);
  const response = await fetchBridge("http://b", {}, 4000, "Bridge", {
    fetchImpl,
    now: () => clock,
    sleep: async (ms) => {
      clock += ms;
    },
    log: () => undefined,
  });
  assert.equal(response.status, 503, "chờ 5s vượt hạn 4s: trả luôn 503");
  assert.equal(calls.length, 1);
});

test("callBridge thử lại khi Python bận rồi ném BridgeHttpError nếu vẫn 503", async () => {
  let hits = 0;
  await withServer(
    (req, res) => {
      hits++;
      res.statusCode = 503;
      res.setHeader("retry-after", "0");
      res.end("Media worker busy");
    },
    async (base) => {
      const config = { bridgeBaseUrl: base, bridgeSecret: "s", accountId: "acc" };
      const message = {
        senderId: "u",
        senderName: "An",
        conversationId: "u",
        messageId: "m1",
        text: "chào",
      };
      const error = await callBridge(config, message, 5000).catch((e) => e);
      assert.ok(error instanceof BridgeHttpError);
      assert.equal(error.status, 503);
    },
  );
  assert.equal(hits, 3);
});

test("callImageBridge: body Blob gửi lại nguyên vẹn khi Python bận, kèm Content-Length", async () => {
  const seen = [];
  await withServer(
    (req, res) => {
      const chunks = [];
      req.on("data", (chunk) => chunks.push(chunk));
      req.on("end", () => {
        seen.push({
          length: req.headers["content-length"],
          type: req.headers["content-type"],
          secret: req.headers["x-zalo-bridge-secret"],
          body: Buffer.concat(chunks),
        });
        if (seen.length === 1) {
          res.statusCode = 503;
          res.setHeader("retry-after", "0");
          res.end("busy");
          return;
        }
        res.setHeader("content-type", "application/json");
        res.end(JSON.stringify({ messages: ["đẹp quá"] }));
      });
    },
    async (base) => {
      const config = { bridgeBaseUrl: base, bridgeSecret: "s", accountId: "acc" };
      const image = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 1, 2, 3, 4]);
      const reply = await callImageBridge(config, "u1", "m1", "ảnh", "image/png", image);
      assert.deepEqual(reply.messages, ["đẹp quá"]);
    },
  );
  assert.equal(seen.length, 2);
  for (const request of seen) {
    assert.equal(request.length, "8");
    assert.equal(request.type, "image/png");
    assert.equal(request.secret, "s");
    assert.deepEqual([...request.body], [0x89, 0x50, 0x4e, 0x47, 1, 2, 3, 4]);
  }
});

test("fetchKnownSenders: gọi đúng route kèm secret, lọc id hỏng, có trần", async () => {
  let request = null;
  const many = Array.from({ length: MAX_KNOWN_SENDERS + 20 }, (_, i) => `z${i}`);
  await withServer(
    (req, res) => {
      request = { url: req.url, secret: req.headers["x-zalo-bridge-secret"] };
      res.setHeader("content-type", "application/json");
      const ids = req.url.endsWith("/many") ? many : [" z1 ", 42, "", null, { id: "x" }, "z2"];
      res.end(JSON.stringify({ sender_ids: ids }));
    },
    async (base) => {
      const config = { bridgeBaseUrl: base, bridgeSecret: "s", accountId: "acc 1" };
      assert.deepEqual(await fetchKnownSenders(config), ["z1", "42", "z2"]);
      assert.deepEqual(request, { url: "/known-senders/acc%201", secret: "s" });
      const capped = await fetchKnownSenders({ ...config, accountId: "many" });
      assert.equal(capped.length, MAX_KNOWN_SENDERS);
    },
  );
});

test("fetchKnownSenders: Python lỗi thì ném (nơi gọi bỏ qua)", async () => {
  await withServer(
    (req, res) => {
      res.statusCode = 404;
      res.end("not found");
    },
    async (base) => {
      const config = { bridgeBaseUrl: base, bridgeSecret: "s", accountId: "acc" };
      await assert.rejects(fetchKnownSenders(config), (error) => error.status === 404);
    },
  );
});
