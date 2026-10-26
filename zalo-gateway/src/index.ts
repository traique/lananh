import { randomInt } from "node:crypto";
import { LoginQRCallbackEventType, ThreadType, Zalo, type Credentials } from "zca-js";
import {
  FAST_LANE_TIMEOUT_MS,
  ackOutbox,
  callBridge,
  callFileBridge,
  callImageBridge,
  callVoiceBridge,
  clearController,
  clearSession,
  fetchAllowedGroups,
  fetchFacebookGroups,
  fetchNotice,
  fetchOutbox,
  loadController,
  loadSavedSession,
  reportGatewayState,
  reportOutboxFailure,
  saveController,
  saveSession,
  storeFacebookGroupPost,
  storeGroupMessage,
  type BridgeResponse,
  type GatewayState,
  type NoticeKind,
} from "./bridge.js";
import { DirectTextBatcher } from "./burst.js";
import { controllerCommandAction, isCancelCommand } from "./commands.js";
import { loadConfig } from "./config.js";
import { startControlServer } from "./control-server.js";
import { FacebookPostBuffer } from "./facebook-buffer.js";
import { TypingKeepalive, fireAndForget, withTimeout } from "./feedback.js";
import {
  BoundedSerialQueue,
  KeyedRateLimiter,
  KeyedSerialQueue,
  QueueFullError,
  SlidingWindowBudget,
} from "./limits.js";
import {
  MediaDownloadError,
  downloadAudio,
  downloadDocument,
  downloadImage,
  type DownloadOptions,
} from "./media.js";
import { directRoute, parseMessage, type ParsedMessage } from "./msgtype.js";
import { noticeText } from "./notices.js";
import { OutboxDelivery } from "./outbox.js";
import { ReconnectScheduler } from "./reconnect.js";
import { STORE_RETRY_DELAYS_MS, retry } from "./retry.js";
import { jitterMs, sendChunks, type SendChunksOptions } from "./sender.js";
import { GatewayStats } from "./stats.js";
import {
  KnownSenders,
  SeenIds,
  errorMessage,
  isConnectionRefused,
  isTransientNetworkError,
  sleep,
  splitForZalo,
  userErrorReply,
} from "./text.js";
import {
  DEST_TYPE_USER,
  REACTION_QUEUED,
  type MediaItem,
  type ZaloApi,
  type ZaloMessage,
  type ZaloSeenParams,
} from "./types.js";

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
/** Số lệnh làn nhanh đang chờ Python (ngoài hàng đợi theo người gửi). */
let fastLaneActive = 0;
let lastDeferLogAt = 0;

const stats = new GatewayStats();
const seen = new SeenIds(5000);
// Người gửi Python đã trả lời (đã pair): chỉ họ nhận câu báo bận/sự cố.
const knownSenders = new KnownSenders(2000);
// Tin riêng: tuần tự theo từng người gửi, song song giữa người gửi khác nhau
// (tối đa directConcurrency lượt, khớp số chỗ thành viên bên Python =
// MAX_CONCURRENT_TURNS - OWNER_RESERVED_SLOTS, xem services/concurrency.py).
const directQueue = new KeyedSerialQueue({
  concurrency: config.directConcurrency,
  perKeyCapacity: config.directPerSenderBacklog,
  capacity: 64,
});
// Báo "đang bận" tối đa 1 lần/phút cho mỗi người khi hàng đợi đầy.
const busyNotices = new KeyedRateLimiter(60000, 256);
// Câu "chưa hỗ trợ" (video, vị trí, file lạ): tối đa 1 lần/phút mỗi người mỗi loại.
const unsupportedNotices = new KeyedRateLimiter(60000, 512);
// "Em nhận được ảnh rồi...": 1 lần/phút mỗi người (gửi album 5 ảnh không thành 5 câu).
const imageNotices = new KeyedRateLimiter(60000, 256);
// Reaction "đang chờ lượt trước": tối đa 1 lần/15s mỗi người.
const queuedReactions = new KeyedRateLimiter(15000, 256);
// Ngân sách tin outbox của tài khoản theo giờ: vượt thì hoãn sang lượt poll sau.
const outboxBudget = new SlidingWindowBudget(config.outboxMaxPerHour, 3600000);
const groupQueue = new BoundedSerialQueue(128);
const facebookBuffer = new FacebookPostBuffer({
  store: (post) => storeFacebookGroupPost(config, post),
  enqueue: (work) => groupQueue.add(work),
  windowMs: config.facebookMergeWindowMs,
});

