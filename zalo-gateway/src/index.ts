/**
 * Điểm vào gateway: chỉ nối dây. Xử lý tin và gửi trả lời của Python (reply.messages,
 * reply.image_b64, reply.voice) ở dispatcher.ts, phiên Zalo ở session.ts, outbox ở
 * outbox.ts, tắt êm ở shutdown.ts, control server ở control-server.ts.
 */
import { ThreadType, Zalo } from "zca-js";
import {
  ackOutbox,
  callBridge,
  callFileBridge,
  callImageBridge,
  callVoiceBridge,
  clearController,
  clearSession,
  fetchAllowedGroups,
  fetchFacebookGroups,
  fetchKnownSenders,
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
  type GatewayState,
} from "./bridge.js";
import { loadConfig } from "./config.js";
import { startControlServer } from "./control-server.js";
import { Dispatcher } from "./dispatcher.js";
import { FacebookPostBuffer } from "./facebook-buffer.js";
import { GroupRoles, ROLE_EVENTS } from "./group-roles.js";
import { TrackedGroups, collectGroups, sharedScan } from "./groups.js";
import { BoundedSerialQueue, KeyedMutex, KeyedSerialQueue, SlidingWindowBudget } from "./limits.js";
import type { ParsedMessage } from "./msgtype.js";
import { OutboxDelivery, OutboxPoller } from "./outbox.js";
import { jitterMs } from "./sender.js";
import { ZaloSession } from "./session.js";
import { gracefulShutdown, installShutdownHandlers } from "./shutdown.js";
import { GatewayStats } from "./stats.js";
import { errorMessage, sleep } from "./text.js";
import type { MediaItem } from "./types.js";

const config = loadConfig();
/** Câu trả lời trước cho cùng người nhận treo: câu sau chờ tối đa chừng này rồi gửi. */
const SEND_LOCK_MAX_WAIT_MS = 60000;
const zalo = new Zalo({ selfListen: false, checkUpdate: false, logging: false });

let lastDeferLogAt = 0;

const stats = new GatewayStats();
// Tin riêng: tuần tự theo từng người gửi, song song giữa người gửi khác nhau
// (tối đa directConcurrency lượt, khớp số chỗ thành viên bên Python =
// MAX_CONCURRENT_TURNS - OWNER_RESERVED_SLOTS, xem services/concurrency.py).
const directQueue = new KeyedSerialQueue({
  concurrency: config.directConcurrency,
  perKeyCapacity: config.directPerSenderBacklog,
  capacity: 64,
});
// Gửi tin tuần tự theo người nhận: đoạn của 1 câu trả lời (nghỉ 0.6-1.5s giữa
// đoạn) không xen với câu trả lời làn nhanh/câu báo/outbox cho cùng người. Chỉ
// bọc việc gửi, không bao giờ giữ khoá qua lúc gọi Python hay tải media.
const sendLock = new KeyedMutex(SEND_LOCK_MAX_WAIT_MS);
// Ngân sách tin outbox của tài khoản theo giờ: vượt thì hoãn sang lượt poll sau.
const outboxBudget = new SlidingWindowBudget(config.outboxMaxPerHour, 3600000);
// Tin chữ nhóm /tongket + tín hiệu cổ phiếu: hàng đợi RIÊNG, không xếp sau việc tải ảnh
// của nhóm nguồn Facebook (trước đây dùng chung 1 hàng nối tiếp: nhóm Facebook đăng dồn
// ảnh làm đầy hàng và tin chữ nhóm chứng khoán bị từ chối, mất hẳn).
const summaryQueue = new BoundedSerialQueue(256);
// Nhóm nguồn Facebook: tải ảnh + gộp bài.
const groupQueue = new BoundedSerialQueue(128);

/** Báo trạng thái gateway về Python, không chờ; lỗi chỉ log. */
function reportState(gatewayState: GatewayState, detail?: string): void {
  reportGatewayState(config, { account_id: config.accountId, state: gatewayState, detail }).catch(
    (error) => console.error(`[zalo] không báo được trạng thái "${gatewayState}" về Python`, error),
  );
}

// Trưởng/phó nhóm của các nhóm đang theo dõi (lọc tín hiệu "chỉ admin nhóm" bên Python).
const groupRoles = new GroupRoles(() => session.api);
const groups = new TrackedGroups({
  fetchSummary: () => fetchAllowedGroups(config),
  fetchFacebook: () => fetchFacebookGroups(config),
  connected: () => !!session.api,
  onSummary: (groupIds) => groupRoles.retain(groupIds),
});

