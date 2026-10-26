import test from "node:test";
import assert from "node:assert/strict";
import {
  DOCX_MIME,
  MediaDownloadError,
  allowedMediaHost,
  downloadAudio,
  downloadDocument,
  downloadImage,
  imageCandidates,
  linkHref,
  sniffAudio,
  sniffDocument,
  sniffImage,
} from "./dist/media.js";
import {
  KnownSenders,
  SeenIds,
  isTransientNetworkError,
  splitForZalo,
  userErrorReply,
} from "./dist/text.js";
import {
  FacebookPostBuffer,
  MAX_BUFFERED_MEDIA_BYTES,
  isPermanentPostError,
} from "./dist/facebook-buffer.js";
import { BridgeHttpError, callFileBridge, callImageBridge } from "./dist/bridge.js";
import { startControlServer } from "./dist/control-server.js";

const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 0]);

test("allowedMediaHost chỉ nhận domain Zalo, chặn domain giả", () => {
  assert.equal(allowedMediaHost("photo-stal.zadn.vn"), true);
  assert.equal(allowedMediaHost("zalo.me"), true);
  assert.equal(allowedMediaHost("evilzalo.me"), false);
  assert.equal(allowedMediaHost("zalo.me.evil.com"), false);
});

test("imageCandidates tìm URL trong JSON lồng và bỏ domain lạ", () => {
  const content = {
    title: "",
    params: JSON.stringify({ hd: "https://a.zdn.vn/x.jpg", other: "https://evil.com/y.jpg" }),
    href: "https://b.zadn.vn/z.jpg",
  };
  assert.deepEqual(imageCandidates(content).sort(), [
    "https://a.zdn.vn/x.jpg",
    "https://b.zadn.vn/z.jpg",
  ]);
});

test("sniffImage nhận diện theo magic bytes", () => {
  assert.equal(sniffImage(PNG), "image/png");
  assert.equal(sniffImage(new Uint8Array([0xff, 0xd8, 0xff, 0])), "image/jpeg");
  assert.equal(sniffImage(new Uint8Array([1, 2, 3])), null);
});

test("downloadImage chặn redirect ra ngoài Zalo", async () => {
  const fetchImpl = async () => {
    const r = new Response(PNG, { status: 200 });
    Object.defineProperty(r, "url", { value: "https://evil.com/x.png" });
    return r;
  };
  await assert.rejects(
    downloadImage(["https://a.zdn.vn/x.png"], { userAgent: "ua", fetchImpl }),
    /redirect ngoài Zalo/,
  );
});

test("downloadImage trả ảnh hợp lệ và gửi cookie", async () => {
  let cookieSent = "";
  const fetchImpl = async (_url, init) => {
    cookieSent = init.headers.cookie;
    return new Response(PNG, { status: 200, headers: { "content-type": "image/png" } });
  };
  const image = await downloadImage(["https://a.zdn.vn/x.png"], {
    userAgent: "ua",
    cookieFor: () => "sid=1",
    fetchImpl,
  });
  assert.equal(image.mime, "image/png");
  assert.equal(cookieSent, "sid=1");
});

test("splitForZalo không vượt giới hạn và không mất nội dung", () => {
  const text = Array.from({ length: 300 }, (_, i) => `dòng ${i}`).join("\n");
  const parts = splitForZalo(text, 200);
  assert.ok(parts.every((p) => p.length <= 200));
  assert.equal(parts.join("\n").replace(/\s+/g, ""), text.replace(/\s+/g, ""));
});

test("SeenIds chống trùng và giới hạn kích thước", () => {
  const seen = new SeenIds(2);
  assert.equal(seen.remember("a"), true);
  assert.equal(seen.remember("a"), false);
  seen.remember("b");
  seen.remember("c"); // đẩy "a" ra
  assert.equal(seen.remember("a"), true);
});

test("isTransientNetworkError nhận ECONNRESET trong cause", () => {
  assert.equal(isTransientNetworkError({ cause: { code: "ECONNRESET" } }), true);
  assert.equal(isTransientNetworkError(new Error("bad request")), false);
});

