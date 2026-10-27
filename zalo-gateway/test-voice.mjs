import test from "node:test";
import assert from "node:assert/strict";
import { parseVoice, sendVoiceReply } from "./dist/voice.js";

const VOICE = {
  path: "tts/abcDEF123456.1793000000.sigsigsigsigsigsig1234.m4a",
  url: "https://bot.example/media/tts/abcDEF123456.1793000000.sigsigsigsigsigsig1234.m4a",
  duration_ms: 4200,
  mime: "audio/mp4",
};

function fakeFetch(status = 200, body = new Uint8Array([1, 2, 3, 4])) {
  const calls = [];
  const impl = async (url, init) => {
    calls.push({ url, init });
    return new Response(status === 200 ? body : "nope", { status });
  };
  return { calls, impl };
}

function fakeClient({ uploadFails = false, sendFails = 0, withUpload = true } = {}) {
  const calls = [];
  let failures = sendFails;
  const client = {
    calls,
    async sendVoice(options, target, type) {
      calls.push(["sendVoice", options.voiceUrl, target, type]);
      if (failures-- > 0) throw new Error("Zalo từ chối");
      return { msgId: "1" };
    },
  };
  if (withUpload) {
    client.uploadAttachment = async (sources, target, type) => {
      calls.push([
        "upload",
        sources[0].filename.endsWith(".m4a"),
        sources[0].data.length,
        target,
        type,
      ]);
      if (uploadFails) throw new Error("upload hỏng");
      return [{ fileType: "others", fileUrl: "https://f.zdn.vn/voice.m4a" }];
    };
  }
  return client;
}

const deps = (fetchImpl) => ({
  bridgeBaseUrl: "http://127.0.0.1:8000/internal/zalo",
  bridgeSecret: "s3cret",
  threadType: 0,
  fetchImpl,
  timeoutMs: 2000,
  log: () => undefined,
});

test("parseVoice: chỉ nhận path tts/<tên> và url https", () => {
  assert.deepEqual(parseVoice(VOICE), VOICE);
  assert.equal(parseVoice(null), null);
  assert.equal(parseVoice({ path: "../session" }), null);
  assert.equal(parseVoice({ path: "tts/a/b" }), null);
  assert.equal(parseVoice({ ...VOICE, url: "http://insecure/x" }).url, null);
});

test("sendVoiceReply: tải qua loopback, lên CDN Zalo rồi sendVoice link CDN", async () => {
  const fetch = fakeFetch();
  const client = fakeClient();
  assert.equal(await sendVoiceReply(client, "u1", VOICE, deps(fetch.impl)), "cdn");
  assert.equal(fetch.calls[0].url, `http://127.0.0.1:8000/internal/zalo/${VOICE.path}`);
  assert.equal(fetch.calls[0].init.headers["x-zalo-bridge-secret"], "s3cret");
  assert.deepEqual(client.calls, [
    ["upload", true, 4, "u1", 0],
    ["sendVoice", "https://f.zdn.vn/voice.m4a", "u1", 0],
  ]);
});

test("sendVoiceReply: tải lên lỗi thì sendVoice link công khai có ký", async () => {
  const client = fakeClient({ uploadFails: true });
  assert.equal(await sendVoiceReply(client, "u1", VOICE, deps(fakeFetch().impl)), "url");
  assert.deepEqual(client.calls.at(-1), ["sendVoice", VOICE.url, "u1", 0]);
});

test("sendVoiceReply: clip hết hạn (404) và không có link công khai -> bỏ, không ném", async () => {
  const client = fakeClient();
  const result = await sendVoiceReply(
    client,
    "u1",
    { ...VOICE, url: null },
    deps(fakeFetch(404).impl),
  );
  assert.equal(result, null);
  assert.deepEqual(client.calls, []);
});

test("sendVoiceReply: sendVoice lỗi cả 2 đường -> null", async () => {
  const client = fakeClient({ sendFails: 2 });
  assert.equal(await sendVoiceReply(client, "u1", VOICE, deps(fakeFetch().impl)), null);
  assert.equal(client.calls.filter((c) => c[0] === "sendVoice").length, 2);
});

test("sendVoiceReply: zca-js không có sendVoice -> bỏ", async () => {
  assert.equal(await sendVoiceReply({}, "u1", VOICE, deps(fakeFetch().impl)), null);
});