const GROUP_ADMIN_COMMAND = /^\/(themnhom|xoanhom|fb_themnhom|fb_xoanhom)\b/i;
/** Làn nhanh: tối đa chừng này lệnh chờ Python cùng lúc; vượt thì vào hàng đợi như thường. */
const FAST_LANE_LIMIT = 4;
/** Timeout mỗi lệnh typing/seen/reaction của zca-js (gọi không chờ). */
const PRESENCE_TIMEOUT_MS = 5000;
/** Gộp tin chữ lúc tới lượt: trần số tin / ký tự của 1 lượt gộp. */
const MERGE_MAX_MESSAGES = 8;
const MERGE_MAX_CHARS = 4000;
const BUSY_REPLY =
  "⏳ Bot đang trả lời nhiều tin cùng lúc nên chưa nhận được tin vừa rồi. Bạn gửi lại sau ít phút nhé.";
const BRIDGE_ERROR_REPLY =
  "⚠️ Bot đang gặp sự cố nên chưa trả lời được tin vừa rồi. Bạn thử gửi lại sau ít phút nhé.";
// Lỗi ảnh: người gửi chỉ thấy câu cố định, chủ bot thấy thêm chi tiết (userErrorReply).
const IMAGE_ERROR_REPLY =
  "⚠️ Bot chưa xử lý được ảnh vừa rồi (AI đang bận hoặc ảnh không tải được). Bạn thử gửi lại sau ít phút nhé.";
const IMAGE_SEND_ERROR_REPLY =
  "⚠️ Ảnh đã tạo xong nhưng gửi qua Zalo bị lỗi. Bạn thử lại lệnh sau ít phút nhé.";
/**
 * Người gửi chắc chắn đã pair: Python từng trả lời họ (từ lúc gateway khởi động)
 * hoặc chính là chủ bot. Chỉ họ nhận câu báo do gateway tự gửi, typing, "đã xem"
 * - người lạ vẫn im lặng hoàn toàn như phía Python.
 */
function isKnown(sender: string): boolean {
  return knownSenders.has(sender) || (!!config.controllerId && sender === config.controllerId);
}

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
  // Tin riêng: target (thread) chính là id người gửi.
  await client.sendMessage(
    { msg: userErrorReply(lastError, target, config.controllerId, IMAGE_SEND_ERROR_REPLY) },
    target,
    ThreadType.User,
  );
}

/** Khoảng nghỉ ngẫu nhiên giữa 2 tin liên tiếp (ZALO_SEND_GAP_MIN_MS..MAX_MS). */
function sendGap(): number {
  return jitterMs(config.sendGapMinMs, config.sendGapMaxMs);
}

function sendOptions(client: ZaloApi): SendChunksOptions {
  return {
    threadType: ThreadType.User,
    isCurrent: () => api === client,
    gapMs: sendGap,
    onChunkSent: () => stats.inc("chunks_sent"),
    onPlainFallback: () => stats.inc("plain_fallbacks"),
  };
}

/**
 * Gửi từng đoạn (kèm styles zca-js nếu Python gửi), nghỉ ngẫu nhiên giữa các
 * đoạn; mỗi đoạn thử lại theo SEND_RETRY_DELAYS_MS (1s, 3s, 8s) để không gửi
 * trùng đoạn đã tới nơi, Zalo từ chối định dạng thì gửi lại 1 lần chữ thuần
 * (sender.ts). Mất đăng nhập giữa chừng thì dừng ngay.
 */
async function send(
  target: string,
  chunks: string[],
  imageB64?: string | null,
  styles?: unknown,
): Promise<void> {
  const client = api;
  if (!client) throw new Error("Zalo is not connected");
  const sent = await sendChunks(client, target, chunks, styles, sendOptions(client));
  if (imageB64) {
    const gap = sent > 0 ? sendGap() : 0;
    if (gap > 0) await sleep(gap);
    await sendImage(client, target, imageB64);
  }
}

/** Gửi 1 câu báo ngắn đúng 1 lần (không thử lại, timeout ngắn); lỗi chỉ log. */
async function sendOnce(target: string, text: string): Promise<boolean> {
  const client = api;
  if (!client) return false;
  try {
    await withTimeout(client.sendMessage({ msg: text }, target, ThreadType.User), 15000);
    stats.inc("chunks_sent");
    return true;
  } catch (error) {
    console.warn(`[zalo] gửi câu báo tới ${target} lỗi: ${errorMessage(error, String(error))}`);
    return false;
  }
}