test("FacebookPostBuffer gom tin cùng người rồi gửi 1 lần", async () => {
  const stored = [];
  const buffer = new FacebookPostBuffer({
    store: async (post) => void stored.push(post),
    enqueue: (work) => work().then(() => undefined),
    windowMs: 20,
  });
  await buffer.add("g1", "u1", "An", "m1", "dòng 1", []);
  await buffer.add("g1", "u1", "An", "m2", "dòng 2", []);
  await new Promise((r) => setTimeout(r, 60));
  assert.equal(stored.length, 1);
  assert.deepEqual(stored[0].messageIds, ["m1", "m2"]);
  assert.equal(stored[0].text, "dòng 1\ndòng 2");
  assert.equal(buffer.size, 0);
});

test("FacebookPostBuffer giữ bài và thử lại khi gửi lỗi", async () => {
  let calls = 0;
  const buffer = new FacebookPostBuffer({
    store: async () => {
      calls++;
      if (calls === 1) throw new Error("backend down");
    },
    enqueue: (work) => work().then(() => undefined),
    windowMs: 10,
    retryMs: 20,
  });
  await buffer.add("g1", "u1", "An", "m1", "x", []);
  await new Promise((r) => setTimeout(r, 80));
  assert.equal(calls, 2);
  assert.equal(buffer.size, 0);
});

const IMG = { mime: "image/png", bytes: PNG };

async function collect(steps, windowMs = 20) {
  const stored = [];
  const buffer = new FacebookPostBuffer({
    store: async (post) => void stored.push(post),
    enqueue: (work) => work().then(() => undefined),
    windowMs,
  });
  for (const [id, text, media] of steps) await buffer.add("g", "u", "An", id, text, media);
  await new Promise((r) => setTimeout(r, windowMs * 4));
  return stored.map((p) => ({ ids: p.messageIds, text: p.text, photos: p.media.length }));
}

test("kiểu ảnh trước rồi caption: 2 sản phẩm liên tiếp tách thành 2 bài", async () => {
  const posts = await collect([
    ["a1", "", [IMG]],
    ["a2", "", [IMG]],
    ["a3", "Áo A 99k https://s.shopee.vn/a", []],
    ["b1", "", [IMG]],
    ["b2", "Quần B 129k https://s.shopee.vn/b", []],
  ]);
  assert.deepEqual(posts, [
    { ids: ["a1", "a2", "a3"], text: "Áo A 99k https://s.shopee.vn/a", photos: 2 },
    { ids: ["b1", "b2"], text: "Quần B 129k https://s.shopee.vn/b", photos: 1 },
  ]);
});

test("kiểu caption trước rồi ảnh: ảnh theo sau thuộc cùng bài, caption mới mở bài mới", async () => {
  const posts = await collect([
    ["a1", "Áo A 99k https://s.shopee.vn/a", []],
    ["a2", "", [IMG]],
    ["a3", "", [IMG]],
    ["b1", "Quần B 129k https://s.shopee.vn/b", []],
    ["b2", "", [IMG]],
  ]);
  assert.deepEqual(
    posts.map((p) => [p.ids, p.photos]),
    [
      [["a1", "a2", "a3"], 2],
      [["b1", "b2"], 1],
    ],
  );
});

test("hai tin chữ cùng có link Shopee là hai sản phẩm khác nhau", async () => {
  const posts = await collect([
    ["a", "Áo A https://s.shopee.vn/a", []],
    ["b", "Quần B https://s.shopee.vn/b", []],
  ]);
  assert.equal(posts.length, 2);
});

