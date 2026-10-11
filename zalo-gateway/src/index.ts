import { randomInt } from "node:crypto";
import { LoginQRCallbackEventType, ThreadType, Zalo, type Credentials } from "zca-js";
import {
  ackOutbox,
  callBridge,
  callImageBridge,
  clearController,
  clearSession,
  fetchAllowedGroups,
  fetchFacebookGroups,
  fetchOutbox,
  loadController,
  loadSavedSession,
  reportOutboxFailure,
  saveController,
  saveSession,
  storeFacebookGroupPost,
  storeGroupMessage,
} from "./bridge.js";
import { loadConfig } from "./config.js";
import { startControlServer } from "./control-server.js";
import { FacebookPostBuffer } from "./facebook-buffer.js";
import {
  BoundedSerialQueue,
  KeyedRateLimiter,
  KeyedSerialQueue,
  QueueFullError,
} from "./limits.js";
import { downloadImage, imageCandidates } from "./media.js";
import { OutboxDelivery } from "./outbox.js";
import {
  KnownSenders,
  SeenIds,
  errorMessage,
  isConnectionRefused,
  isTransientNetworkError,
  sleep,
  splitForZalo,
} from "./text.js";
import type { MediaItem, ZaloApi, ZaloMessage } from "./types.js";

const config = loadConfig();
const zalo = new Zalo({ selfListen: false, checkUpdate: false, logging: false });

let api: ZaloApi | null = null;
let qr: string | null = null;
let state = "idle";
let loginPromise: Promise<void> | null = null;
let allowedGroups = new Set<string>();
let facebookGroups = new Set<string>();
let pairing: { code: string; expires: number } | null = null;
let outboxBusy = false;

const seen = new SeenIds(5000);
// Người gửi Python đã trả lời (đã pair): chỉ họ nhận câu báo bận/sự cố.
const knownSenders = new KnownSenders(2000);
// Tin riêng: tuần tự theo từng người gửi, song song giữa người gửi khác nhau
// (tối đa directConcurrency lượt, khớp MAX_CONCURRENT_TURNS bên Python).
const directQueue = new KeyedSerialQueue({
  concurrency: config.directConcurrency,
  perKeyCapacity: config.directPerSenderBacklog,
  capacity: 64,
});
// Báo "đang bận" tối đa 1 lần/phút cho mỗi người khi hàng đợi đầy.
const busyNotices = new KeyedRateLimiter(60000, 256);
const groupQueue = new BoundedSerialQueue(128);
const facebookBuffer = new FacebookPostBuffer({
  store: (post) => storeFacebookGroupPost(config, post),
  enqueue: (work) => groupQueue.add(work),
  windowMs: config.facebookMergeWindowMs,
});

const GROUP_ADMIN_COMMAND = /^\/(themnhom|xoanhom|fb_themnhom|fb_xoanhom)\b/i;
const BUSY_REPLY =
  "⏳ Bot đang trả lời nhiều tin cùng lúc nên chưa nhận được tin vừa rồi. Bạn gửi lại sau ít phút nhé.";
const BRIDGE_ERROR_REPLY =
  "⚠️ Bot đang gặp sự cố nên chưa trả lời được tin vừa rồi. Bạn thử gửi lại sau ít phút nhé.";

// ─── Gửi tin ────────────────────────────────────────────────────────────────

async function sendImage(client: ZaloApi, target: string, imageB64: string): Promise<void> {
  const data = Buffer.from(imageB64, "base64");
  if (!data.length) {
    await client.sendMessage(
      { msg: "❌ Tạo ảnh xong nhưng dữ liệu ảnh rỗng" },
      target,
      ThreadType.User,
    );
    return;
  }
  let lastError: unknown = null;
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      if (attempt > 0) await sleep(1500 * attempt);
      await client.sendMessage(
        {
          msg: "",
          attachments: [
            { data, filename: `agnes-${Date.now()}.png`, metadata: { totalSize: data.length } },
          ],
        },
        target,
        ThreadType.User,
      );
      return;
    } catch (error) {
      lastError = error;
      console.error(`[zalo] send image failed (lần ${attempt + 1}/3)`, error);
      if (!isTransientNetworkError(error)) break;
    }
  }
  await client.sendMessage(
    { msg: `❌ Tạo ảnh xong nhưng gửi thất bại: ${errorMessage(lastError)}` },
    target,
    ThreadType.User,
  );
}

