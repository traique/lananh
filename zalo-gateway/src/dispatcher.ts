/**
 * Xử lý tin Zalo nhận được: lọc trùng, phân luồng tin riêng (msgtype.directRoute),
 * gộp tin chữ, làn nhanh, ảnh/thoại/file, ghép đôi /pair, tin nhóm đang theo dõi, và
 * gửi trả lời (typing/đã xem/reaction cho người quen). Mọi thứ bên ngoài (phiên zca-js,
 * Python, hàng đợi, cấu hình) được tiêm vào để test bằng fake; index.ts chỉ nối dây.
 */
import { randomInt } from "node:crypto";
import { ThreadType } from "zca-js";
import {
  FAST_LANE_TIMEOUT_MS,
  type BridgeResponse,
  type GatewayState,
  type IncomingDirectMessage,
  type IncomingGroupMessage,
  type NoticeKind,
  type NoticeReply,
} from "./bridge.js";
import { DirectTextBatcher } from "./burst.js";
import { isCancelCommand } from "./commands.js";
import type { GatewayConfig } from "./config.js";
import { FastLane } from "./fast-lane.js";
import { TypingKeepalive, fireAndForget, withTimeout } from "./feedback.js";
import {
  BoundedSerialQueue,
  KeyedMutex,
  KeyedRateLimiter,
  KeyedSerialQueue,
  QueueFullError,
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
import { STORE_RETRY_DELAYS_MS, retry } from "./retry.js";
import { jitterMs, sendChunks, type SendChunksOptions } from "./sender.js";
import { GatewayStats } from "./stats.js";
import {
  KnownSenders,
  SeenIds,
  errorMessage,
  isTransientNetworkError,
  sleep as defaultSleep,
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
import { parseVoice, sendVoiceReply, type VoiceClient } from "./voice.js";

/** Lệnh gọi Python của dispatcher (index.ts nối tới bridge.ts kèm config). */
export type DispatcherBridge = {
  /** POST /message; `timeoutMs` ngắn cho làn nhanh. */
  message(message: IncomingDirectMessage, timeoutMs?: number): Promise<BridgeResponse>;
  image(
    senderId: string,
    messageId: string,
    caption: string,
    mime: string,
    bytes: Uint8Array<ArrayBuffer>,
  ): Promise<BridgeResponse>;
  voice(
    senderId: string,
    messageId: string,
    mime: string,
    bytes: Uint8Array<ArrayBuffer>,
    durationMs: number | null,
  ): Promise<BridgeResponse>;
  file(
    senderId: string,
    messageId: string,
    fileName: string,
    caption: string,
    mime: string,
    bytes: Uint8Array<ArrayBuffer>,
  ): Promise<BridgeResponse>;
  notice(senderId: string, kind: NoticeKind): Promise<NoticeReply>;
  storeGroupMessage(message: IncomingGroupMessage): Promise<void>;
  saveController(id: string): Promise<void>;
  /** Báo trạng thái gateway về Python, không chờ (lỗi tự log). */
  reportState(state: GatewayState, detail?: string): void;
};

export type DispatcherQueues = {
  /** Tin riêng: tuần tự theo người gửi, song song tối đa ZALO_DIRECT_CONCURRENCY người. */
  direct: KeyedSerialQueue;
  /** Tin chữ nhóm /tongket + tín hiệu: lưu sang Python tuần tự, hàng riêng. */
  summary: BoundedSerialQueue;
  /** Gửi tin tuần tự theo người nhận (không xen đoạn của 2 câu trả lời). */
  sendLock: KeyedMutex;
};

/** Nhóm đang theo dõi (groups.TrackedGroups) + vai trò người gửi (group-roles.ts). */
export type DispatcherGroups = {
  isSummary(groupId: string): boolean;
  isFacebook(groupId: string): boolean;
  roleOf(groupId: string, senderId: string): Promise<string>;
  /** Nạp lại danh sách nhóm từ Python (sau /themnhom... gõ từ Zalo). */
  refresh(): Promise<void>;
};

/** Phần cấu hình dispatcher đọc; controllerId được GHI khi /pair thành công. */
export type DispatcherConfig = Pick<
  GatewayConfig,
  | "controllerId"
  | "userAgent"
  | "bridgeBaseUrl"
  | "bridgeSecret"
  | "presenceEvents"
  | "voiceMaxBytes"
  | "documentMaxBytes"
  | "directDebounceMs"
  | "directDebounceMaxMs"
  | "directDebounceMaxMessages"
  | "sendGapMinMs"
  | "sendGapMaxMs"
>;

/** Tải media từ CDN Zalo (media.ts); test thay bằng fake. */
export type MediaDownloads = {
  image(urls: string[], options: DownloadOptions): Promise<MediaItem>;
  audio(urls: string[], options: DownloadOptions, maxBytes: number): Promise<MediaItem>;
  document(
    urls: string[],
    ext: string,
    options: DownloadOptions,
    maxBytes: number,
  ): Promise<MediaItem>;
};

export type DispatcherDeps = {
  /** Phiên Zalo hiện tại; null = chưa đăng nhập hoặc mất kết nối. */
  api(): ZaloApi | null;
  bridge: DispatcherBridge;
  queues: DispatcherQueues;
  config: DispatcherConfig;
  groups: DispatcherGroups;
  /** Tin của nhóm nguồn Facebook (index.ts: tải ảnh + gộp bài). */
  facebook?(msg: ParsedMessage): void;
  stats?: GatewayStats;
  knownSenders?: KnownSenders;
  seen?: Pick<SeenIds, "remember">;
  downloads?: MediaDownloads;
  sleep?(ms: number): Promise<void>;
  now?(): number;
  /** Mã ghép đôi /pair (mặc định 6 chữ số ngẫu nhiên). */
  pairingCode?(): string;
};

const DEFAULT_DOWNLOADS: MediaDownloads = {
  image: downloadImage,
  audio: downloadAudio,
  document: downloadDocument,
};

const GROUP_ADMIN_COMMAND = /^\/(themnhom|xoanhom|fb_themnhom|fb_xoanhom)\b/i;
/** Làn nhanh: tối đa chừng này lệnh chỉ đọc chờ Python cùng lúc; vượt thì vào hàng đợi như thường. */
const FAST_LANE_LIMIT = 4;
/** /huy chờ Python cùng lúc (mỗi người tối đa 1, trần riêng); vượt thì bỏ. */
const FAST_LANE_CANCEL_LIMIT = 4;
/** Timeout mỗi lệnh typing/seen/reaction của zca-js (gọi không chờ). */
const PRESENCE_TIMEOUT_MS = 5000;
/** Gộp tin chữ lúc tới lượt: trần số tin / ký tự của 1 lượt gộp. */
const MERGE_MAX_MESSAGES = 8;
const MERGE_MAX_CHARS = 4000;
/** Mã /pair sống chừng này kể từ lúc chủ bot gõ /zalo. */
const PAIRING_TTL_MS = 300000;
export const BUSY_REPLY =
  "⏳ Bot đang trả lời nhiều tin cùng lúc nên chưa nhận được tin vừa rồi. Bạn gửi lại sau ít phút nhé.";
export const BRIDGE_ERROR_REPLY =
  "⚠️ Bot đang gặp sự cố nên chưa trả lời được tin vừa rồi. Bạn thử gửi lại sau ít phút nhé.";
// Lỗi ảnh: người gửi chỉ thấy câu cố định, chủ bot thấy thêm chi tiết (userErrorReply).
export const IMAGE_ERROR_REPLY =
  "⚠️ Bot chưa xử lý được ảnh vừa rồi (AI đang bận hoặc ảnh không tải được). Bạn thử gửi lại sau ít phút nhé.";
const IMAGE_SEND_ERROR_REPLY =
  "⚠️ Ảnh đã tạo xong nhưng gửi qua Zalo bị lỗi. Bạn thử lại lệnh sau ít phút nhé.";
export const PAIRED_REPLY = "✅ Ghép đôi thành công. Tài khoản này giờ có thể điều khiển bot B.";

type TurnFeedback = { stop(): void };
const NO_FEEDBACK: TurnFeedback = { stop: () => undefined };

export class Dispatcher {
  /** Người gửi Python đã trả lời (đã pair): chỉ họ nhận câu báo bận/sự cố. */
  readonly knownSenders: KnownSenders;
  private readonly stats: GatewayStats;
  private readonly seen: Pick<SeenIds, "remember">;
  private readonly downloads: MediaDownloads;
  private readonly sleep: (ms: number) => Promise<void>;
  private readonly now: () => number;
  // Báo "đang bận" tối đa 1 lần/phút cho mỗi người khi hàng đợi đầy.
  private readonly busyNotices = new KeyedRateLimiter(60000, 256);
  // Câu "chưa hỗ trợ" (video, vị trí, file lạ): tối đa 1 lần/phút mỗi người mỗi loại.
  private readonly unsupportedNotices = new KeyedRateLimiter(60000, 512);
  // "Em nhận được ảnh rồi...": 1 lần/phút mỗi người (gửi album 5 ảnh không thành 5 câu).
  private readonly imageNotices = new KeyedRateLimiter(60000, 256);
  // Reaction "đang chờ lượt trước": tối đa 1 lần/15s mỗi người.
  private readonly queuedReactions = new KeyedRateLimiter(15000, 256);
  private readonly batcher: DirectTextBatcher<ParsedMessage>;
  private readonly fastLane: FastLane<ParsedMessage>;
  private pairing: { code: string; expires: number } | null = null;
  private closed = false;

  constructor(private readonly deps: DispatcherDeps) {
    this.knownSenders = deps.knownSenders ?? new KnownSenders(2000);
    this.stats = deps.stats ?? new GatewayStats();
    this.seen = deps.seen ?? new SeenIds(5000);
    this.downloads = deps.downloads ?? DEFAULT_DOWNLOADS;
    this.sleep = deps.sleep ?? defaultSleep;
    this.now = deps.now ?? Date.now;
    const config = deps.config;
    // Tin chữ thường: debounce + gộp lúc tới lượt (burst.ts), mỗi lô là 1 việc của
    // hàng đợi tin riêng nên vẫn tuần tự theo người gửi và chịu trần ZALO_DIRECT_CONCURRENCY.
    this.batcher = new DirectTextBatcher<ParsedMessage>({
      debounceMs: config.directDebounceMs,
      maxWaitMs: config.directDebounceMaxMs,
      maxBufferItems: config.directDebounceMaxMessages,
      maxMergeItems: MERGE_MAX_MESSAGES,
      maxMergeChars: MERGE_MAX_CHARS,
      maxSenders: 256,
      charsOf: (msg) => msg.text.length,
      now: this.now,
      enqueue: (sender, work) => deps.queues.direct.add(sender, work),
      run: (_sender, items) => this.handleTextTurn(items),
      onRejected: (_sender, items, error) => {
        const last = items[items.length - 1];
        if (last) this.onDirectRejected(last, error);
      },
    });
    /**
     * /huy và lệnh chỉ đọc (commands.ts): gửi thẳng Python NGOÀI hàng đợi theo người
     * gửi, có trần (fast-lane.ts) - dùng được khi lượt dài của chính người đó đang
     * chạy. /huy bỏ luôn các tin chữ còn đang chờ debounce (chưa gửi Python); lệnh
     * chỉ đọc đẩy chúng vào hàng đợi trước.
     */
    this.fastLane = new FastLane<ParsedMessage>({
      commandLimit: FAST_LANE_LIMIT,
      cancelLimit: FAST_LANE_CANCEL_LIMIT,
      isCancel: (msg) => isCancelCommand(msg.text),
      batcher: this.batcher,
      call: (msg) => this.callFastLane(msg),
      enqueue: (msg) => {
        this.reactIfQueued(msg);
        this.enqueueDirect(msg);
      },
    });
  }

  // ─── Trạng thái cho /status và lúc tắt ────────────────────────────────────

  /** Số người gửi đang có lô tin chữ mở (debounce hoặc chờ lượt). */
  get batching(): number {
    return this.batcher.size;
  }

  /** Số lệnh làn nhanh đang chờ Python. */
  get fastLaneActive(): number {
    return this.fastLane.active;
  }

  /** Không còn việc tin riêng/tin nhóm/gửi tin nào đang chờ hay đang chạy. */
  get idle(): boolean {
    const { direct, summary, sendLock } = this.deps.queues;
    return (
      direct.pending === 0 &&
      direct.running === 0 &&
      this.batcher.size === 0 &&
      this.fastLane.active === 0 &&
      summary.pending === 0 &&
      sendLock.size === 0
    );
  }

  /**
   * Lúc tắt (SIGTERM): thôi nhận tin mới; tin chữ đang chờ gộp vào hàng đợi NGAY để
   * tin đã nhận vẫn tới Python trong thời gian chờ hàng đợi xong.
   */
  close(): void {
    this.closed = true;
    this.batcher.flushAll();
  }

  // ─── Người quen / ghép đôi (control server) ───────────────────────────────

  /**
   * Người gửi chắc chắn đã pair: Python từng trả lời họ, có trong danh sách Python gửi
   * lúc gắn phiên (seedKnownSenders), hoặc chính là chủ bot. Chỉ họ nhận câu báo do
   * gateway tự gửi, typing, "đã xem" - người lạ vẫn im lặng hoàn toàn như phía Python.
   */
  isKnown(sender: string): boolean {
    const controller = this.deps.config.controllerId;
    return this.knownSenders.has(sender) || (!!controller && sender === controller);
  }

  /** Nhớ sẵn người đã pair (GET /known-senders lúc gắn phiên); trả số id đã nhớ. */
  seedKnownSenders(ids: readonly string[]): number {
    let seeded = 0;
    for (const id of ids) {
      if (!id) continue;
      this.knownSenders.mark(id);
      seeded++;
    }
    return seeded;
  }

  /** Python gỡ pairing (/zaloxoa): quên ngay; true nếu đang nhớ người này. */
  forgetSender(id: string): boolean {
    return this.knownSenders.forget(id);
  }

  /** Chủ bot gõ /zalo: mã /pair mới (5 phút, dùng 1 lần); null khi Zalo chưa kết nối. */
  startPairing(): { code: string; expiresAt: number } | null {
    if (!this.deps.api()) return null;
    const code = this.deps.pairingCode?.() ?? String(randomInt(100000, 1000000));
    this.pairing = { code, expires: this.now() + PAIRING_TTL_MS };
    return { code, expiresAt: this.pairing.expires };
  }

  /** Đăng xuất (/zalologout): bỏ mã ghép đôi đang chờ. */
  clearPairing(): void {
    this.pairing = null;
  }

  // ─── Cửa vào ──────────────────────────────────────────────────────────────

  onMessage(message: ZaloMessage): void {
    if (this.closed || message.isSelf) return;
    if (message.type === ThreadType.Group) {
      // Nhóm không theo dõi (/tongket, tín hiệu, nguồn Facebook): bỏ ngay, không parse và
      // KHÔNG ghi SeenIds - trước đây tin của mọi nhóm đẩy id tin riêng ra khỏi LRU.
      const groupId = String(message.threadId);
      if (!this.deps.groups.isSummary(groupId) && !this.deps.groups.isFacebook(groupId)) return;
    }
    const msg = parseMessage(message);
    if (!msg.id) return;
    if (message.type === ThreadType.Group) {
      if ((!msg.text && !msg.urls.length) || !this.seen.remember(msg.id)) return;
      this.handleGroupMessage(message, msg);
      return;
    }
    if (message.type !== ThreadType.User) return;
    // Sticker, ảnh động, tin rỗng: im lặng như trước (không nhớ id, không phản hồi gì).
    if (directRoute(msg) === "drop" || !this.seen.remember(msg.id)) return;
    this.stats.inc("direct_messages");
    if (this.handlePairing(msg)) return;
    this.routeDirect(msg);
  }

  /** Phân luồng 1 tin riêng theo loại (msgtype.directRoute). */
  routeDirect(msg: ParsedMessage): void {
    switch (directRoute(msg)) {
      case "drop":
        return;
      case "notice":
        // Không phải lượt Python: trả lời ngay, không chờ lượt đang chạy.
        if (this.unsupportedNotices.allow(`${msg.sender}:${msg.kind}`, this.now())) {
          this.sendNotice(msg, msg.kind as NoticeKind).catch(console.error);
        }
        return;
      case "fast_lane":
        this.runFastLane(msg);
        return;
      case "batch":
        this.reactIfQueued(msg);
        this.batcher.add(msg.sender, msg);
        return;
      case "queue":
        this.reactIfQueued(msg);
        this.batcher.close(msg.sender);
        this.enqueueDirect(msg);
        return;
    }
  }

  /** Tải ảnh bằng cookie + user agent của phiên Zalo hiện tại (cả nhóm nguồn Facebook). */
  fetchImage(urls: string[], attempts = 2): Promise<MediaItem> {
    return this.fetchMedia((options) => this.downloads.image(urls, options), attempts);
  }

  // ─── Gửi tin ──────────────────────────────────────────────────────────────

  private notePaired(sender: string, reply: BridgeResponse): void {
    this.knownSenders.noteReply(sender, reply.paired, !!(reply.messages.length || reply.image_b64));
  }

  private async sendImage(client: ZaloApi, target: string, imageB64: string): Promise<void> {
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
        if (attempt > 0) await this.sleep(1500 * attempt);
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
      {
        msg: userErrorReply(
          lastError,
          target,
          this.deps.config.controllerId,
          IMAGE_SEND_ERROR_REPLY,
        ),
      },
      target,
      ThreadType.User,
    );
  }

  /** Khoảng nghỉ ngẫu nhiên giữa 2 tin liên tiếp (ZALO_SEND_GAP_MIN_MS..MAX_MS). */
  private sendGap(): number {
    return jitterMs(this.deps.config.sendGapMinMs, this.deps.config.sendGapMaxMs);
  }

  private sendOptions(client: ZaloApi): SendChunksOptions {
    return {
      threadType: ThreadType.User,
      isCurrent: () => this.deps.api() === client,
      gapMs: () => this.sendGap(),
      sleep: this.sleep,
      onChunkSent: () => this.stats.inc("chunks_sent"),
      onPlainFallback: () => this.stats.inc("plain_fallbacks"),
    };
  }

  /**
   * Gửi từng đoạn (kèm styles zca-js nếu Python gửi), nghỉ ngẫu nhiên giữa các
   * đoạn; mỗi đoạn thử lại theo SEND_RETRY_DELAYS_MS (1s, 3s, 8s) để không gửi
   * trùng đoạn đã tới nơi, Zalo từ chối định dạng thì gửi lại 1 lần chữ thuần
   * (sender.ts). Mất đăng nhập giữa chừng thì dừng ngay. Giữ sendLock của người
   * nhận suốt cả câu trả lời (mọi đoạn + ảnh).
   */
  private send(
    target: string,
    chunks: string[],
    imageB64?: string | null,
    styles?: unknown,
    voice?: unknown,
  ): Promise<void> {
    return this.deps.queues.sendLock.run(target, async () => {
      const client = this.deps.api();
      if (!client) throw new Error("Zalo is not connected");
      const sent = await sendChunks(client, target, chunks, styles, this.sendOptions(client));
      if (imageB64) {
        const gap = sent > 0 ? this.sendGap() : 0;
        if (gap > 0) await this.sleep(gap);
        await this.sendImage(client, target, imageB64);
      }
      // Tin thoại /noi đi SAU tin chữ; lỗi chỉ log (voice.ts không ném).
      const clip = parseVoice(voice);
      if (clip) {
        const gap = sent > 0 ? this.sendGap() : 0;
        if (gap > 0) await this.sleep(gap);
        const how = await sendVoiceReply(client as VoiceClient, target, clip, {
          bridgeBaseUrl: this.deps.config.bridgeBaseUrl,
          bridgeSecret: this.deps.config.bridgeSecret,
          threadType: ThreadType.User,
        });
        this.stats.inc(how ? "voice_replies" : "voice_reply_failures");
      }
    });
  }

  /** Gửi 1 câu báo ngắn đúng 1 lần (không thử lại, timeout ngắn); lỗi chỉ log. */
  private sendOnce(target: string, text: string): Promise<boolean> {
    return this.deps.queues.sendLock.run(target, async () => {
      const client = this.deps.api();
      if (!client) return false;
      try {
        await withTimeout(client.sendMessage({ msg: text }, target, ThreadType.User), 15000);
        this.stats.inc("chunks_sent");
        return true;
      } catch (error) {
        console.warn(`[zalo] gửi câu báo tới ${target} lỗi: ${errorMessage(error, String(error))}`);
        return false;
      }
    });
  }

  /**
   * Gửi trả lời cho 1 tin riêng. Hết lượt thử lại thì log + báo Python
   * ("send_failed") để chủ bot biết, không ném lỗi nữa (không còn gì để làm).
   */
  private async sendReply(
    msg: ParsedMessage,
    chunks: string[],
    imageB64?: string | null,
    styles?: unknown,
    voice?: unknown,
  ): Promise<void> {
    try {
      await this.send(msg.threadId, chunks, imageB64, styles, voice);
    } catch (error) {
      this.stats.inc("send_failures");
      const reason = errorMessage(error, String(error));
      console.error(
        `[zalo] gửi trả lời tin ${msg.id} tới ${msg.sender} thất bại sau khi thử lại`,
        error,
      );
      this.deps.bridge.reportState("send_failed", `tin ${msg.id}: ${reason}`);
    }
  }

  // ─── Phản hồi trong lúc chờ (typing, đã xem, reaction) ───────────────────

  private presenceError(label: string): (error: unknown) => void {
    return (error) => {
      this.stats.inc("presence_errors");
      console.warn(`[zalo] ${label} lỗi (bỏ qua): ${errorMessage(error, String(error))}`);
    };
  }

  /**
   * Bắt đầu 1 lượt gửi Python: đánh dấu "đã xem" các tin của lượt (đủ tham số
   * zca-js cần) và hiện "đang soạn tin", làm mới mỗi ~6s, tối đa 30 lần (~3 phút).
   * Gọi không chờ, timeout ngắn, lỗi bỏ qua - không bao giờ chặn hàng đợi. Chỉ cho
   * người quen (người lạ không được biết bot đã đọc tin).
   */
  private startTurnFeedback(
    sender: string,
    threadId: string,
    items: ParsedMessage[],
  ): TurnFeedback {
    const client = this.deps.api();
    if (!client || !this.deps.config.presenceEvents || !this.isKnown(sender)) return NO_FEEDBACK;
    const seenList = items.map((item) => item.seen).filter((s): s is ZaloSeenParams => !!s);
    if (seenList.length) {
      fireAndForget(
        async () => {
          await client.sendSeenEvent(seenList.slice(-50), ThreadType.User);
          this.stats.inc("seen_events");
        },
        PRESENCE_TIMEOUT_MS,
        this.presenceError("sendSeenEvent"),
      );
    }
    const typing = new TypingKeepalive(
      () => client.sendTypingEvent(threadId, ThreadType.User, DEST_TYPE_USER),
      {
        intervalMs: 6000,
        maxEvents: 30,
        timeoutMs: PRESENCE_TIMEOUT_MS,
        onSent: () => this.stats.inc("typing_events"),
        onError: this.presenceError("sendTypingEvent"),
      },
    ).start();
    return { stop: () => typing.stop() };
  }

  /** Tin phải chờ sau lượt đang chạy của CHÍNH người gửi: thả 👌 để họ biết bot đã thấy. */
  private reactIfQueued(msg: ParsedMessage): void {
    const client = this.deps.api();
    if (!client || !this.deps.config.presenceEvents || !msg.seen || !this.isKnown(msg.sender)) {
      return;
    }
    if (!this.deps.queues.direct.isRunning(msg.sender)) return;
    if (!this.queuedReactions.allow(msg.sender, this.now())) return;
    const target = {
      data: { msgId: msg.seen.msgId, cliMsgId: msg.seen.cliMsgId },
      threadId: msg.threadId,
      type: ThreadType.User,
    };
    fireAndForget(
      async () => {
        await client.addReaction(REACTION_QUEUED, target);
        this.stats.inc("reactions");
      },
      PRESENCE_TIMEOUT_MS,
      this.presenceError("addReaction"),
    );
  }

  /**
   * Gửi câu báo dựng sẵn (ảnh đã nhận, tin thoại/file lỗi, loại tin chưa hỗ trợ).
   * Câu do Python dựng (POST /notice: giọng Lan Anh, đúng cách gọi riêng, người lạ
   * -> null = im lặng); Python không trả lời được thì người quen nhận câu trung
   * tính LOCAL_NOTICES.
   */
  private async sendNotice(msg: ParsedMessage, kind: NoticeKind): Promise<boolean> {
    const text = await noticeText(msg.sender, kind, {
      fetch: (sender, noticeKind) => this.deps.bridge.notice(sender, noticeKind),
      isKnown: (sender) => this.isKnown(sender),
      onPaired: (sender) => this.knownSenders.mark(sender),
      onStranger: (sender) => this.knownSenders.forget(sender),
    });
    if (!text) return false;
    this.stats.inc("notices");
    return this.sendOnce(msg.threadId, text);
  }

  // ─── Media ────────────────────────────────────────────────────────────────

  /** Tải media bằng cookie + user agent của phiên Zalo hiện tại, thử `attempts` lần. */
  private async fetchMedia(
    download: (options: DownloadOptions) => Promise<MediaItem>,
    attempts: number,
  ): Promise<MediaItem> {
    let lastError: unknown = null;
    for (let attempt = 0; attempt < attempts; attempt++) {
      if (attempt > 0) await this.sleep(2000);
      const ctx = this.deps.api()?.getContext?.();
      try {
        return await download({
          userAgent: ctx?.userAgent || this.deps.config.userAgent,
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

  // ─── Xử lý tin nhắn ──────────────────────────────────────────────────────

  /** Trả về true nếu tin nhắn là mã ghép đôi hợp lệ (đã xử lý). */
  private handlePairing(msg: ParsedMessage): boolean {
    const pairing = this.pairing;
    if (!pairing || !msg.text) return false;
    if (this.now() > pairing.expires) {
      this.pairing = null;
      return false;
    }
    if (msg.text !== `/pair ${pairing.code}`) return false;
    this.batcher.close(msg.sender);
    this.deps.queues.direct
      .add(msg.sender, async () => {
        // Lưu bên Python xong mới nhận trong RAM: lưu lỗi thì không ai được coi là chủ bot.
        await this.deps.bridge.saveController(msg.sender);
        this.deps.config.controllerId = msg.sender;
        this.pairing = null;
        await this.send(msg.threadId, [PAIRED_REPLY]);
      })
      .catch(console.error);
    return true;
  }

  private handleGroupMessage(message: ZaloMessage, msg: ParsedMessage): void {
    const gid = msg.threadId;
    let sentAtMs = Number(message.data?.ts || this.now());
    if (sentAtMs < 1e12) sentAtMs *= 1000;
    // Ảnh kèm chú thích: lưu chú thích (luồng Facebook vốn đã đọc text || caption).
    const text = msg.text || msg.caption;
    if (this.deps.groups.isSummary(gid) && text) {
      this.deps.queues.summary
        .add(async () => {
          // Vai trò TẠI LÚC NHẮN; lấy lỗi -> "unknown" (Python không coi là admin).
          const senderRole = await this.deps.groups.roleOf(gid, msg.sender);
          await retry(
            () =>
              this.deps.bridge.storeGroupMessage({
                groupId: gid,
                messageId: msg.id,
                senderId: msg.sender,
                senderName: msg.senderName,
                text,
                sentAtMs,
                quoteText: msg.quoteText,
                senderRole,
              }),
            { delaysMs: STORE_RETRY_DELAYS_MS, sleep: this.sleep },
          );
          this.stats.inc("group_messages");
        })
        .catch((error) => {
          this.stats.inc("group_store_failures");
          console.error(
            `[zalo] lưu tin nhóm ${msg.id} (nhóm ${gid}) thất bại` +
              (error instanceof QueueFullError
                ? " - hàng đợi tin nhóm đầy"
                : ` sau ${STORE_RETRY_DELAYS_MS.length + 1} lần`),
            error,
          );
        });
    }
    if (this.deps.groups.isFacebook(gid)) this.deps.facebook?.(msg);
  }

  /**
   * 1 lượt chữ (1 tin, hoặc nhiều tin chữ liên tiếp đã gộp): nối bằng xuống dòng,
   * gửi 1 lần với id tin CUỐI (Python cache idempotency theo id này), kèm chữ tin
   * được trích dẫn gần nhất. Typing/đã xem trong lúc chờ.
   */
  private async handleTextTurn(items: ParsedMessage[]): Promise<void> {
    const last = items[items.length - 1];
    if (!last) return;
    const text = items.map((item) => item.text).join("\n");
    const quoteText = [...items].reverse().find((item) => item.quoteText)?.quoteText || "";
    this.stats.inc("text_turns");
    if (items.length > 1) this.stats.inc("merged_messages", items.length - 1);
    const feedback = this.startTurnFeedback(last.sender, last.threadId, items);
    let reply: BridgeResponse;
    try {
      reply = await this.deps.bridge.message({
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
      if (this.isKnown(last.sender)) await this.sendReply(last, [BRIDGE_ERROR_REPLY]);
      return;
    }
    feedback.stop();
    this.notePaired(last.sender, reply);
    await this.sendReply(last, reply.messages, reply.image_b64, reply.styles, reply.voice);
    if (GROUP_ADMIN_COMMAND.test(text)) await this.deps.groups.refresh();
  }

  /**
   * Ảnh (chat.photo, chat.doodle, file ảnh): câu "đã nhận ảnh" gửi TRƯỚC khi tải
   * và gọi Python (trước đây gửi chung với kết quả - không báo được gì).
   */
  private async handleImage(msg: ParsedMessage): Promise<void> {
    this.stats.inc("image_turns");
    const feedback = this.startTurnFeedback(msg.sender, msg.threadId, [msg]);
    try {
      if (this.imageNotices.allow(msg.sender, this.now())) await this.sendNotice(msg, "image");
      let image: MediaItem | null = await this.fetchImage(msg.urls, 1);
      const pending = this.deps.bridge.image(
        msg.sender,
        msg.id,
        msg.caption,
        image.mime,
        image.bytes,
      );
      // Bridge đã chép ảnh vào body gửi đi: thả bản tải về (tới 8 MB) để GC thu ngay
      // thay vì giữ suốt lượt Python (tới 240s).
      image = null;
      const reply = await pending;
      feedback.stop();
      this.notePaired(msg.sender, reply);
      if (reply.messages.length) await this.sendReply(msg, reply.messages, null, reply.styles);
    } catch (error) {
      feedback.stop();
      console.error(`[zalo] image bridge failed for message ${msg.id}`, error);
      // Người lạ chưa pair: im lặng như phía Python.
      if (this.isKnown(msg.sender)) {
        await this.sendReply(msg, [
          userErrorReply(error, msg.sender, this.deps.config.controllerId, IMAGE_ERROR_REPLY),
        ]);
      }
    }
  }

  /**
   * Tin thoại (chat.voice): tải bản ghi (ưu tiên m4a, chỉ domain Zalo, trần
   * VOICE_MAX_BYTES, kiểm magic bytes âm thanh) rồi POST /voice - Python chép lời
   * và trả lời như tin chữ. Không tải được -> câu "em chưa nghe được" thay vì lỗi ảnh.
   */
  private async handleVoice(msg: ParsedMessage): Promise<void> {
    this.stats.inc("voice_turns");
    const feedback = this.startTurnFeedback(msg.sender, msg.threadId, [msg]);
    let audio: MediaItem | null;
    try {
      audio = await this.fetchMedia(
        (options) => this.downloads.audio(msg.voiceUrls, options, this.deps.config.voiceMaxBytes),
        2,
      );
    } catch (error) {
      feedback.stop();
      console.warn(
        `[zalo] không tải được tin thoại ${msg.id}: ${errorMessage(error, String(error))}`,
      );
      const tooLarge = error instanceof MediaDownloadError && error.tooLarge;
      await this.sendNotice(msg, tooLarge ? "voice_too_large" : "voice_failed");
      return;
    }
    let reply: BridgeResponse;
    try {
      const pending = this.deps.bridge.voice(
        msg.sender,
        msg.id,
        audio.mime,
        audio.bytes,
        msg.durationMs,
      );
      audio = null; // bridge đã chép vào body gửi đi: không giữ suốt lượt Python
      reply = await pending;
    } catch (error) {
      feedback.stop();
      console.error(`[zalo] voice bridge failed for message ${msg.id}`, error);
      await this.sendNotice(msg, "voice_failed");
      return;
    }
    feedback.stop();
    this.notePaired(msg.sender, reply);
    await this.sendReply(msg, reply.messages, reply.image_b64, reply.styles, reply.voice);
  }

  /**
   * File tài liệu (share.file PDF/DOCX/TXT): kiểm dung lượng Zalo khai báo trước
   * khi tải, tải trong trần DOCUMENT_MAX_BYTES, nội dung phải khớp đuôi, rồi POST
   * /file (không caption -> Python tóm tắt).
   */
  private async handleFile(msg: ParsedMessage): Promise<void> {
    const file = msg.file;
    if (!file) return;
    if (file.size && file.size > this.deps.config.documentMaxBytes) {
      await this.sendNotice(msg, "file_too_large");
      return;
    }
    this.stats.inc("file_turns");
    const feedback = this.startTurnFeedback(msg.sender, msg.threadId, [msg]);
    let document: MediaItem | null;
    try {
      document = await this.fetchMedia(
        (options) =>
          this.downloads.document(
            msg.fileUrls,
            file.ext,
            options,
            this.deps.config.documentMaxBytes,
          ),
        2,
      );
    } catch (error) {
      feedback.stop();
      console.warn(`[zalo] không tải được file ${msg.id}: ${errorMessage(error, String(error))}`);
      const tooLarge = error instanceof MediaDownloadError && error.tooLarge;
      await this.sendNotice(msg, tooLarge ? "file_too_large" : "file_failed");
      return;
    }
    let reply: BridgeResponse;
    try {
      const pending = this.deps.bridge.file(
        msg.sender,
        msg.id,
        file.name || `tai-lieu.${file.ext}`,
        "",
        document.mime,
        document.bytes,
      );
      document = null; // bridge đã chép vào body gửi đi: không giữ suốt lượt Python
      reply = await pending;
    } catch (error) {
      feedback.stop();
      console.error(`[zalo] file bridge failed for message ${msg.id}`, error);
      if (this.isKnown(msg.sender)) await this.sendReply(msg, [BRIDGE_ERROR_REPLY]);
      return;
    }
    feedback.stop();
    this.notePaired(msg.sender, reply);
    await this.sendReply(msg, reply.messages, reply.image_b64, reply.styles);
  }

  /** 1 tin (không gộp) khi tới lượt trong hàng đợi theo người gửi. */
  private async handleDirectMessage(msg: ParsedMessage): Promise<void> {
    if (msg.kind === "link") {
      // Tin chia sẻ link: title chứa link = chữ người dùng gõ kèm link, giữ nguyên;
      // ngược lại title chỉ là tiêu đề trang -> gửi riêng link (Python coi là
      // "đọc bài này"). Thumb xem trước không phải tin ảnh.
      await this.handleTextTurn([
        { ...msg, text: msg.text.includes(msg.link) ? msg.text : msg.link, urls: [] },
      ]);
      return;
    }
    if (msg.kind === "image" && msg.urls.length && !msg.text.startsWith("/")) {
      await this.handleImage(msg);
      return;
    }
    if (msg.kind === "voice") {
      await this.handleVoice(msg);
      return;
    }
    if (msg.kind === "file") {
      await this.handleFile(msg);
      return;
    }
    if (!msg.text) return;
    // /nhomzalo do Python xử lý (mọi kênh, kiểm quyền admin) qua GET /groups của control server.
    await this.handleTextTurn([msg]);
  }

  /** Hàng đợi từ chối (đầy) hoặc lượt ném lỗi ngoài dự kiến. */
  private onDirectRejected(msg: ParsedMessage, error: unknown): void {
    if (!(error instanceof QueueFullError)) {
      console.error(`[zalo] direct message ${msg.id} failed`, error);
      return;
    }
    const direct = this.deps.queues.direct;
    this.stats.inc("busy_rejections");
    console.warn(
      `[zalo] bỏ tin ${msg.id} của ${msg.sender}: ${error.message} ` +
        `(đang chờ ${direct.pending}, đang chạy ${direct.running})`,
    );
    if (!this.isKnown(msg.sender) || !this.busyNotices.allow(msg.sender, this.now())) return;
    this.send(msg.threadId, [BUSY_REPLY]).catch((sendError) =>
      console.error("[zalo] busy notice failed", sendError),
    );
  }

  private enqueueDirect(msg: ParsedMessage): void {
    this.deps.queues.direct
      .add(msg.sender, () => this.handleDirectMessage(msg))
      .catch((error) => this.onDirectRejected(msg, error));
  }

  /** 1 lệnh làn nhanh: gửi thẳng Python, timeout ngắn; lỗi thì báo người quen. */
  private async callFastLane(msg: ParsedMessage): Promise<void> {
    this.stats.inc("fast_lane");
    try {
      const reply = await this.deps.bridge.message(
        {
          senderId: msg.sender,
          senderName: msg.senderName,
          conversationId: msg.threadId,
          messageId: msg.id,
          text: msg.text,
        },
        FAST_LANE_TIMEOUT_MS,
      );
      this.notePaired(msg.sender, reply);
      await this.sendReply(msg, reply.messages, reply.image_b64, reply.styles);
    } catch (error) {
      console.error(`[zalo] fast-lane command ${msg.id} failed`, error);
      if (this.isKnown(msg.sender)) await this.sendReply(msg, [BRIDGE_ERROR_REPLY]);
    }
  }

  private runFastLane(msg: ParsedMessage): void {
    const result = this.fastLane.submit(msg);
    if (result === "coalesced" || result === "dropped") this.stats.inc("fast_lane_dropped");
  }
}