test("gửi bài cũ lỗi khi tách bài không làm mất tin mới", async () => {
  let calls = 0;
  const stored = [];
  const buffer = new FacebookPostBuffer({
    store: async (post) => {
      calls++;
      if (calls === 1) throw new Error("backend down");
      stored.push(post.messageIds);
    },
    enqueue: (work) => work().then(() => undefined),
    windowMs: 20,
    retryMs: 20,
  });
  await buffer.add("g", "u", "An", "a1", "Áo A https://s.shopee.vn/a", [IMG]);
  await buffer.add("g", "u", "An", "b1", "Quần B https://s.shopee.vn/b", [IMG]);
  await new Promise((r) => setTimeout(r, 120));
  assert.deepEqual(stored.sort(), [["a1"], ["b1"]]);
});

test("isPermanentPostError: 4xx là lỗi của bài (trừ 408/429), 5xx và lỗi mạng là tạm", () => {
  assert.equal(isPermanentPostError(new BridgeHttpError("FB", 413, "too large")), true);
  assert.equal(isPermanentPostError(new BridgeHttpError("FB", 400, "bad")), true);
  assert.equal(isPermanentPostError(new BridgeHttpError("FB", 429, "slow down")), false);
  assert.equal(isPermanentPostError(new BridgeHttpError("FB", 503, "down")), false);
  assert.equal(isPermanentPostError(new Error("fetch failed")), false);
});

test("bài bị 413 chỉ thử lại 1 lần rồi bỏ, không lặp mãi", async () => {
  let calls = 0;
  const buffer = new FacebookPostBuffer({
    store: async () => {
      calls++;
      throw new BridgeHttpError("Facebook group post", 413, "Media payload too large");
    },
    enqueue: (work) => work().then(() => undefined),
    windowMs: 10,
    retryMs: 15,
  });
  await buffer.add("g", "u", "An", "m1", "x", [IMG]);
  await new Promise((r) => setTimeout(r, 150));
  assert.equal(calls, 2);
  assert.equal(buffer.size, 0);
});

test("lỗi 5xx: thử lại có backoff tới trần số lần rồi bỏ", async () => {
  const at = [];
  const buffer = new FacebookPostBuffer({
    store: async () => {
      at.push(Date.now());
      throw new BridgeHttpError("Facebook group post", 500, "boom");
    },
    enqueue: (work) => work().then(() => undefined),
    windowMs: 5,
    retryMs: 10,
    maxAttempts: 4,
  });
  await buffer.add("g", "u", "An", "m1", "x", []);
  await new Promise((r) => setTimeout(r, 250));
  assert.equal(at.length, 4);
  assert.equal(buffer.size, 0);
  // Khoảng cách tăng dần (10, 20, 40ms) - cho phép sai số hẹn giờ.
  assert.ok(at[3] - at[2] > at[1] - at[0]);
});

test("bộ đệm đầy và bài cũ nhất gửi lỗi: bỏ bài cũ, tin mới không bị mất", async () => {
  const stored = [];
  const buffer = new FacebookPostBuffer({
    store: async (post) => {
      if (post.senderId === "u0") throw new BridgeHttpError("Facebook group post", 500, "boom");
      stored.push(post.senderId);
    },
    enqueue: (work) => work().then(() => undefined),
    windowMs: 60000,
  });
  for (let i = 0; i < 64; i++) await buffer.add("g", `u${i}`, "An", `m${i}`, `tin ${i}`, []);
  assert.equal(buffer.size, 64);
  await buffer.add("g", "u64", "An", "m64", "tin mới", []);
  assert.equal(buffer.size, 64);
  assert.deepEqual(stored, []);
  // Bài mới nằm trong bộ đệm, gửi được khi flush.
  assert.equal(await buffer.flush("g:u64"), true);
  assert.deepEqual(stored, ["u64"]);
});

test("trần media trong RAM là 6 MiB (heap Node ~96 MB, base64+JSON nhân đôi)", () => {
  assert.equal(MAX_BUFFERED_MEDIA_BYTES, 6 * 1024 * 1024);
});