async function send(target: string, chunks: string[], imageB64?: string | null): Promise<void> {
  const client = api;
  if (!client) throw new Error("Zalo is not connected");
  for (const chunk of chunks) {
    if (chunk.trim()) await client.sendMessage({ msg: chunk }, target, ThreadType.User);
  }
  if (imageB64) await sendImage(client, target, imageB64);
}

// ─── Đồng bộ với Python ─────────────────────────────────────────────────────

async function refreshGroups(): Promise<void> {
  if (!api) return;
  try {
    allowedGroups = await fetchAllowedGroups(config);
  } catch (error) {
    console.error("[zalo] summary group refresh failed", error);
  }
  try {
    facebookGroups = await fetchFacebookGroups(config);
  } catch (error) {
    console.error("[zalo] Facebook group refresh failed", error);
  }
}

const outbox = new OutboxDelivery({
  fetch: () => fetchOutbox(config),
  sendChunk: async (recipientId, chunk) => {
    const client = api;
    if (!client) throw new Error("Zalo is not connected");
    await client.sendMessage({ msg: chunk }, recipientId, ThreadType.User);
  },
  ack: (id) => ackOutbox(config, id),
  fail: (id, error, chunksSent) => reportOutboxFailure(config, id, error, chunksSent),
  connected: () => !!api,
});

async function deliverOutbox(): Promise<void> {
  const result = await outbox.runOnce();
  if (result.failed) console.warn(`[zalo] outbox: ${result.sent} gửi xong, ${result.failed} lỗi`);
}

async function listGroups(threadId: string): Promise<void> {
  const client = api;
  if (!client) return;
  const all = await client.getAllGroups();
  const ids = Object.keys(all?.gridVerMap || {});
  const lines = [ids.length ? "Các nhóm B đang tham gia:" : "Chưa có nhóm."];
  for (const gid of ids.slice(0, 100)) {
    try {
      const info = await client.getGroupInfo(gid);
      const detail = info?.changed_groups?.[gid] || info?.gridInfoMap?.[gid];
      lines.push(`• ${detail?.name || "Không rõ tên"} — ${gid}`);
    } catch {
      lines.push(`• ${gid}`);
    }
  }
  await send(threadId, splitForZalo(lines.join("\n")));
}

async function fetchImage(urls: string[], attempts = 2): Promise<MediaItem> {
  let lastError: unknown = null;
  for (let attempt = 0; attempt < attempts; attempt++) {
    if (attempt > 0) await sleep(2000);
    const ctx = api?.getContext?.();
    try {
      return await downloadImage(urls, {
        userAgent: ctx?.userAgent || config.userAgent,
        cookieFor: (url) => ctx?.cookie?.getCookieStringSync?.(url) || "",
      });
    } catch (error) {
      lastError = error;
    }
  }
  throw lastError;
}

// ─── Xử lý tin nhắn ─────────────────────────────────────────────────────────

type ParsedMessage = {
  text: string;
  caption: string;
  urls: string[];
  sender: string;
  senderName: string;
  id: string;
  threadId: string;
};

function parseMessage(message: ZaloMessage): ParsedMessage {
  const content = message.data?.content as
    | string
    | { title?: unknown; description?: unknown }
    | undefined;
  const isObject = typeof content === "object" && content !== null;
  return {
    text:
      typeof content === "string"
        ? content.trim()
        : String((isObject && content.title) || "").trim(),
    caption: isObject ? String(content.description || content.title || "").trim() : "",
    urls: isObject ? imageCandidates(content) : [],
    sender: String(message.data?.uidFrom || ""),
    senderName: String(message.data?.dName || ""),
    id: String(message.data?.msgId || message.data?.cliMsgId || ""),
    threadId: String(message.threadId),
  };
}