const facebookBuffer = new FacebookPostBuffer({
  store: (post) => storeFacebookGroupPost(config, post),
  enqueue: (work) => groupQueue.add(work),
  windowMs: config.facebookMergeWindowMs,
});

/** Tin của nhóm nguồn Facebook: tải ảnh rồi gộp tin liên tiếp cùng người thành 1 bài. */
function handleFacebookGroupMessage(msg: ParsedMessage): void {
  const postText = msg.text || msg.caption;
  groupQueue
    .add(async () => {
      const media: MediaItem[] = [];
      // Nhóm nguồn Facebook giữ nguyên danh sách URL như trước (kể cả thumb video).
      if (msg.urls.length) {
        try {
          // Ảnh nhóm: thử lại 1 lần, vì tải hỏng = bài bị bộ lọc bỏ ("không có ảnh").
          media.push(await dispatcher.fetchImage(msg.urls, 2));
        } catch (error) {
          console.error("[zalo] Facebook group image download failed", error);
        }
      }
      if (postText || media.length) {
        await facebookBuffer.add(msg.threadId, msg.sender, msg.senderName, msg.id, postText, media);
      }
    })
    .catch(console.error);
}

const dispatcher = new Dispatcher({
  api: () => session.api,
  bridge: {
    message: (message, timeoutMs) => callBridge(config, message, timeoutMs),
    image: (sender, id, caption, mime, bytes) =>
      callImageBridge(config, sender, id, caption, mime, bytes),
    voice: (sender, id, mime, bytes, durationMs) =>
      callVoiceBridge(config, sender, id, mime, bytes, durationMs),
    file: (sender, id, name, caption, mime, bytes) =>
      callFileBridge(config, sender, id, name, caption, mime, bytes),
    notice: (sender, kind) => fetchNotice(config, sender, kind),
    storeGroupMessage: (message) => storeGroupMessage(config, message),
    saveController: (id) => saveController(config, id),
    reportState,
  },
  queues: { direct: directQueue, summary: summaryQueue, sendLock },
  config,
  groups: {
    isSummary: (groupId) => groups.isSummary(groupId),
    isFacebook: (groupId) => groups.isFacebook(groupId),
    roleOf: (groupId, senderId) => groupRoles.roleOf(groupId, senderId),
    refresh: () => groups.refresh(),
  },
  facebook: handleFacebookGroupMessage,
  stats,
});

/**
 * Người đã pair (Python) để gateway nhận ra người quen ngay sau khi khởi động lại -
 * Python sập lúc đó thì họ vẫn nhận câu báo sự cố thay vì im lặng. Lỗi chỉ log.
 */
function seedKnownSenders(): void {
  fetchKnownSenders(config)
    .then((ids) => {
      const seeded = dispatcher.seedKnownSenders(ids);
      if (seeded) console.log(`[zalo] nhớ sẵn ${seeded} người đã pair`);
    })
    .catch((error) =>
      console.warn(
        `[zalo] chưa lấy được danh sách người đã pair: ${errorMessage(error, String(error))}`,
      ),
    );
}

const session = new ZaloSession({
  config,
  zalo,
  bridge: {
    loadSession: () => loadSavedSession(config),
    saveSession: (saved) => saveSession(config, saved),
    clearSession: () => clearSession(config),
    loadController: () => loadController(config),
    clearController: () => clearController(config),
    reportState,
  },
  onAttach: async () => {
    await groups.refresh();
    seedKnownSenders();
  },
  onMessage: (message) => dispatcher.onMessage(message),
  // Đổi trưởng/phó nhóm: lấy lại danh sách admin ở tin kế tiếp của nhóm đó.
  onGroupEvent: (event) => {
    const gid = String(event?.threadId ?? "");
    if (gid && groups.isSummary(gid) && ROLE_EVENTS.has(String(event?.type ?? ""))) {
      groupRoles.invalidate(gid);
    }
  },
  onLogout: () => dispatcher.clearPairing(),
});

// ─── Outbox ─────────────────────────────────────────────────────────────────