test("ảnh một mình vượt trần media: bỏ ảnh (có log), giữ chữ; tin chỉ có ảnh thì bỏ hẳn", async () => {
  const stored = [];
  const logged = [];
  const originalError = console.error;
  console.error = (...args) => logged.push(args.join(" "));
  try {
    const buffer = new FacebookPostBuffer({
      store: async (post) => void stored.push(post),
      enqueue: (work) => work().then(() => undefined),
      windowMs: 10,
      maxBufferedMediaBytes: 10,
    });
    const big = { mime: "image/png", bytes: new Uint8Array(11) };
    await buffer.add("g", "u", "An", "m1", "", [big]);
    assert.equal(buffer.size, 0, "tin chỉ có ảnh quá lớn không được giữ");
    await buffer.add("g", "u", "An", "m2", "caption", [big]);
    await new Promise((r) => setTimeout(r, 40));
    assert.equal(stored.length, 1);
    assert.deepEqual(stored[0].messageIds, ["m2"]);
    assert.equal(stored[0].media.length, 0);
    assert.equal(logged.filter((line) => line.includes("vượt trần")).length, 2);
  } finally {
    console.error = originalError;
  }
});

test("KnownSenders chỉ nhớ người đã được trả lời, LRU có trần", () => {
  const known = new KnownSenders(2);
  assert.equal(known.has("a"), false);
  known.mark("a");
  known.mark("b");
  known.mark("a"); // dùng lại -> "b" thành cũ nhất
  known.mark("c");
  assert.equal(known.has("a"), true);
  assert.equal(known.has("b"), false);
  assert.equal(known.has("c"), true);
  assert.equal(known.size, 2);
  known.mark("");
  assert.equal(known.size, 2);
});

test("KnownSenders: Python báo paired=false (người lạ, đã bị /zaloxoa) thì quên ngay", () => {
  const known = new KnownSenders(10);
  known.mark("x");
  assert.equal(known.forget("x"), true);
  assert.equal(known.has("x"), false);
  assert.equal(known.forget("x"), false);
  // Theo trả lời của Python: false quên, true nhớ, thiếu (bản cache cũ) thì nhớ khi có trả lời.
  known.noteReply("a", true, false);
  assert.equal(known.has("a"), true);
  known.noteReply("a", false, true);
  assert.equal(known.has("a"), false, "paired=false thắng cả khi có chữ trả lời");
  known.noteReply("b", undefined, false);
  assert.equal(known.has("b"), false);
  known.noteReply("b", null, true);
  assert.equal(known.has("b"), true);
  known.noteReply("b", undefined, false);
  assert.equal(known.has("b"), true, "không có tin hiệu thì giữ nguyên");
});

test("control server: POST /known/forget {id} gọi forgetSender, body sai/quá lớn bị từ chối", async () => {
  const forgotten = [];
  const server = startControlServer(0, {
    status: () => ({}),
    startQr: async () => "idle",
    startPairing: () => null,
    logout: async () => undefined,
    forgetSender: (id) => {
      forgotten.push(id);
      return id === "z-member";
    },
  });
  try {
    if (!server.listening) await new Promise((resolve) => server.once("listening", resolve));
    const base = `http://127.0.0.1:${server.address().port}`;
    const post = (body) =>
      fetch(`${base}/known/forget`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body,
      });
    const ok = await post(JSON.stringify({ id: "z-member" }));
    assert.equal(ok.status, 200);
    assert.deepEqual(await ok.json(), { ok: true, forgotten: true });
    assert.deepEqual(await (await post(JSON.stringify({ id: "other" }))).json(), {
      ok: true,
      forgotten: false,
    });
    assert.equal((await post("không phải json")).status, 400);
    assert.equal((await post(JSON.stringify({ id: "" }))).status, 400);
    assert.equal((await post(JSON.stringify({ id: "x".repeat(5000) }))).status, 413);
    assert.deepEqual(forgotten, ["z-member", "other"]);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});

