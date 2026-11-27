import test from "node:test";
import assert from "node:assert/strict";
import {
  BRIDGE_ERROR_REPLY,
  BUSY_REPLY,
  Dispatcher,
  IMAGE_ERROR_REPLY,
  PAIRED_REPLY,
} from "./dist/dispatcher.js";
import { FAST_LANE_TIMEOUT_MS } from "./dist/bridge.js";
import { BoundedSerialQueue, KeyedMutex, KeyedSerialQueue } from "./dist/limits.js";
import { MediaDownloadError } from "./dist/media.js";
import { GatewayStats } from "./dist/stats.js";

const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

/** Lời hứa mở bằng tay: giữ 1 lượt Python "đang chạy". */
function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** Tin riêng zca-js (ThreadType.User = 0). */
function direct(id, content, { sender = "u1", msgType = "webchat", quote } = {}) {
  return {
    type: 0,
    threadId: sender,
    data: {
      msgId: id,
      cliMsgId: `c-${id}`,
      uidFrom: sender,
      idTo: "bot",
      dName: `Tên ${sender}`,
      content,
      msgType,
      ts: "1700000000000",
      quote,
    },
  };
}

/** Tin nhóm zca-js (ThreadType.Group = 1); ts tính bằng giây như Zalo đôi khi gửi. */
function group(id, groupId, content, { sender = "member", msgType = "webchat", quote } = {}) {
  return {
    type: 1,
    threadId: groupId,
    data: {
      msgId: id,
      cliMsgId: `c-${id}`,
      uidFrom: sender,
      idTo: groupId,
      dName: "Thành viên",
      content,
      msgType,
      ts: "1700000000",
      quote,
    },
  };
}