/** Trả về true nếu tin nhắn là mã ghép đôi hợp lệ (đã xử lý). */
function handlePairing(msg: ParsedMessage): boolean {
  if (!pairing || !msg.text) return false;
  if (Date.now() > pairing.expires) {
    pairing = null;
    return false;
  }
  if (msg.text !== `/pair ${pairing.code}`) return false;
  directQueue
    .add(msg.sender, async () => {
      config.controllerId = msg.sender;
      await saveController(config, msg.sender);
      pairing = null;
      await send(msg.threadId, [
        "✅ Ghép đôi thành công. Tài khoản này giờ có thể điều khiển bot B.",
      ]);
    })
    .catch(console.error);
  return true;
}

function handleGroupMessage(message: ZaloMessage, msg: ParsedMessage): void {
  const gid = msg.threadId;
  let sentAtMs = Number(message.data?.ts || Date.now());
  if (sentAtMs < 1e12) sentAtMs *= 1000;
  if (allowedGroups.has(gid) && msg.text) {
    groupQueue
      .add(() =>
        storeGroupMessage(config, {
          groupId: gid,
          messageId: msg.id,
          senderId: msg.sender,
          senderName: msg.senderName,
          text: msg.text,
          sentAtMs,
        }),
      )
      .catch(console.error);
  }
  if (!facebookGroups.has(gid)) return;
  const postText = msg.text || msg.caption;
  groupQueue
    .add(async () => {
      const media: MediaItem[] = [];
      if (msg.urls.length) {
        try {
          // Ảnh nhóm: thử lại 1 lần, vì tải hỏng = bài bị bộ lọc bỏ ("không có ảnh").
          media.push(await fetchImage(msg.urls, 2));
        } catch (error) {
          console.error("[zalo] Facebook group image download failed", error);
        }
      }
      if (postText || media.length) {
        await facebookBuffer.add(gid, msg.sender, msg.senderName, msg.id, postText, media);
      }
    })
    .catch(console.error);
}

async function handleDirectMessage(msg: ParsedMessage): Promise<void> {
  if (msg.urls.length && !msg.text.startsWith("/")) {
    try {
      const image = await fetchImage(msg.urls, 1);
      const reply = await callImageBridge(
        config,
        msg.sender,
        msg.id,
        msg.caption,
        image.mime,
        image.bytes,
      );
      if (reply.messages.length) {
        knownSenders.mark(msg.sender);
        await send(msg.threadId, ["🖼️ Đã nhận ảnh, đang viết prompt...", ...reply.messages]);
      }
    } catch (error) {
      console.error(`[zalo] image bridge failed for message ${msg.id}`, error);
      // Người lạ chưa pair: im lặng như phía Python.
      if (knownSenders.has(msg.sender)) {
        await send(msg.threadId, [`❌ Không xử lý được ảnh: ${errorMessage(error)}`]);
      }
    }
    return;
  }
  if (msg.text.toLowerCase() === "/nhomzalo") {
    if (!config.controllerId || msg.sender !== config.controllerId) {
      await send(msg.threadId, ["Lệnh này chỉ dành cho chủ bot (chưa ghép đôi /pair)."]);
      return;
    }
    await listGroups(msg.threadId);
    return;
  }
  let reply: Awaited<ReturnType<typeof callBridge>>;
  try {
    reply = await callBridge(config, {
      senderId: msg.sender,
      senderName: msg.senderName,
      conversationId: msg.threadId,
      messageId: msg.id,
      text: msg.text,
    });
  } catch (error) {
    // Python lỗi/timeout: báo người gửi đã pair thay vì im lặng; người lạ
    // chưa pair vẫn không nhận gì (không lộ ra đây là bot).
    console.error(`[zalo] bridge failed for message ${msg.id}`, error);
    if (knownSenders.has(msg.sender)) await send(msg.threadId, [BRIDGE_ERROR_REPLY]);
    return;
  }
  if (reply.messages.length || reply.image_b64) knownSenders.mark(msg.sender);
  await send(msg.threadId, reply.messages, reply.image_b64);
  if (GROUP_ADMIN_COMMAND.test(msg.text)) await refreshGroups();
}