test("linkHref chỉ nhận tin chia sẻ link có href http(s)", () => {
  const content = {
    title: "xem bài này",
    href: "https://vnexpress.net/bai-viet-123.html",
    thumb: "https://photo-stal.zadn.vn/thumb.jpg",
  };
  assert.equal(linkHref("chat.recommended", content), "https://vnexpress.net/bai-viet-123.html");
  assert.equal(
    linkHref("webchat", { ...content, action: "recommened.link" }),
    "https://vnexpress.net/bai-viet-123.html",
  );
  // Tin ảnh cũng có href (link ảnh) nhưng không phải tin link.
  assert.equal(linkHref("chat.photo", content), "");
  assert.equal(linkHref("chat.recommended", { href: "javascript:alert(1)" }), "");
  assert.equal(linkHref("chat.recommended", "text"), "");
});

test("userErrorReply: chỉ chủ bot thấy chi tiết lỗi, người khác nhận câu cố định", () => {
  const error = new Error("Image prompt returned 500: {secret body} photo-stal.zadn.vn");
  const reply = "⚠️ Bot chưa xử lý được ảnh.";
  assert.equal(userErrorReply(error, "member", "owner", reply), reply);
  assert.equal(userErrorReply(error, "owner", "", reply), reply);
  const owner = userErrorReply(error, "owner", "owner", reply);
  assert.ok(owner.startsWith(reply) && owner.includes("Image prompt returned 500"));
  assert.ok(userErrorReply("lạ", "owner", "owner", reply).endsWith("🔎 lạ"));
});

const bytes = (text) => new Uint8Array([...text].map((c) => c.charCodeAt(0)));
const M4A = new Uint8Array([0, 0, 0, 0x20, ...bytes("ftypM4A "), 0, 0, 0, 0]);
const OGG = bytes("OggS\0\x02\0\0\0\0\0\0");
const ADTS = new Uint8Array([0xff, 0xf1, 0x50, 0x80, 0, 0x1f, 0xfc]);

test("sniffAudio nhận m4a/ogg/aac/mp3/wav, chặn trang lỗi HTML và AMR", () => {
  assert.equal(sniffAudio(M4A), "audio/mp4");
  assert.equal(sniffAudio(OGG), "audio/ogg");
  assert.equal(sniffAudio(ADTS), "audio/aac");
  assert.equal(sniffAudio(bytes("ID3\x04\0\0")), "audio/mpeg");
  assert.equal(sniffAudio(new Uint8Array([0xff, 0xfb, 0x90, 0x64])), "audio/mpeg");
  assert.equal(sniffAudio(bytes("RIFF\0\0\0\0WAVEfmt ")), "audio/wav");
  assert.equal(sniffAudio(bytes("<!DOCTYPE html>")), null);
  assert.equal(sniffAudio(bytes("#!AMR\n")), null);
  assert.equal(sniffAudio(PNG), null);
});

test("sniffDocument: nội dung phải khớp đuôi", () => {
  assert.equal(sniffDocument(bytes("%PDF-1.7 ..."), "pdf"), "application/pdf");
  assert.equal(sniffDocument(bytes("<html>"), "pdf"), null);
  assert.equal(sniffDocument(bytes("PK\x03\x04...."), "docx"), DOCX_MIME);
  assert.equal(sniffDocument(bytes("Ghi chú\nDòng 2"), "txt"), "text/plain");
  assert.equal(sniffDocument(bytes("# Tiêu đề"), "md"), "text/markdown");
  assert.equal(sniffDocument(bytes("ab\0cd"), "txt"), null, "file nhị phân đội lốt .txt");
  assert.equal(sniffDocument(new Uint8Array([0xff, 0xfe, 0x41, 0]), "txt"), "text/plain");
  assert.equal(sniffDocument(bytes("PK\x03\x04"), "zip"), null);
});

test("downloadAudio: URL đầu là trang lỗi thì thử URL kế, trả MIME theo magic bytes", async () => {
  const seen = [];
  const fetchImpl = async (url) => {
    seen.push(url);
    if (url.endsWith(".m4a")) {
      return new Response("<html>expired</html>", {
        status: 200,
        headers: { "content-type": "audio/mp4" },
      });
    }
    return new Response(ADTS, { status: 200, headers: { "content-type": "audio/aac" } });
  };
  const audio = await downloadAudio(
    ["https://voice-aac-dl.zdn.vn/a.m4a", "https://voice-aac-dl.zdn.vn/a.aac"],
    { userAgent: "ua", fetchImpl },
    1024,
  );
  assert.equal(audio.mime, "audio/aac");
  assert.equal(seen.length, 2);
});