function harness(options = {}) {
  const sent = [];
  const presence = [];
  const calls = {
    message: [],
    image: [],
    voice: [],
    file: [],
    notice: [],
    group: [],
    controller: [],
    states: [],
    facebook: [],
    downloads: [],
    refresh: 0,
  };
  let connected = true;
  let clock = 1_700_000_000_000;
  const api = {
    listener: { on() {}, start() {} },
    getOwnId: () => "bot",
    getContext: () => ({
      cookie: { serializeSync: () => [], getCookieStringSync: () => "zpw=1" },
      imei: "imei",
      userAgent: "ua-zalo",
    }),
    sendMessage: async (content, threadId, type) => {
      sent.push({ threadId, type, msg: content.msg, files: content.attachments?.length ?? 0 });
    },
    sendTypingEvent: async (threadId) => void presence.push(["typing", threadId]),
    sendSeenEvent: async (items) => void presence.push(["seen", items.length]),
    addReaction: async (icon, target) => void presence.push(["reaction", icon, target.data.msgId]),
  };
  const config = {
    controllerId: "",
    userAgent: "ua",
    bridgeBaseUrl: "http://bridge",
    bridgeSecret: "s",
    presenceEvents: false,
    voiceMaxBytes: 1024 * 1024,
    documentMaxBytes: 1024 * 1024,
    directDebounceMs: 0,
    directDebounceMaxMs: 0,
    directDebounceMaxMessages: 1,
    sendGapMinMs: 0,
    sendGapMaxMs: 0,
    ...options.config,
  };
  const groups = {
    summary: new Set(options.summaryGroups ?? []),
    facebook: new Set(options.facebookGroups ?? []),
  };
  const remembered = [];
  const seenIds = new Set();
  const h = {
    sent,
    presence,
    calls,
    config,
    stats: new GatewayStats(),
    /** Trả lời của Python cho POST /message (đổi được trong từng test). */
    reply: (m) => ({ messages: [`trả lời: ${m.text}`], paired: true }),
    // Như Python (POST /notice): người lạ chưa pair -> text null, paired false.
    notice: (sender, kind) =>
      sender === "stranger" ? { text: null, paired: false } : { text: `báo ${kind}`, paired: true },
    download: async () => ({ mime: "image/png", bytes: PNG }),
    disconnect: () => {
      connected = false;
    },
    advance: (ms) => {
      clock += ms;
    },
    remembered,
  };
  h.queues = {
    direct: new KeyedSerialQueue({
      concurrency: 1,
      perKeyCapacity: 8,
      capacity: 64,
      ...options.queue,
    }),
    summary: new BoundedSerialQueue(256),
    sendLock: new KeyedMutex(60000),
  };
  h.dispatcher = new Dispatcher({
    api: () => (connected ? api : null),
    bridge: {
      message: async (m, timeoutMs) => {
        calls.message.push({ ...m, timeoutMs });
        return h.reply(m);
      },
      image: async (sender, id, caption, mime, bytes) => {
        calls.image.push({ sender, id, caption, mime, size: bytes.length });
        return { messages: ["ảnh đẹp"], paired: true };
      },
      voice: async (sender, id, mime, bytes, durationMs) => {
        calls.voice.push({ sender, id, mime, size: bytes.length, durationMs });
        return { messages: ["Em nghe là: alo"], paired: true };
      },
      file: async (sender, id, name, caption, mime, bytes) => {
        calls.file.push({ sender, id, name, mime, size: bytes.length });
        return { messages: ["tóm tắt"], paired: true };
      },
      notice: async (sender, kind) => {
        calls.notice.push([sender, kind]);
        return h.notice(sender, kind);
      },
      storeGroupMessage: async (m) => void calls.group.push(m),
      saveController: async (id) => void calls.controller.push(id),
      reportState: (state, detail) => calls.states.push([state, detail]),
    },
    queues: h.queues,
    config,
    groups: {
      isSummary: (gid) => groups.summary.has(gid),
      isFacebook: (gid) => groups.facebook.has(gid),
      roleOf: async (gid, sender) => (sender === "boss" ? "owner" : "member"),
      refresh: async () => void calls.refresh++,
    },
    facebook: (msg) => calls.facebook.push(msg),
    stats: h.stats,
    seen: {
      remember: (id) => {
        remembered.push(id);
        if (seenIds.has(id)) return false;
        seenIds.add(id);
        return true;
      },
    },
    downloads: {
      image: async (urls) => {
        calls.downloads.push(["image", urls]);
        return h.download();
      },
      audio: async (urls) => {
        calls.downloads.push(["audio", urls]);
        return h.download();
      },
      document: async (urls, ext) => {
        calls.downloads.push(["document", urls, ext]);
        return h.download();
      },
    },
    sleep: async () => undefined,
    now: () => clock,
    pairingCode: () => "123456",
  });
  /** Chờ mọi việc của dispatcher xong (hàng đợi, làn nhanh, gửi tin). */
  h.settle = async () => {
    for (let i = 0; i < 200; i++) {
      await new Promise((r) => setImmediate(r));
      if (h.dispatcher.idle) return;
    }
    assert.fail("dispatcher chưa xong việc");
  };
  h.texts = () => sent.map((s) => s.msg);
  return h;
}

test("tin chữ: gửi Python đúng id/người, trả lời đúng thread, nhớ người đã pair, bỏ tin trùng", async () => {
  const h = harness();
  h.dispatcher.onMessage(direct("m1", "chào em"));
  await h.settle();
  assert.deepEqual(h.calls.message, [
    {
      senderId: "u1",
      senderName: "Tên u1",
      conversationId: "u1",
      messageId: "m1",
      text: "chào em",
      quoteText: "",
      timeoutMs: undefined,
    },
  ]);
  assert.deepEqual(h.sent, [{ threadId: "u1", type: 0, msg: "trả lời: chào em", files: 0 }]);
  assert.equal(h.dispatcher.isKnown("u1"), true);
  h.dispatcher.onMessage(direct("m1", "chào em"));
  h.dispatcher.onMessage({ ...direct("m9", "tin của chính bot"), isSelf: true });
  await h.settle();
  assert.equal(h.calls.message.length, 1, "tin trùng id / tin của bot bị bỏ");
  assert.equal(h.stats.get("direct_messages"), 1);
  assert.equal(h.stats.get("text_turns"), 1);
});

