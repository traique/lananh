import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { storeFacebookGroupPost } from "./dist/bridge.js";

/** Python giả: nhận 1 request, trả 204 và giữ lại header + body thô để kiểm tra. */
async function fakePython() {
  const seen = [];
  const server = createServer((req, res) => {
    const chunks = [];
    req.on("data", (chunk) => chunks.push(chunk));
    req.on("end", () => {
      seen.push({ url: req.url, headers: req.headers, body: Buffer.concat(chunks) });
      res.statusCode = 204;
      res.end();
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address();
  return { seen, server, base: `http://127.0.0.1:${port}/internal/zalo` };
}

test("bài Facebook gửi sang Python dạng multipart: meta JSON + ảnh nhị phân", async () => {
  const python = await fakePython();
  try {
    const jpeg = new Uint8Array([0xff, 0xd8, 0xff, 1, 2, 3]);
    const png = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 9]);
    await storeFacebookGroupPost(
      { bridgeBaseUrl: python.base, accountId: "acc", bridgeSecret: "s3cr3t" },
      {
        groupId: "g1",
        messageIds: ["m1", "m2"],
        senderId: "u1",
        senderName: "Lan",
        text: "Nồi chiên 899k",
        media: [
          { mime: "image/jpeg", bytes: jpeg },
          { mime: "image/png", bytes: png },
        ],
      },
    );
    const [request] = python.seen;
    assert.equal(request.url, "/internal/zalo/facebook-group-post-multipart");
    assert.equal(request.headers["x-zalo-bridge-secret"], "s3cr3t");
    assert.match(request.headers["content-type"], /^multipart\/form-data; boundary=/);
    // Không còn base64: thân request chứa đúng bytes ảnh.
    assert.ok(!request.body.toString("latin1").includes(Buffer.from(jpeg).toString("base64")));

    const form = await new Response(request.body, {
      headers: { "content-type": request.headers["content-type"] },
    }).formData();
    assert.deepEqual(JSON.parse(form.get("meta")), {
      account_id: "acc",
      group_id: "g1",
      message_ids: ["m1", "m2"],
      sender_id: "u1",
      sender_name: "Lan",
      text: "Nồi chiên 899k",
    });
    const media = form.getAll("media");
    assert.deepEqual(
      media.map((file) => file.type),
      ["image/jpeg", "image/png"],
    );
    assert.deepEqual(new Uint8Array(await media[0].arrayBuffer()), jpeg);
    assert.deepEqual(new Uint8Array(await media[1].arrayBuffer()), png);
  } finally {
    python.server.close();
  }
});

test("Python trả lỗi thì báo BridgeHttpError kèm mã trạng thái (4xx bỏ, 5xx thử lại)", async () => {
  const server = createServer((req, res) => {
    req.resume();
    req.on("end", () => {
      res.statusCode = 413;
      res.end("Image too large");
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  try {
    const base = `http://127.0.0.1:${server.address().port}`;
    await assert.rejects(
      storeFacebookGroupPost(
        { bridgeBaseUrl: base, accountId: "acc", bridgeSecret: "s" },
        { groupId: "g", messageIds: ["m"], senderId: "u", senderName: "", text: "x", media: [] },
      ),
      (error) => error.status === 413,
    );
  } finally {
    server.close();
  }
});