const outbox = new OutboxDelivery({
  fetch: () => fetchOutbox(config),
  sendChunk: (recipientId, chunk) =>
    sendLock.run(recipientId, async () => {
      const client = session.api;
      if (!client) throw new Error("Zalo is not connected");
      await client.sendMessage({ msg: chunk }, recipientId, ThreadType.User);
      stats.inc("chunks_sent");
    }),
  ack: (id) => ackOutbox(config, id),
  fail: (id, error, chunksSent) => reportOutboxFailure(config, id, error, chunksSent),
  connected: () => !!session.api,
  pace: async () => {
    const gap = jitterMs(config.sendGapMinMs, config.sendGapMaxMs);
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
    // Poll định kỳ + kick từ Python: log tối đa 1 dòng/10 phút khi đang hết ngân sách.
    if (Date.now() - lastDeferLogAt > 600000) {
      lastDeferLogAt = Date.now();
      console.warn(
        `[zalo] outbox: hết ngân sách ${config.outboxMaxPerHour} tin/giờ, ` +
          `hoãn ${result.deferred} tin sang lượt sau (không bỏ tin nào)`,
      );
    }
  }
}

// Poll dự phòng mỗi ZALO_OUTBOX_POLL_MS; Python kick (POST /outbox/kick) khi thêm tin.
const outboxPoller = new OutboxPoller({ run: deliverOutbox, intervalMs: config.outboxPollMs });

// GET /groups (Python /nhomzalo): 1 lượt quét tại 1 thời điểm, mỗi người chờ tối đa 25s.
const scanGroups = sharedScan(() => {
  const client = session.api;
  if (!client) return Promise.reject(new Error("Zalo is not connected"));
  return collectGroups(client);
});

// ─── Khởi động ──────────────────────────────────────────────────────────────

const controlServer = startControlServer(
  config.controlPort,
  {
    status: () => ({
      state: session.state,
      connected: !!session.api,
      accountId: session.api ? String(session.api.getOwnId()) : null,
      controllerPaired: !!config.controllerId,
      qr: session.qr,
      // Bộ đếm hoạt động + hàng đợi hiện tại (Python hiện trên /admin, không có dữ liệu riêng tư).
      stats: stats.snapshot(),
      queue: {
        pending: directQueue.pending,
        running: directQueue.running,
        senders: directQueue.keys,
        batching: dispatcher.batching,
        fastLane: dispatcher.fastLaneActive,
        groupStore: summaryQueue.pending,
        groupRoles: groupRoles.size,
        outboxBudgetLeft: Number.isFinite(outboxBudget.remaining())
          ? outboxBudget.remaining()
          : null,
      },
    }),
    startQr: () => session.startQr(),
    // Python gỡ pairing (/zaloxoa): quên ngay, để từ tin kế tiếp người này không còn
    // nhận "đã xem", typing, reaction hay câu báo bận/sự cố của gateway.
    forgetSender: (id) => {
      const forgotten = dispatcher.forgetSender(id);
      console.log(
        `[zalo] quên người gửi ${id} (Python gỡ pairing)${forgotten ? "" : " - chưa nhớ"}`,
      );
      return forgotten;
    },
    // Python /nhomzalo (Telegram, Zoom, Zalo): mọi nhóm tài khoản bot đang tham gia.
    groups: async () => (session.api ? scanGroups() : null),
    // Python vừa đổi danh sách nhóm theo dõi (/themnhom, /xoanhom... gõ từ Telegram/Zoom):
    // nạp lại ngay thay vì chờ tới ZALO_GROUP_REFRESH_MS.
    refreshGroups: async () => {
      if (!session.api) return false;
      await groups.refresh();
      return true;
    },
    // Python vừa thêm tin outbox (nhắc việc, tổng kết...): gửi ngay thay vì chờ lượt poll.
    kickOutbox: () => outboxPoller.kick(),
    startPairing: () => dispatcher.startPairing(),
    logout: () => session.logout(),
  },
  config.bridgeSecret,
);

const groupRefreshTimer = setInterval(() => void groups.refresh(), config.groupRefreshMs);
groupRefreshTimer.unref();
outboxPoller.start();

installShutdownHandlers(async () => {
  const result = await gracefulShutdown({
    stopIntake: () => {
      session.stopListening();
      dispatcher.close();
      clearInterval(groupRefreshTimer);
    },
    stopOutbox: () => {
      outbox.stop();
      return outboxPoller.stop();
    },
    idle: () => dispatcher.idle,
    flushOutboxProgress: () => outbox.flushProgress(),
    closeServer: () =>
      new Promise<void>((resolve) => {
        controlServer.close(() => resolve());
        controlServer.closeAllConnections();
      }),
  });
  console.log(
    `[zalo] đã tắt sau ${result.waitedMs}ms` +
      (result.drained ? "" : " (còn việc dở bị bỏ)") +
      (result.reported ? `, báo tiến độ ${result.reported} tin outbox` : ""),
  );
});

void session.bootstrap();