test("gộp tin chữ liên tiếp thành 1 lượt với id tin cuối, kèm chữ tin được trích dẫn", async () => {
  const h = harness({
    config: { directDebounceMs: 20, directDebounceMaxMs: 500, directDebounceMaxMessages: 4 },
  });
  h.dispatcher.onMessage(direct("m1", "em ơi"));
  h.dispatcher.onMessage(direct("m2", "mai trời mưa ko", { quote: { msg: "dự báo hôm qua" } }));
  h.dispatcher.onMessage(direct("m3", "ở Đà Lạt"));
  await new Promise((r) => setTimeout(r, 60));
  await h.settle();
  assert.equal(h.calls.message.length, 1);
  assert.equal(h.calls.message[0].messageId, "m3");
  assert.equal(h.calls.message[0].text, "em ơi\nmai trời mưa ko\nở Đà Lạt");
  assert.equal(h.calls.message[0].quoteText, "dự báo hôm qua");
  assert.equal(h.stats.get("merged_messages"), 2);
});

test("Python lỗi: người quen (đã pair/chủ bot) nhận câu báo sự cố, người lạ im lặng", async () => {
  const h = harness();
  h.reply = () => {
    throw new Error("Bridge returned 500");
  };
  h.dispatcher.onMessage(direct("m1", "alo", { sender: "stranger" }));
  await h.settle();
  assert.deepEqual(h.sent, [], "người lạ không biết đây là bot");
  // Người đã pair do Python gửi lúc gắn phiên (GET /known-senders).
  assert.equal(h.dispatcher.seedKnownSenders(["u2", ""]), 1);
  h.dispatcher.onMessage(direct("m2", "alo", { sender: "u2" }));
  h.config.controllerId = "owner";
  h.dispatcher.onMessage(direct("m3", "alo", { sender: "owner" }));
  await h.settle();
  assert.deepEqual(
    h.sent.map((s) => [s.threadId, s.msg]),
    [
      ["u2", BRIDGE_ERROR_REPLY],
      ["owner", BRIDGE_ERROR_REPLY],
    ],
  );
});

test("Python báo paired=false (vừa bị /zaloxoa): quên người gửi; forgetSender từ control server", async () => {
  const h = harness();
  h.dispatcher.seedKnownSenders(["u1", "u2"]);
  h.reply = () => ({ messages: [], paired: false });
  h.dispatcher.onMessage(direct("m1", "alo"));
  await h.settle();
  assert.equal(h.dispatcher.isKnown("u1"), false);
  assert.equal(h.dispatcher.forgetSender("u2"), true);
  assert.equal(h.dispatcher.forgetSender("u2"), false);
});

test("làn nhanh: lệnh chỉ đọc chạy ngoài hàng đợi khi lượt dài của chính người đó đang chạy", async () => {
  const h = harness();
  const long = deferred();
  h.reply = (m) =>
    m.text === "/help" ? { messages: ["danh sách lệnh"], paired: true } : long.promise;
  h.dispatcher.onMessage(direct("m1", "phân tích giúp em"));
  await new Promise((r) => setImmediate(r));
  h.dispatcher.onMessage(direct("m2", "/help"));
  for (let i = 0; i < 10; i++) await new Promise((r) => setImmediate(r));
  assert.deepEqual(
    h.calls.message.map((m) => [m.text, m.timeoutMs]),
    [
      ["phân tích giúp em", undefined],
      ["/help", FAST_LANE_TIMEOUT_MS],
    ],
  );
  assert.deepEqual(h.texts(), ["danh sách lệnh"], "trả lời /help trước lượt dài");
  long.resolve({ messages: ["kết quả"], paired: true });
  await h.settle();
  assert.deepEqual(h.texts(), ["danh sách lệnh", "kết quả"]);
  assert.equal(h.stats.get("fast_lane"), 1);
});