function enqueueDirect(msg: ParsedMessage): void {
  directQueue
    .add(msg.sender, () => handleDirectMessage(msg))
    .catch((error) => {
      if (!(error instanceof QueueFullError)) {
        console.error(`[zalo] direct message ${msg.id} failed`, error);
        return;
      }
      console.warn(
        `[zalo] bỏ tin ${msg.id} của ${msg.sender}: ${error.message} ` +
          `(đang chờ ${directQueue.pending}, đang chạy ${directQueue.running})`,
      );
      if (!knownSenders.has(msg.sender) || !busyNotices.allow(msg.sender)) return;
      send(msg.threadId, [BUSY_REPLY]).catch((sendError) =>
        console.error("[zalo] busy notice failed", sendError),
      );
    });
}

function onMessage(message: ZaloMessage): void {
  if (message.isSelf) return;
  const msg = parseMessage(message);
  if ((!msg.text && !msg.urls.length) || !msg.id || !seen.remember(msg.id)) return;
  if (message.type === ThreadType.User && handlePairing(msg)) return;
  if (message.type === ThreadType.Group) {
    handleGroupMessage(message, msg);
    return;
  }
  if (message.type !== ThreadType.User) return;
  enqueueDirect(msg);
}

// ─── Đăng nhập / phiên ──────────────────────────────────────────────────────

async function attach(next: ZaloApi): Promise<void> {
  api = next;
  state = "connected";
  qr = null;
  config.accountId = String(next.getOwnId() || config.accountId);
  await refreshGroups();
  const listener = next.listener;
  // Mọi sự kiện chỉ áp dụng cho phiên hiện tại: listener cũ (sau logout/đăng
  // nhập lại) có báo muộn cũng không được xoá phiên mới.
  listener.on("message", (message) => {
    if (api === next) onMessage(message);
  });
  listener.on("connected", () => {
    if (api === next) state = "connected";
    // Listener cũ tự kết nối lại sau logout: đóng ngay.
    else listener.stop?.();
  });
  listener.on("disconnected", (code, reason) => {
    // zca-js tự kết nối lại (retryOnClose) với mã đóng cho phép; API HTTP vẫn dùng được.
    if (api === next) state = "reconnecting";
    console.warn(`[zalo] listener disconnected code=${code ?? "?"} ${reason ?? ""}`);
  });
  listener.on("closed", (code, reason) => {
    console.error(`[zalo] listener closed code=${code ?? "?"} ${reason ?? ""}; cần đăng nhập lại`);
    if (api !== next) return;
    api = null;
    state = "closed";
  });
  listener.on("error", console.error);
  listener.start({ retryOnClose: true });
  console.log(`[zalo] listener started account=${config.accountId}`);
}

async function persist(next: ZaloApi): Promise<void> {
  const ctx = next.getContext();
  await saveSession(config, {
    cookie: ctx.cookie.serializeSync(),
    imei: ctx.imei,
    userAgent: ctx.userAgent,
    accountId: String(next.getOwnId()),
  });
}

type QrEvent = { type: number; data?: { image?: string; qrData?: string } };

function onQrEvent(event: QrEvent): void {
  if (event.type === LoginQRCallbackEventType.QRCodeGenerated) {
    qr = String(event.data?.image || event.data?.qrData || "").replace(
      /^data:image\/png;base64,/,
      "",
    );
    state = "qr_ready";
  } else if (event.type === LoginQRCallbackEventType.QRCodeScanned) {
    qr = null;
    state = "scanned";
  } else if (event.type === LoginQRCallbackEventType.QRCodeExpired) {
    qr = null;
    state = "expired";
  } else if (event.type === LoginQRCallbackEventType.QRCodeDeclined) {
    qr = null;
    state = "declined";
  }
}