/** Báo trạng thái gateway về Python, không chờ; lỗi chỉ log. */
function reportState(gatewayState: GatewayState, detail?: string): void {
  reportGatewayState(config, { account_id: config.accountId, state: gatewayState, detail }).catch(
    (error) => console.error(`[zalo] không báo được trạng thái "${gatewayState}" về Python`, error),
  );
}

/**
 * Gửi trả lời cho 1 tin riêng. Hết lượt thử lại thì log + báo Python
 * ("send_failed") để chủ bot biết, không ném lỗi nữa (không còn gì để làm).
 */
async function sendReply(
  msg: ParsedMessage,
  chunks: string[],
  imageB64?: string | null,
  styles?: unknown,
): Promise<void> {
  try {
    await send(msg.threadId, chunks, imageB64, styles);
  } catch (error) {
    stats.inc("send_failures");
    const reason = errorMessage(error, String(error));
    console.error(
      `[zalo] gửi trả lời tin ${msg.id} tới ${msg.sender} thất bại sau khi thử lại`,
      error,
    );
    reportState("send_failed", `tin ${msg.id}: ${reason}`);
  }
}

// ─── Phản hồi trong lúc chờ (typing, đã xem, reaction) ─────────────────────

type TurnFeedback = { stop(): void };
const NO_FEEDBACK: TurnFeedback = { stop: () => undefined };

function presenceError(label: string): (error: unknown) => void {
  return (error) => {
    stats.inc("presence_errors");
    console.warn(`[zalo] ${label} lỗi (bỏ qua): ${errorMessage(error, String(error))}`);
  };
}

/**
 * Bắt đầu 1 lượt gửi Python: đánh dấu "đã xem" các tin của lượt (đủ tham số
 * zca-js cần) và hiện "đang soạn tin", làm mới mỗi ~6s, tối đa 30 lần (~3 phút).
 * Gọi không chờ, timeout ngắn, lỗi bỏ qua - không bao giờ chặn hàng đợi. Chỉ cho
 * người quen (người lạ không được biết bot đã đọc tin).
 */
function startTurnFeedback(sender: string, threadId: string, items: ParsedMessage[]): TurnFeedback {
  const client = api;
  if (!client || !config.presenceEvents || !isKnown(sender)) return NO_FEEDBACK;
  const seenList = items.map((item) => item.seen).filter((s): s is ZaloSeenParams => !!s);
  if (seenList.length) {
    fireAndForget(
      async () => {
        await client.sendSeenEvent(seenList.slice(-50), ThreadType.User);
        stats.inc("seen_events");
      },
      PRESENCE_TIMEOUT_MS,
      presenceError("sendSeenEvent"),
    );
  }
  const typing = new TypingKeepalive(
    () => client.sendTypingEvent(threadId, ThreadType.User, DEST_TYPE_USER),
    {
      intervalMs: 6000,
      maxEvents: 30,
      timeoutMs: PRESENCE_TIMEOUT_MS,
      onSent: () => stats.inc("typing_events"),
      onError: presenceError("sendTypingEvent"),
    },
  ).start();
  return { stop: () => typing.stop() };
}

/** Tin phải chờ sau lượt đang chạy của CHÍNH người gửi: thả 👌 để họ biết bot đã thấy. */
function reactIfQueued(msg: ParsedMessage): void {
  const client = api;
  if (!client || !config.presenceEvents || !msg.seen || !isKnown(msg.sender)) return;
  if (!directQueue.isRunning(msg.sender) || !queuedReactions.allow(msg.sender)) return;
  const target = {
    data: { msgId: msg.seen.msgId, cliMsgId: msg.seen.cliMsgId },
    threadId: msg.threadId,
    type: ThreadType.User,
  };
  fireAndForget(
    async () => {
      await client.addReaction(REACTION_QUEUED, target);
      stats.inc("reactions");
    },
    PRESENCE_TIMEOUT_MS,
    presenceError("addReaction"),
  );
}

/**
 * Gửi câu báo dựng sẵn (ảnh đã nhận, tin thoại/file lỗi, loại tin chưa hỗ trợ).
 * Câu do Python dựng (POST /notice: giọng Lan Anh, đúng cách gọi riêng, người lạ
 * -> null = im lặng); Python không trả lời được thì người quen nhận câu trung
 * tính LOCAL_NOTICES.
 */