test("hàng đợi đầy: người quen nhận câu báo bận tối đa 1 lần/phút, người lạ không", async () => {
  const h = harness({ queue: { perKeyCapacity: 1 } });
  const hold = deferred();
  h.reply = () => hold.promise;
  h.dispatcher.seedKnownSenders(["u1"]);
  // Link (không gộp được như tin chữ thường): mỗi tin là 1 việc của hàng đợi.
  const link = (n) => `https://vnexpress.net/bai-${n}.html`;
  h.dispatcher.onMessage(direct("m1", "tin 1"));
  h.dispatcher.onMessage(direct("m2", link(2)));
  h.dispatcher.onMessage(direct("m3", link(3)));
  h.dispatcher.onMessage(direct("s1", link(1), { sender: "stranger" }));
  h.dispatcher.onMessage(direct("s2", link(2), { sender: "stranger" }));
  for (let i = 0; i < 10; i++) await new Promise((r) => setImmediate(r));
  assert.deepEqual(
    h.sent.map((s) => [s.threadId, s.msg]),
    [["u1", BUSY_REPLY]],
  );
  assert.equal(h.stats.get("busy_rejections"), 3);
  h.advance(61000);
  h.dispatcher.onMessage(direct("m4", link(4)));
  for (let i = 0; i < 10; i++) await new Promise((r) => setImmediate(r));
  assert.equal(h.texts().filter((t) => t === BUSY_REPLY).length, 2, "sau 1 phút báo lại");
  hold.resolve({ messages: [], paired: true });
  await h.settle();
});

test("ghép đôi /pair: đúng mã thì lưu controller rồi báo; sai mã/hết hạn đi đường thường", async () => {
  const h = harness();
  assert.deepEqual(h.dispatcher.startPairing(), {
    code: "123456",
    expiresAt: 1_700_000_000_000 + 300000,
  });
  h.dispatcher.onMessage(direct("m1", "/pair 000000", { sender: "a" }));
  await h.settle();
  assert.deepEqual(h.calls.controller, []);
  assert.equal(h.calls.message[0].text, "/pair 000000", "mã sai: Python xử lý như lệnh thường");
  h.dispatcher.onMessage(direct("m2", "/pair 123456", { sender: "a" }));
  await h.settle();
  assert.deepEqual(h.calls.controller, ["a"]);
  assert.equal(h.config.controllerId, "a");
  assert.equal(h.texts().at(-1), PAIRED_REPLY);
  // Mã dùng 1 lần.
  h.dispatcher.onMessage(direct("m3", "/pair 123456", { sender: "b" }));
  await h.settle();
  assert.deepEqual(h.calls.controller, ["a"]);
  // Hết hạn sau 5 phút.
  h.dispatcher.startPairing();
  h.advance(300001);
  h.dispatcher.onMessage(direct("m4", "/pair 123456", { sender: "c" }));
  await h.settle();
  assert.deepEqual(h.calls.controller, ["a"]);
  h.dispatcher.startPairing();
  h.dispatcher.clearPairing();
  h.dispatcher.onMessage(direct("m5", "/pair 123456", { sender: "d" }));
  await h.settle();
  assert.deepEqual(h.calls.controller, ["a"], "đăng xuất bỏ mã đang chờ");
  h.disconnect();
  assert.equal(h.dispatcher.startPairing(), null, "Zalo chưa kết nối");
});

test("tin nhóm: chỉ nhóm đang theo dõi được ghi nhớ id; lưu kèm vai trò, trích dẫn, giờ ms", async () => {
  const h = harness({ summaryGroups: ["g-stock"], facebookGroups: ["g-shop"] });
  for (let i = 0; i < 20; i++) h.dispatcher.onMessage(group(`x${i}`, "g-other", "tin nhóm lạ"));
  assert.deepEqual(h.remembered, [], "nhóm không theo dõi không đẩy id tin riêng ra khỏi LRU");
  h.dispatcher.onMessage(
    group("g1", "g-stock", "HPG múc được", { sender: "boss", quote: { msg: "mã nào" } }),
  );
  h.dispatcher.onMessage(group("g1", "g-stock", "HPG múc được", { sender: "boss" }));
  h.dispatcher.onMessage(group("g2", "g-shop", "áo thun 99k"));
  await h.settle();
  assert.deepEqual(h.remembered, ["g1", "g1", "g2"]);
  assert.deepEqual(h.calls.group, [
    {
      groupId: "g-stock",
      messageId: "g1",
      senderId: "boss",
      senderName: "Thành viên",
      text: "HPG múc được",
      sentAtMs: 1_700_000_000_000,
      quoteText: "mã nào",
      senderRole: "owner",
    },
  ]);
  assert.deepEqual(
    h.calls.facebook.map((m) => [m.threadId, m.id, m.text]),
    [["g-shop", "g2", "áo thun 99k"]],
  );
  assert.equal(h.stats.get("group_messages"), 1);
  assert.equal(h.stats.get("direct_messages"), 0);
});