async function startQr(): Promise<string> {
  if (api || loginPromise) return state;
  state = "waiting_qr";
  qr = null;
  loginPromise = (async () => {
    try {
      const next = (await zalo.loginQR({ userAgent: config.userAgent }, onQrEvent)) as ZaloApi;
      state = "saving_session";
      await persist(next);
      await attach(next);
    } catch (error) {
      qr = null;
      state = "error";
      console.error("[zalo] QR login failed", error);
    } finally {
      loginPromise = null;
    }
  })();
  return state;
}

type SavedSession = { cookie?: unknown; imei?: string; userAgent?: string };

const BOOTSTRAP_MAX_ATTEMPTS = 30;

/** Lỗi tạm khi khôi phục phiên: Python chưa lên/đang khởi động, mạng tới Zalo chập chờn. */
function retryableBootstrapError(error: unknown): boolean {
  if (isConnectionRefused(error) || isTransientNetworkError(error)) return true;
  const e = error as { status?: unknown; name?: unknown } | null;
  return e?.name === "TimeoutError" || (typeof e?.status === "number" && e.status >= 500);
}

async function bootstrap(attempt = 0): Promise<void> {
  // Đã đăng nhập bằng QR trong lúc chờ thử lại: không đăng nhập chồng phiên.
  if (api || loginPromise) return;
  try {
    state = "waiting_backend";
    if (!config.controllerId) config.controllerId = await loadController(config);
    const session: SavedSession =
      config.cookie && config.imei
        ? { cookie: config.cookie, imei: config.imei, userAgent: config.userAgent }
        : ((await loadSavedSession(config)) as SavedSession);
    if (session?.cookie && session?.imei) {
      state = "restoring";
      const credentials: Credentials = {
        cookie: session.cookie,
        imei: session.imei,
        userAgent: session.userAgent || config.userAgent,
      };
      await attach((await zalo.login(credentials)) as ZaloApi);
    } else {
      state = "awaiting_login";
    }
  } catch (error) {
    // Python (uvicorn) có thể khởi động chậm hơn Node trong cùng container (chạy
    // migration, DB Supabase đang ngủ...): thử lại ~4 phút thay vì bắt quét QR lại.
    if (retryableBootstrapError(error) && attempt < BOOTSTRAP_MAX_ATTEMPTS) {
      state = "waiting_backend";
      const delay = Math.min(10000, 1000 * (attempt + 1));
      console.warn(`[zalo] restore chưa được (lần ${attempt + 1}); thử lại sau ${delay}ms`);
      setTimeout(() => void bootstrap(attempt + 1), delay).unref();
      return;
    }
    state = "awaiting_login";
    console.error("[zalo] restore failed", error);
  }
}

// ─── Khởi động ──────────────────────────────────────────────────────────────

startControlServer(config.controlPort, {
  status: () => ({
    state,
    connected: !!api,
    accountId: api ? String(api.getOwnId()) : null,
    controllerPaired: !!config.controllerId,
    qr,
  }),
  startQr,
  startPairing: () => {
    if (!api) return null;
    pairing = { code: String(randomInt(100000, 1000000)), expires: Date.now() + 300000 };
    return { code: pairing.code, expiresAt: pairing.expires };
  },
  logout: async () => {
    try {
      api?.listener?.stop?.();
    } catch {
      // Listener đã đóng: bỏ qua.
    }
    api = null;
    qr = null;
    pairing = null;
    config.controllerId = "";
    state = "awaiting_login";
    await clearSession(config);
    await clearController(config);
  },
});

setInterval(() => void refreshGroups(), config.groupRefreshMs).unref();
setInterval(() => {
  if (outboxBusy) return;
  outboxBusy = true;
  deliverOutbox()
    .catch(console.error)
    .finally(() => {
      outboxBusy = false;
    });
}, config.outboxPollMs).unref();
void bootstrap();
