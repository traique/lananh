import test from "node:test";
import assert from "node:assert/strict";
import {
  classifyDirect,
  directRoute,
  fileCandidates,
  fileInfo,
  parseMessage,
  quoteText,
  seenParams,
  voiceCandidates,
  voiceDurationMs,
} from "./dist/msgtype.js";
import {
  isCancelCommand,
  isFastLaneCommand,
  isReadonlyCommand,
  parseCommand,
} from "./dist/commands.js";
import { LOCAL_NOTICES, noticeText } from "./dist/notices.js";

// Fixture: tin riêng zca-js 2.1.2 (dist/models/Message.d.ts TMessage) - các trường
// sendSeenEvent cần đều có mặt.
function zaloMessage(msgType, content, extra = {}) {
  return {
    type: 0,
    threadId: "u1",
    isSelf: false,
    data: {
      msgId: "7001",
      cliMsgId: "9001",
      msgType,
      uidFrom: "u1",
      idTo: "bot",
      dName: "Lan",
      ts: "1761400000000",
      st: 3,
      at: 5,
      cmd: 501,
      content,
      ...extra,
    },
  };
}

const PHOTO = {
  title: "",
  description: "",
  href: "https://photo-stal.zadn.vn/a/photo.jpg",
  thumb: "https://photo-stal.zadn.vn/a/thumb.jpg",
  params: JSON.stringify({ hd: "https://photo-stal.zadn.vn/a/hd.jpg" }),
};
const VOICE = {
  title: "",
  description: "",
  href: "https://voice-aac-dl.zdn.vn/v/abc.aac",
  thumb: "",
  params: JSON.stringify({ duration: 3500, m4aUrl: "https://voice-aac-dl.zdn.vn/v/abc.m4a" }),
};
const VIDEO = {
  title: "",
  href: "https://video-dl.zdn.vn/v/clip.mp4",
  thumb: "https://photo-stal.zadn.vn/v/clip.jpg",
  params: JSON.stringify({ duration: 12000, video_width: 720 }),
};
const fileContent = (title, ext, size = 12345) => ({
  title,
  href: `https://f20-zfcloud.zdn.vn/file/${title}`,
  thumb: "https://photo-stal.zadn.vn/icon/file.png",
  params: JSON.stringify({ fileSize: String(size), fileExt: ext, checksum: "x" }),
});

test("msgType: ảnh/ảnh vẽ/file ảnh vẫn đi đường ảnh", () => {
  assert.equal(classifyDirect("chat.photo", PHOTO), "image");
  assert.equal(classifyDirect("chat.doodle", PHOTO), "image");
  assert.equal(classifyDirect("share.file", fileContent("anh.JPG", "")), "image");
  assert.equal(classifyDirect("share.file", fileContent("x", "webp")), "image");
});

test("msgType: tin thoại, video, vị trí, sticker, gif không vào đường ảnh", () => {
  assert.equal(classifyDirect("chat.voice", VOICE), "voice");
  assert.equal(classifyDirect("chat.video.msg", VIDEO), "video");
  assert.equal(classifyDirect("chat.location.new", { title: "Hồ Gươm", href: "" }), "location");
  assert.equal(classifyDirect("chat.sticker", { id: 1, catId: 2, type: 7 }), "silent");
  assert.equal(classifyDirect("chat.gif", PHOTO), "silent");
});

test("msgType: file PDF/Word/TXT -> file; zip/xlsx -> file chưa hỗ trợ", () => {
  assert.equal(classifyDirect("share.file", fileContent("hop-dong.pdf", "pdf")), "file");
  assert.equal(classifyDirect("share.file", fileContent("bao-cao.docx", "")), "file");
  assert.equal(classifyDirect("share.file", fileContent("ghi-chu.TXT", "")), "file");
  assert.equal(classifyDirect("share.file", fileContent("du-lieu.zip", "zip")), "file_unsupported");
  assert.equal(classifyDirect("share.file", fileContent("bang.xlsx", "")), "file_unsupported");
});