test("downloadAudio: vượt trần dung lượng -> MediaDownloadError.tooLarge, chặn domain lạ", async () => {
  const big = async () =>
    new Response(new Uint8Array(2048), { status: 200, headers: { "content-length": "2048" } });
  const error = await downloadAudio(
    ["https://voice-aac-dl.zdn.vn/a.m4a"],
    { userAgent: "ua", fetchImpl: big },
    1024,
  ).catch((e) => e);
  assert.ok(error instanceof MediaDownloadError);
  assert.equal(error.tooLarge, true);
  // Không khai báo Content-Length: vẫn dừng khi đọc quá trần.
  const stream = async () => new Response(new Uint8Array(4096), { status: 200 });
  const streamed = await downloadAudio(
    ["https://voice-aac-dl.zdn.vn/a.m4a"],
    { userAgent: "ua", fetchImpl: stream },
    1024,
  ).catch((e) => e);
  assert.equal(streamed.tooLarge, true);
  let called = false;
  const blocked = await downloadAudio(
    ["https://evil.com/a.m4a"],
    {
      userAgent: "ua",
      fetchImpl: async () => {
        called = true;
        return new Response(M4A);
      },
    },
    1024,
  ).catch((e) => e);
  assert.equal(called, false);
  assert.match(blocked.message, /domain bị chặn/);
  assert.equal(blocked.tooLarge, false);
});

test("downloadDocument kiểm nội dung theo đuôi file", async () => {
  const fetchImpl = async () => new Response(bytes("%PDF-1.4 abc"), { status: 200 });
  const doc = await downloadDocument(
    ["https://f20-zfcloud.zdn.vn/a.pdf"],
    "pdf",
    { userAgent: "ua", fetchImpl },
    1024,
  );
  assert.equal(doc.mime, "application/pdf");
  await assert.rejects(
    downloadDocument(
      ["https://f20-zfcloud.zdn.vn/a.docx"],
      "docx",
      { userAgent: "ua", fetchImpl },
      1024,
    ),
    /Không tải được file từ Zalo/,
  );
});

test("downloadImage giữ câu lỗi cũ (ảnh)", async () => {
  const fetchImpl = async () =>
    new Response(bytes("not image"), { status: 200, headers: { "content-type": "text/html" } });
  await assert.rejects(
    downloadImage(["https://a.zdn.vn/x.png"], { userAgent: "ua", fetchImpl }),
    /Không tải được ảnh từ Zalo \(a\.zdn\.vn: MIME text\/html\)/,
  );
});

test("header tên file/caption cắt theo ký tự: emoji ở chỗ cắt không làm encodeURIComponent ném lỗi", async () => {
  const seen = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    seen.push({ url: String(url), headers: init.headers });
    return new Response(JSON.stringify({ messages: ["ok"] }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };
  const config = { bridgeBaseUrl: "http://bridge", bridgeSecret: "s", accountId: "acc" };
  // 199 ký tự ASCII + emoji: cắt 200 đơn vị UTF-16 sẽ để lại nửa cặp surrogate.
  const name = "a".repeat(199) + "😀b.pdf";
  const caption = "c".repeat(499) + "😀 đuôi";
  try {
    await callFileBridge(config, "u1", "m1", name, caption, "application/pdf", bytes("%PDF"));
    await callImageBridge(config, "u1", "m2", caption, "image/png", PNG);
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.equal(seen.length, 2);
  const file = seen[0].headers;
  assert.equal(decodeURIComponent(file["x-zalo-file-name"]), "a".repeat(199) + "😀");
  assert.equal(decodeURIComponent(file["x-zalo-caption"]), "c".repeat(499) + "😀");
  assert.equal(decodeURIComponent(seen[1].headers["x-zalo-caption"]), "c".repeat(499) + "😀");
});