test("ảnh: báo đã nhận trước, gửi Python đúng bytes; lỗi thì người quen nhận câu lỗi ảnh", async () => {
  const h = harness();
  const photo = { title: "", href: "https://photo-stal.zdn.vn/a.jpg" };
  h.dispatcher.onMessage(direct("p1", photo, { msgType: "chat.photo" }));
  await h.settle();
  assert.deepEqual(h.calls.notice, [["u1", "image"]]);
  assert.deepEqual(h.calls.image, [
    { sender: "u1", id: "p1", caption: "", mime: "image/png", size: PNG.length },
  ]);
  assert.deepEqual(h.texts(), ["báo image", "ảnh đẹp"]);
  h.download = async () => {
    throw new Error("Không tải được ảnh từ Zalo (photo-stal.zdn.vn: HTTP 404)");
  };
  h.dispatcher.onMessage(direct("p2", photo, { msgType: "chat.photo" }));
  h.dispatcher.onMessage(direct("p3", photo, { msgType: "chat.photo", sender: "stranger" }));
  await h.settle();
  assert.equal(h.calls.notice.length, 2, "câu 'đã nhận ảnh' tối đa 1 lần/phút mỗi người");
  assert.deepEqual(h.texts().slice(2), [IMAGE_ERROR_REPLY], "người lạ im lặng");
});

test("tin thoại: gửi Python kèm thời lượng; tải lỗi/quá lớn thì báo đúng loại", async () => {
  const h = harness();
  const voice = {
    href: "https://voice-aac-dl.zdn.vn/v.aac",
    params: JSON.stringify({ m4a: "https://voice-aac-dl.zdn.vn/v.m4a", duration: 3200 }),
  };
  h.download = async () => ({ mime: "audio/mp4", bytes: new Uint8Array(12) });
  h.dispatcher.onMessage(direct("v1", voice, { msgType: "chat.voice" }));
  await h.settle();
  assert.deepEqual(h.calls.voice, [
    { sender: "u1", id: "v1", mime: "audio/mp4", size: 12, durationMs: 3200 },
  ]);
  assert.equal(h.calls.downloads[0][1][0], "https://voice-aac-dl.zdn.vn/v.m4a", "m4a trước");
  h.download = async () => {
    throw new MediaDownloadError("Ghi âm lớn hơn 1 MB", true);
  };
  h.dispatcher.onMessage(direct("v2", voice, { msgType: "chat.voice" }));
  await h.settle();
  assert.deepEqual(h.calls.notice.at(-1), ["u1", "voice_too_large"]);
  assert.equal(h.calls.downloads.length, 2, "quá trần: không thử tải lại");
});

test("file: khai báo quá trần thì báo ngay không tải; PDF hợp lệ gửi Python để tóm tắt", async () => {
  const h = harness();
  const file = (size) => ({
    title: "bao-cao.pdf",
    href: "https://f20-zfcloud.zdn.vn/x.pdf",
    params: JSON.stringify({ fileExt: "pdf", fileSize: size }),
  });
  h.dispatcher.onMessage(direct("f1", file(50 * 1024 * 1024), { msgType: "share.file" }));
  await h.settle();
  assert.deepEqual(h.calls.notice, [["u1", "file_too_large"]]);
  assert.deepEqual(h.calls.downloads, []);
  h.download = async () => ({ mime: "application/pdf", bytes: new Uint8Array(5) });
  h.dispatcher.onMessage(direct("f2", file(5), { msgType: "share.file" }));
  await h.settle();
  assert.deepEqual(h.calls.file, [
    { sender: "u1", id: "f2", name: "bao-cao.pdf", mime: "application/pdf", size: 5 },
  ]);
  assert.equal(h.texts().at(-1), "tóm tắt");
});