test("msgType lạ: chỉ lấy title làm chữ, không coi URL CDN là ảnh", () => {
  const msg = parseMessage(zaloMessage("chat.todo", { title: "việc cần làm", href: PHOTO.href }));
  assert.equal(msg.kind, "text");
  assert.equal(msg.text, "việc cần làm");
  assert.equal(directRoute(msg), "batch");
  // Nhóm/Facebook vẫn thấy đủ URL như trước.
  assert.deepEqual(msg.urls, [PHOTO.href]);
  const empty = parseMessage(zaloMessage("chat.todo", { title: "", href: PHOTO.href }));
  assert.equal(directRoute(empty), "drop");
});

test("tin chia sẻ link vẫn ưu tiên nhánh link trước msgType", () => {
  const msg = parseMessage(
    zaloMessage("chat.recommended", {
      title: "Bài hay",
      href: "https://vnexpress.net/a.html",
      thumb: "https://photo-stal.zadn.vn/t.jpg",
    }),
  );
  assert.equal(msg.kind, "link");
  assert.equal(msg.link, "https://vnexpress.net/a.html");
  assert.equal(directRoute(msg), "queue");
});

test("tin thoại: ưu tiên URL m4a, đọc thời lượng, bỏ thumb", () => {
  assert.deepEqual(voiceCandidates(VOICE), [
    "https://voice-aac-dl.zdn.vn/v/abc.m4a",
    "https://voice-aac-dl.zdn.vn/v/abc.aac",
  ]);
  assert.equal(voiceDurationMs(VOICE), 3500);
  const withThumb = { ...VOICE, thumb: "https://photo-stal.zadn.vn/v/t.jpg" };
  assert.ok(!voiceCandidates(withThumb).some((url) => url.endsWith(".jpg")));
  assert.deepEqual(voiceCandidates({ href: "https://evil.com/a.m4a" }), []);
  const msg = parseMessage(zaloMessage("chat.voice", VOICE));
  assert.equal(msg.kind, "voice");
  assert.equal(msg.durationMs, 3500);
  assert.equal(directRoute(msg), "queue");
});

test("file: tên, đuôi, dung lượng khai báo và URL tải (không lấy icon)", () => {
  assert.deepEqual(fileInfo(fileContent("Hợp đồng.pdf", "pdf", 2048)), {
    name: "Hợp đồng.pdf",
    ext: "pdf",
    size: 2048,
  });
  assert.deepEqual(fileInfo({ title: "a.DOCX" }), { name: "a.DOCX", ext: "docx", size: null });
  const content = fileContent("a.pdf", "pdf");
  assert.deepEqual(fileCandidates(content), [content.href]);
  const msg = parseMessage(zaloMessage("share.file", content));
  assert.equal(msg.kind, "file");
  assert.deepEqual(msg.fileUrls, [content.href]);
  // Tên file không bị coi là chú thích (câu hỏi) của ảnh/file.
  assert.equal(parseMessage(zaloMessage("share.file", fileContent("IMG_1.jpg", ""))).caption, "");
  assert.equal(
    parseMessage(zaloMessage("chat.photo", { ...PHOTO, title: "đẹp không?" })).caption,
    "đẹp không?",
  );
});

test("đường đi: video/vị trí/file lạ -> câu báo; sticker -> im lặng", () => {
  assert.equal(directRoute(parseMessage(zaloMessage("chat.video.msg", VIDEO))), "notice");
  assert.equal(
    directRoute(parseMessage(zaloMessage("chat.location.new", { title: "" }))),
    "notice",
  );
  assert.equal(
    directRoute(parseMessage(zaloMessage("share.file", fileContent("a.zip", "zip")))),
    "notice",
  );
  assert.equal(directRoute(parseMessage(zaloMessage("chat.sticker", { id: 1 }))), "drop");
});

test("đường đi tin chữ: chữ thường gộp, lệnh/link vào hàng đợi, /huy và lệnh chỉ đọc đi làn nhanh", () => {
  const route = (text) => directRoute(parseMessage(zaloMessage("webchat", text)));
  assert.equal(route("mai trời mưa ko"), "batch");
  assert.equal(route("đọc giúp https://vnexpress.net/a.html"), "queue");
  assert.equal(route("/nhac 8h mai đi khám"), "queue");
  assert.equal(route("/huy"), "fast_lane");
  assert.equal(route("/help"), "fast_lane");
  assert.equal(route("/memory"), "fast_lane");
  assert.equal(route("/memory off"), "queue");
  assert.equal(route("   "), "drop");
});

