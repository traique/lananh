import test from "node:test";
import assert from "node:assert/strict";
import { allowedMediaHost, downloadImage, imageCandidates, sniffImage } from "./dist/media.js";
import { SeenIds, isTransientNetworkError, splitForZalo } from "./dist/text.js";
import { FacebookPostBuffer } from "./dist/facebook-buffer.js";

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
