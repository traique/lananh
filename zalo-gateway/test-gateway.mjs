import test from "node:test";
import assert from "node:assert/strict";
import {
  allowedMediaHost,
  downloadImage,
  imageCandidates,
  linkHref,
  sniffImage,
} from "./dist/media.js";
import { KnownSenders, SeenIds, isTransientNetworkError, splitForZalo } from "./dist/text.js";
import {
  FacebookPostBuffer,
  MAX_BUFFERED_MEDIA_BYTES,
  isPermanentPostError,
} from "./dist/facebook-buffer.js";
import { BridgeHttpError } from "./dist/bridge.js";

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