test("ảnh có caption bắt đầu bằng / vẫn là lệnh (như trước)", () => {
  const msg = parseMessage(zaloMessage("chat.photo", { ...PHOTO, title: "/fb_ok 3" }));
  assert.equal(msg.kind, "image");
  assert.equal(directRoute(msg), "queue");
});

test("trích dẫn: lấy quote.msg (tối đa 600 ký tự), tin gốc là ảnh thì lấy title đính kèm", () => {
  assert.equal(quoteText({ msg: "  Mai họp lúc 9h nhé " }), "Mai họp lúc 9h nhé");
  assert.equal(Array.from(quoteText({ msg: "ả".repeat(700) })).length, 600);
  assert.equal(
    quoteText({ msg: "", attach: JSON.stringify({ title: "bao-cao.pdf" }) }),
    "bao-cao.pdf",
  );
  assert.equal(quoteText({ msg: { nested: true } }), "");
  assert.equal(quoteText(undefined), "");
  const msg = parseMessage(
    zaloMessage("webchat", "giải thích ý 2", { quote: { msg: "1. A\n2. B", ownerId: "bot" } }),
  );
  assert.equal(msg.quoteText, "1. A\n2. B");
});

test("seenParams giữ đủ tham số sendSeenEvent; thiếu id thì null", () => {
  const params = seenParams(zaloMessage("webchat", "x").data);
  assert.deepEqual(params, {
    msgId: "7001",
    cliMsgId: "9001",
    uidFrom: "u1",
    idTo: "bot",
    msgType: "webchat",
    st: 3,
    at: 5,
    cmd: 501,
    ts: "1761400000000",
  });
  assert.equal(seenParams({ msgId: "1" }), null);
});

test("lệnh làn nhanh khớp services/turn_control.py", () => {
  assert.deepEqual(parseCommand("/Help@lananh_bot abc"), ["/help", "abc"]);
  assert.deepEqual(parseCommand("xin chào"), ["", ""]);
  assert.equal(isCancelCommand("/huy"), true);
  assert.equal(isCancelCommand("/HUY  "), true);
  assert.equal(isReadonlyCommand("/tuvi 1990"), true);
  assert.equal(isReadonlyCommand("/nhac"), true);
  assert.equal(isReadonlyCommand("/nhac 8h mai"), false);
  // Tách ở dấu cách đầu tiên như Python str.partition(" ").
  assert.equal(isReadonlyCommand("/help\nabc"), false);
  assert.equal(isFastLaneCommand("/fb_ok 3"), false);
  assert.equal(isFastLaneCommand("/status"), false);
});

test("câu báo: Python dựng câu; người lạ im lặng kể cả khi Python lỗi; người quen có câu dự phòng", async () => {
  const paired = [];
  const deps = (fetch, known) => ({
    fetch,
    isKnown: () => known,
    onPaired: (sender) => paired.push(sender),
    log: () => undefined,
  });
  assert.equal(
    await noticeText(
      "u1",
      "video",
      deps(async () => "🎬 Chị ơi em chưa xem được video", false),
    ),
    "🎬 Chị ơi em chưa xem được video",
  );
  assert.deepEqual(paired, ["u1"]);
  // Người lạ chưa pair: Python trả null.
  assert.equal(
    await noticeText(
      "stranger",
      "video",
      deps(async () => null, false),
    ),
    null,
  );
  const down = async () => {
    throw new Error("fetch failed");
  };
  assert.equal(await noticeText("stranger", "voice_failed", deps(down, false)), null);
  assert.equal(
    await noticeText("u1", "voice_failed", deps(down, true)),
    LOCAL_NOTICES.voice_failed,
  );
  // Câu dự phòng không gọi trống "anh".
  for (const text of Object.values(LOCAL_NOTICES)) assert.ok(!/\banh\b/i.test(text), text);
});