test("video/vị trí: câu 'chưa hỗ trợ' tối đa 1 lần/phút mỗi người mỗi loại; sticker im lặng", async () => {
  const h = harness();
  const video = { title: "", href: "https://video.zdn.vn/v.mp4" };
  h.dispatcher.onMessage(direct("v1", video, { msgType: "chat.video.msg" }));
  h.dispatcher.onMessage(direct("v2", video, { msgType: "chat.video.msg" }));
  h.dispatcher.onMessage(direct("s1", { id: 1 }, { msgType: "chat.sticker" }));
  await h.settle();
  assert.deepEqual(h.calls.notice, [["u1", "video"]]);
  assert.deepEqual(h.texts(), ["báo video"]);
  assert.deepEqual(h.calls.message, []);
  assert.equal(h.stats.get("direct_messages"), 2, "sticker không tính");
});

test("lệnh nhóm gõ từ Zalo (/themnhom...): nạp lại danh sách nhóm sau khi Python trả lời", async () => {
  const h = harness();
  h.dispatcher.onMessage(direct("m1", "/themnhom 2 sale"));
  await h.settle();
  assert.equal(h.calls.refresh, 1);
  h.dispatcher.onMessage(direct("m2", "/nhom"));
  await h.settle();
  assert.equal(h.calls.refresh, 1);
});

test("typing/đã xem chỉ cho người quen; reaction 👌 khi tin phải chờ lượt trước", async () => {
  const h = harness({ config: { presenceEvents: true } });
  const hold = deferred();
  h.reply = () => hold.promise;
  h.dispatcher.onMessage(direct("s1", "alo", { sender: "stranger" }));
  await new Promise((r) => setImmediate(r));
  assert.deepEqual(h.presence, [], "người lạ không thấy dấu hiệu bot đã đọc");
  hold.resolve({ messages: [], paired: false });
  await h.settle();
  const next = deferred();
  h.reply = () => next.promise;
  h.dispatcher.seedKnownSenders(["u1"]);
  h.dispatcher.onMessage(direct("m1", "câu dài"));
  for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r));
  h.dispatcher.onMessage(direct("m2", "https://vnexpress.net/bai-1.html"));
  for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r));
  assert.deepEqual(h.presence, [
    ["seen", 1],
    ["typing", "u1"],
    ["reaction", "/-ok", "m2"],
  ]);
  next.resolve({ messages: ["xong"], paired: true });
  await h.settle();
});

test("close() lúc tắt: tin chữ đang chờ gộp vẫn tới Python, tin tới sau bị bỏ", async () => {
  const h = harness({
    config: { directDebounceMs: 5000, directDebounceMaxMs: 10000, directDebounceMaxMessages: 4 },
  });
  h.dispatcher.onMessage(direct("m1", "nhắc em mai 8h"));
  await new Promise((r) => setImmediate(r));
  assert.equal(h.calls.message.length, 0, "đang chờ debounce 5s");
  assert.equal(h.dispatcher.idle, false);
  h.dispatcher.close();
  h.dispatcher.onMessage(direct("m2", "tin tới sau khi tắt"));
  await h.settle();
  assert.deepEqual(
    h.calls.message.map((m) => m.text),
    ["nhắc em mai 8h"],
  );
  assert.equal(h.dispatcher.idle, true);
});

test("gửi trả lời lỗi hẳn: báo Python send_failed, không ném", async () => {
  const h = harness();
  h.disconnect();
  h.dispatcher.routeDirect({
    text: "chào",
    caption: "",
    urls: [],
    link: "",
    sender: "u1",
    senderName: "An",
    id: "m1",
    threadId: "u1",
    msgType: "webchat",
    kind: "text",
    quoteText: "",
    seen: null,
    voiceUrls: [],
    durationMs: null,
    file: null,
    fileUrls: [],
  });
  await h.settle();
  assert.equal(h.calls.message.length, 1);
  assert.equal(h.calls.states.length, 1);
  assert.equal(h.calls.states[0][0], "send_failed");
  assert.match(h.calls.states[0][1], /tin m1: Zalo is not connected/);
  assert.equal(h.stats.get("send_failures"), 1);
});