async function sendNotice(msg: ParsedMessage, kind: NoticeKind): Promise<boolean> {
  const text = await noticeText(msg.sender, kind, {
    fetch: (sender, noticeKind) => fetchNotice(config, sender, noticeKind),
    isKnown,
    onPaired: (sender) => knownSenders.mark(sender),
  });
  if (!text) return false;
  stats.inc("notices");
  return sendOnce(msg.threadId, text);
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
    stats.inc("chunks_sent");
  },
  ack: (id) => ackOutbox(config, id),
  fail: (id, error, chunksSent) => reportOutboxFailure(config, id, error, chunksSent),
  connected: () => !!api,
  pace: async () => {
    const gap = sendGap();
    if (gap > 0) await sleep(gap);
  },
  takeBudget: () => outboxBudget.tryTake(),
});

async function deliverOutbox(): Promise<void> {
  const result = await outbox.runOnce();
  if (result.sent) stats.inc("outbox_sent", result.sent);
  if (result.failed) {
    stats.inc("outbox_failed", result.failed);
    console.warn(`[zalo] outbox: ${result.sent} gửi xong, ${result.failed} lỗi`);
  }
  if (result.deferred) {
    stats.inc("outbox_deferred");
    // Poll mỗi 15s: log tối đa 1 dòng/10 phút khi đang hết ngân sách.
    if (Date.now() - lastDeferLogAt > 600000) {
      lastDeferLogAt = Date.now();
      console.warn(
        `[zalo] outbox: hết ngân sách ${config.outboxMaxPerHour} tin/giờ, ` +
          `hoãn ${result.deferred} tin sang lượt sau (không bỏ tin nào)`,
      );
    }
  }
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

/** Tải media bằng cookie + user agent của phiên Zalo hiện tại, thử `attempts` lần. */
async function fetchMedia(
  download: (options: DownloadOptions) => Promise<MediaItem>,
  attempts: number,
): Promise<MediaItem> {
  let lastError: unknown = null;
  for (let attempt = 0; attempt < attempts; attempt++) {
    if (attempt > 0) await sleep(2000);
    const ctx = api?.getContext?.();
    try {
      return await download({
        userAgent: ctx?.userAgent || config.userAgent,
        cookieFor: (url) => ctx?.cookie?.getCookieStringSync?.(url) || "",
      });
    } catch (error) {
      lastError = error;
      // Quá trần dung lượng: thử lại cũng vậy.
      if (error instanceof MediaDownloadError && error.tooLarge) break;
    }
  }
  throw lastError;
}

function fetchImage(urls: string[], attempts = 2): Promise<MediaItem> {
  return fetchMedia((options) => downloadImage(urls, options), attempts);
}

// ─── Xử lý tin nhắn ─────────────────────────────────────────────────────────

/** Trả về true nếu tin nhắn là mã ghép đôi hợp lệ (đã xử lý). */
function handlePairing(msg: ParsedMessage): boolean {
  if (!pairing || !msg.text) return false;
  if (Date.now() > pairing.expires) {
    pairing = null;
    return false;
  }
  if (msg.text !== `/pair ${pairing.code}`) return false;
  batcher.close(msg.sender);
  directQueue
    .add(msg.sender, async () => {
      // Lưu bên Python xong mới nhận trong RAM: lưu lỗi thì không ai được coi là chủ bot.
      await saveController(config, msg.sender);
      config.controllerId = msg.sender;
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
        retry(
          () =>
            storeGroupMessage(config, {
              groupId: gid,
              messageId: msg.id,
              senderId: msg.sender,
              senderName: msg.senderName,
              text: msg.text,
              sentAtMs,
            }),
          { delaysMs: STORE_RETRY_DELAYS_MS },
        ),
      )
      .catch((error) =>
        console.error(
          `[zalo] lưu tin nhóm ${msg.id} (nhóm ${gid}) thất bại sau ${STORE_RETRY_DELAYS_MS.length + 1} lần`,
          error,
        ),
      );
  }
  if (!facebookGroups.has(gid)) return;
  const postText = msg.text || msg.caption;
  groupQueue
    .add(async () => {
      const media: MediaItem[] = [];
      // Nhóm nguồn Facebook giữ nguyên danh sách URL như trước (kể cả thumb video).
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

/**
 * 1 lượt chữ (1 tin, hoặc nhiều tin chữ liên tiếp đã gộp): nối bằng xuống dòng,
 * gửi 1 lần với id tin CUỐI (Python cache idempotency theo id này), kèm chữ tin
 * được trích dẫn gần nhất. Typing/đã xem trong lúc chờ.
 */
async function handleTextTurn(items: ParsedMessage[]): Promise<void> {
  const last = items[items.length - 1];
  const text = items.map((item) => item.text).join("\n");
  const quoteText = [...items].reverse().find((item) => item.quoteText)?.quoteText || "";
  stats.inc("text_turns");
  if (items.length > 1) stats.inc("merged_messages", items.length - 1);
  const feedback = startTurnFeedback(last.sender, last.threadId, items);
  let reply: BridgeResponse;
  try {
    reply = await callBridge(config, {
      senderId: last.sender,
      senderName: last.senderName,
      conversationId: last.threadId,
      messageId: last.id,
      text,
      quoteText,
    });
  } catch (error) {
    // Python lỗi/timeout: báo người gửi đã pair thay vì im lặng; người lạ
    // chưa pair vẫn không nhận gì (không lộ ra đây là bot).
    feedback.stop();
    console.error(`[zalo] bridge failed for message ${last.id}`, error);
    if (isKnown(last.sender)) await sendReply(last, [BRIDGE_ERROR_REPLY]);
    return;
  }
  feedback.stop();
  if (reply.messages.length || reply.image_b64) knownSenders.mark(last.sender);
  await sendReply(last, reply.messages, reply.image_b64, reply.styles);
  if (GROUP_ADMIN_COMMAND.test(text)) await refreshGroups();
}

/**
 * Ảnh (chat.photo, chat.doodle, file ảnh): câu "đã nhận ảnh" gửi TRƯỚC khi tải
 * và gọi Python (trước đây gửi chung với kết quả - không báo được gì).
 */
async function handleImage(msg: ParsedMessage): Promise<void> {
  stats.inc("image_turns");
  const feedback = startTurnFeedback(msg.sender, msg.threadId, [msg]);
  try {
    if (imageNotices.allow(msg.sender)) await sendNotice(msg, "image");
    const image = await fetchImage(msg.urls, 1);
    const reply = await callImageBridge(
      config,
      msg.sender,
      msg.id,
      msg.caption,
      image.mime,
      image.bytes,
    );
    feedback.stop();
    if (reply.messages.length) {
      knownSenders.mark(msg.sender);
      await sendReply(msg, reply.messages, null, reply.styles);
    }
  } catch (error) {
    feedback.stop();
    console.error(`[zalo] image bridge failed for message ${msg.id}`, error);
    // Người lạ chưa pair: im lặng như phía Python.
    if (isKnown(msg.sender)) {
      await sendReply(msg, [
        userErrorReply(error, msg.sender, config.controllerId, IMAGE_ERROR_REPLY),
      ]);
    }
  }
}

/**
 * Tin thoại (chat.voice): tải bản ghi (ưu tiên m4a, chỉ domain Zalo, trần
 * VOICE_MAX_BYTES, kiểm magic bytes âm thanh) rồi POST /voice - Python chép lời
 * và trả lời như tin chữ. Không tải được -> câu "em chưa nghe được" thay vì lỗi ảnh.
 */
async function handleVoice(msg: ParsedMessage): Promise<void> {
  stats.inc("voice_turns");
  const feedback = startTurnFeedback(msg.sender, msg.threadId, [msg]);
  let audio: MediaItem;
  try {
    audio = await fetchMedia(
      (options) => downloadAudio(msg.voiceUrls, options, config.voiceMaxBytes),
      2,
    );
  } catch (error) {
    feedback.stop();
    console.warn(
      `[zalo] không tải được tin thoại ${msg.id}: ${errorMessage(error, String(error))}`,
    );
    const tooLarge = error instanceof MediaDownloadError && error.tooLarge;
    await sendNotice(msg, tooLarge ? "voice_too_large" : "voice_failed");
    return;
  }
  let reply: BridgeResponse;
  try {
    reply = await callVoiceBridge(
      config,
      msg.sender,
      msg.id,
      audio.mime,
      audio.bytes,
      msg.durationMs,
    );
  } catch (error) {
    feedback.stop();
    console.error(`[zalo] voice bridge failed for message ${msg.id}`, error);
    await sendNotice(msg, "voice_failed");
    return;
  }
  feedback.stop();
  if (reply.messages.length || reply.image_b64) knownSenders.mark(msg.sender);
  await sendReply(msg, reply.messages, reply.image_b64, reply.styles);
}

/**
 * File tài liệu (share.file PDF/DOCX/TXT): kiểm dung lượng Zalo khai báo trước
 * khi tải, tải trong trần DOCUMENT_MAX_BYTES, nội dung phải khớp đuôi, rồi POST
 * /file (không caption -> Python tóm tắt).
 */
async function handleFile(msg: ParsedMessage): Promise<void> {
  const file = msg.file;
  if (!file) return;
  if (file.size && file.size > config.documentMaxBytes) {
    await sendNotice(msg, "file_too_large");
    return;
  }
  stats.inc("file_turns");
  const feedback = startTurnFeedback(msg.sender, msg.threadId, [msg]);
  let document: MediaItem;
  try {
    document = await fetchMedia(
      (options) => downloadDocument(msg.fileUrls, file.ext, options, config.documentMaxBytes),
      2,
    );
  } catch (error) {
    feedback.stop();
    console.warn(`[zalo] không tải được file ${msg.id}: ${errorMessage(error, String(error))}`);
    const tooLarge = error instanceof MediaDownloadError && error.tooLarge;
    await sendNotice(msg, tooLarge ? "file_too_large" : "file_failed");
    return;
  }
  let reply: BridgeResponse;
  try {
    reply = await callFileBridge(
      config,
      msg.sender,
      msg.id,
      file.name || `tai-lieu.${file.ext}`,
      "",
      document.mime,
      document.bytes,
    );
  } catch (error) {
    feedback.stop();
    console.error(`[zalo] file bridge failed for message ${msg.id}`, error);
    if (isKnown(msg.sender)) await sendReply(msg, [BRIDGE_ERROR_REPLY]);
    return;
  }
  feedback.stop();
  if (reply.messages.length || reply.image_b64) knownSenders.mark(msg.sender);
  await sendReply(msg, reply.messages, reply.image_b64, reply.styles);
}

/** 1 tin (không gộp) khi tới lượt trong hàng đợi theo người gửi. */
async function handleDirectMessage(msg: ParsedMessage): Promise<void> {
  if (msg.kind === "link") {
    // Tin chia sẻ link: title chứa link = chữ người dùng gõ kèm link, giữ nguyên;
    // ngược lại title chỉ là tiêu đề trang -> gửi riêng link (Python coi là
    // "đọc bài này"). Thumb xem trước không phải tin ảnh.
    await handleTextTurn([
      { ...msg, text: msg.text.includes(msg.link) ? msg.text : msg.link, urls: [] },
    ]);
    return;
  }
  if (msg.kind === "image" && msg.urls.length && !msg.text.startsWith("/")) {
    await handleImage(msg);
    return;
  }
  if (msg.kind === "voice") {
    await handleVoice(msg);
    return;
  }
  if (msg.kind === "file") {
    await handleFile(msg);
    return;
  }
  if (!msg.text) return;
  const controllerAction = controllerCommandAction(msg.text, msg.sender, config.controllerId);
  // Người khác chủ bot gửi /nhomzalo: im lặng (commands.controllerCommandAction).
  if (controllerAction === "ignore") return;
  if (controllerAction === "run") {
    await listGroups(msg.threadId);
    return;
  }
  await handleTextTurn([msg]);
}

/** Hàng đợi từ chối (đầy) hoặc lượt ném lỗi ngoài dự kiến. */
function onDirectRejected(msg: ParsedMessage, error: unknown): void {
  if (!(error instanceof QueueFullError)) {
    console.error(`[zalo] direct message ${msg.id} failed`, error);
    return;
  }
  stats.inc("busy_rejections");
  console.warn(
    `[zalo] bỏ tin ${msg.id} của ${msg.sender}: ${error.message} ` +
      `(đang chờ ${directQueue.pending}, đang chạy ${directQueue.running})`,
  );
  if (!isKnown(msg.sender) || !busyNotices.allow(msg.sender)) return;
  send(msg.threadId, [BUSY_REPLY]).catch((sendError) =>
    console.error("[zalo] busy notice failed", sendError),
  );
}

function enqueueDirect(msg: ParsedMessage): void {
  directQueue
    .add(msg.sender, () => handleDirectMessage(msg))
    .catch((error) => onDirectRejected(msg, error));
}

// Tin chữ thường: debounce + gộp lúc tới lượt (burst.ts), mỗi lô là 1 việc của
// directQueue nên vẫn tuần tự theo người gửi và chịu trần ZALO_DIRECT_CONCURRENCY.
const batcher = new DirectTextBatcher<ParsedMessage>({
  debounceMs: config.directDebounceMs,
  maxWaitMs: config.directDebounceMaxMs,
  maxBufferItems: config.directDebounceMaxMessages,
  maxMergeItems: MERGE_MAX_MESSAGES,
  maxMergeChars: MERGE_MAX_CHARS,
  maxSenders: 256,
  charsOf: (msg) => msg.text.length,
  enqueue: (sender, work) => directQueue.add(sender, work),
  run: (_sender, items) => handleTextTurn(items),
  onRejected: (_sender, items, error) => onDirectRejected(items[items.length - 1], error),
});

/**
 * /huy và lệnh chỉ đọc (commands.ts): gửi thẳng Python NGOÀI hàng đợi theo người
 * gửi, timeout ngắn - dùng được khi lượt dài của chính người đó đang chạy.
 * /huy bỏ luôn các tin chữ còn đang chờ debounce (chưa gửi Python); lệnh chỉ đọc
 * đẩy chúng vào hàng đợi trước.
 */
function runFastLane(msg: ParsedMessage): void {
  const cancel = isCancelCommand(msg.text);
  if (cancel) {
    const dropped = batcher.discardBuffered(msg.sender);
    if (dropped) console.log(`[zalo] /huy: bỏ ${dropped} tin chữ chưa gửi của ${msg.sender}`);
  } else {
    batcher.flush(msg.sender);
    if (fastLaneActive >= FAST_LANE_LIMIT) {
      enqueueDirect(msg);
      return;
    }
  }
  fastLaneActive++;
  stats.inc("fast_lane");
  void (async () => {
    try {
      const reply = await callBridge(
        config,
        {
          senderId: msg.sender,
          senderName: msg.senderName,
          conversationId: msg.threadId,
          messageId: msg.id,
          text: msg.text,
        },
        FAST_LANE_TIMEOUT_MS,
      );
      if (reply.messages.length || reply.image_b64) knownSenders.mark(msg.sender);
      await sendReply(msg, reply.messages, reply.image_b64, reply.styles);
    } catch (error) {
      console.error(`[zalo] fast-lane command ${msg.id} failed`, error);
      if (isKnown(msg.sender)) await sendReply(msg, [BRIDGE_ERROR_REPLY]);
    } finally {
      fastLaneActive--;
    }
  })();
}

/** Phân luồng 1 tin riêng theo loại (msgtype.directRoute). */
function routeDirect(msg: ParsedMessage): void {
  switch (directRoute(msg)) {
    case "drop":
      return;
    case "notice":
      // Không phải lượt Python: trả lời ngay, không chờ lượt đang chạy.
      if (unsupportedNotices.allow(`${msg.sender}:${msg.kind}`)) {
        sendNotice(msg, msg.kind as NoticeKind).catch(console.error);
      }
      return;
    case "fast_lane":
      runFastLane(msg);
      return;
    case "batch":
      reactIfQueued(msg);
      batcher.add(msg.sender, msg);
      return;
    case "queue":
      reactIfQueued(msg);
      batcher.close(msg.sender);
      enqueueDirect(msg);
      return;
  }
}

function onMessage(message: ZaloMessage): void {
  if (message.isSelf) return;
  const msg = parseMessage(message);
  if (!msg.id) return;
  if (message.type === ThreadType.Group) {
    if ((!msg.text && !msg.urls.length) || !seen.remember(msg.id)) return;
    handleGroupMessage(message, msg);
    return;
  }
  if (message.type !== ThreadType.User) return;
  // Sticker, ảnh động, tin rỗng: im lặng như trước (không nhớ id, không phản hồi gì).
  if (directRoute(msg) === "drop" || !seen.remember(msg.id)) return;
  stats.inc("direct_messages");
  if (handlePairing(msg)) return;
  routeDirect(msg);
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
    const detail = `listener closed code=${code ?? "?"} ${reason ?? ""}`.trim();
    console.error(`[zalo] ${detail}; sẽ thử đăng nhập lại từ phiên đã lưu`);
    if (api !== next) return;
    api = null;
    state = "closed";
    reportState("closed", detail);
    reconnect.start();
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
  // Người dùng chủ động quét QR: thôi tự kết nối lại từ phiên cũ.
  reconnect.cancel();
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

/**
 * Đăng nhập lại từ phiên đã lưu (env hoặc Python) và gắn listener. Dùng chung
 * cho khởi động và cho kết nối lại sau khi listener đóng. Trả false nếu không
 * có phiên nào để khôi phục; ném lỗi nếu Python/Zalo lỗi.
 */
async function restoreSession(): Promise<boolean> {
  state = "waiting_backend";
  if (!config.controllerId) config.controllerId = await loadController(config);
  const session: SavedSession =
    config.cookie && config.imei
      ? { cookie: config.cookie, imei: config.imei, userAgent: config.userAgent }
      : ((await loadSavedSession(config)) as SavedSession);
  if (!session?.cookie || !session?.imei) return false;
  state = "restoring";
  const credentials: Credentials = {
    cookie: session.cookie,
    imei: session.imei,
    userAgent: session.userAgent || config.userAgent,
  };
  await attach((await zalo.login(credentials)) as ZaloApi);
  return true;
}

/** Số lần liên tiếp Zalo từ chối phiên đã lưu (không phải lỗi mạng) trước khi coi là phiên hỏng. */
const RECONNECT_PERMANENT_FAILURES = 2;
let reconnectPermanentFailures = 0;

/**
 * Listener đóng hẳn (zca-js không tự thử lại): đăng nhập lại từ phiên đã lưu
 * với backoff 5s/15s/60s/5 phút, tối đa MAX_RECONNECT_ATTEMPTS lần. Lỗi tạm
 * (Python đang khởi động lại, mạng) thử tiếp; Zalo từ chối cookie 2 lần liên
 * tiếp hoặc không còn phiên -> "login_required" để chủ bot quét QR lại.
 */
const reconnect = new ReconnectScheduler({
  run: async (attempt) => {
    // Đã đăng nhập (QR) trong lúc chờ: dừng, không đăng nhập chồng phiên.
    if (api) return "connected";
    if (loginPromise) return "retry";
    try {
      if (await restoreSession()) return "connected";
      console.error("[zalo] không còn phiên đã lưu để kết nối lại; cần quét QR");
      return "give_up";
    } catch (error) {
      if (retryableBootstrapError(error)) {
        reconnectPermanentFailures = 0;
        console.warn(`[zalo] kết nối lại lần ${attempt} chưa được (lỗi tạm)`, error);
        return "retry";
      }
      reconnectPermanentFailures++;
      console.error(`[zalo] kết nối lại lần ${attempt} bị Zalo từ chối`, error);
      return reconnectPermanentFailures >= RECONNECT_PERMANENT_FAILURES ? "give_up" : "retry";
    }
  },
  onSchedule: (attempt, delayMs) => {
    if (api) return;
    state = "reconnecting";
    console.warn(`[zalo] thử đăng nhập lại lần ${attempt + 1} sau ${delayMs}ms`);
  },
  onConnected: (attempts) => {
    reconnectPermanentFailures = 0;
    console.log(`[zalo] đã kết nối lại sau ${attempts} lần thử`);
    reportState("connected", `kết nối lại sau ${attempts} lần thử`);
  },
  onGiveUp: (attempts) => {
    reconnectPermanentFailures = 0;
    if (api) return;
    state = "login_required";
    console.error(`[zalo] bỏ cuộc sau ${attempts} lần kết nối lại; cần đăng nhập lại (/zalo)`);
    reportState("login_required", `không kết nối lại được sau ${attempts} lần thử`);
  },
});

async function bootstrap(attempt = 0): Promise<void> {
  // Đã đăng nhập bằng QR trong lúc chờ thử lại: không đăng nhập chồng phiên.
  if (api || loginPromise) return;
  try {
    if (!(await restoreSession())) state = "awaiting_login";
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
    // Bộ đếm hoạt động + hàng đợi hiện tại (Python hiện trên /admin, không có dữ liệu riêng tư).
    stats: stats.snapshot(),
    queue: {
      pending: directQueue.pending,
      running: directQueue.running,
      senders: directQueue.keys,
      batching: batcher.size,
      fastLane: fastLaneActive,
      outboxBudgetLeft: Number.isFinite(outboxBudget.remaining()) ? outboxBudget.remaining() : null,
    },
  }),
  startQr,
  startPairing: () => {
    if (!api) return null;
    pairing = { code: String(randomInt(100000, 1000000)), expires: Date.now() + 300000 };
    return { code: pairing.code, expiresAt: pairing.expires };
  },
  logout: async () => {
    reconnect.cancel();
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
