import type { GatewayConfig } from "./config.js";
export type IncomingDirectMessage = {
  senderId: string;
  senderName: string;
  conversationId: string;
  messageId: string;
  text: string;
};
export type IncomingGroupMessage = {
  groupId: string;
  messageId: string;
  senderId: string;
  senderName: string;
  text: string;
  sentAtMs: number;
};
export type IncomingFacebookPost = {
  groupId: string;
  messageIds: string[];
  senderId: string;
  senderName: string;
  text: string;
  media: { mime: string; bytes: Uint8Array }[];
};
export type OutboxItem = {
  id: number;
  recipient_id: string;
  content: string;
  /** Số đoạn đầu của tin dài đã gửi được ở lần thử trước (không gửi lại). */
  chunks_sent?: number;
};
type GroupRow = { group_id: string | number };
type BridgeResponse = { messages: string[]; provider?: string | null; image_b64?: string | null };
/**
 * Thời gian chờ Python trả lời 1 tin. Phải LỚN HƠN hạn chót mỗi lượt bên Python
 * (CHAT_TURN_TIMEOUT_SEC, mặc định 200s, tối đa 230s) để người dùng nhận được
 * thông báo lỗi rõ ràng từ Python thay vì gateway bỏ cuộc trước.
 */
export const BRIDGE_TIMEOUT_MS = 240000;
/** Lệnh nhẹ (session, nhóm, outbox): không để 1 request treo chặn cả hàng đợi/vòng poll. */
const SHORT_TIMEOUT_MS = 30000;
/** Lỗi HTTP từ Python, giữ mã trạng thái để nơi gọi phân loại (4xx bỏ / 5xx thử lại). */
export class BridgeHttpError extends Error {
  constructor(
    label: string,
    readonly status: number,
    body: string,
  ) {
    super(`${label} returned ${status}: ${body.slice(0, 200)}`);
    this.name = "BridgeHttpError";
  }
}
const h = (c: GatewayConfig) => ({
  "content-type": "application/json",
  "x-zalo-bridge-secret": c.bridgeSecret,
});
async function ok(r: Response, l: string) {
  if (!r.ok) throw new BridgeHttpError(l, r.status, await r.text().catch(() => ""));
  return r;
}
export async function loadSavedSession(c: GatewayConfig) {
  return (await (
    await ok(
      await fetch(`${c.bridgeBaseUrl}/session`, {
        headers: h(c),
        signal: AbortSignal.timeout(SHORT_TIMEOUT_MS),
      }),
      "Session load",
    )
  ).json()) as unknown;
}
export async function saveSession(c: GatewayConfig, s: unknown) {
  await ok(
    await fetch(`${c.bridgeBaseUrl}/session`, {
      method: "PUT",
      headers: h(c),
      body: JSON.stringify(s),
      signal: AbortSignal.timeout(SHORT_TIMEOUT_MS),
    }),
    "Session save",
  );
}
export async function clearSession(c: GatewayConfig) {
  await ok(
    await fetch(`${c.bridgeBaseUrl}/session`, {
      method: "DELETE",
      headers: h(c),
      signal: AbortSignal.timeout(SHORT_TIMEOUT_MS),
    }),
    "Session clear",
  );
}
export async function loadController(c: GatewayConfig) {
  const r = (await (
    await ok(
      await fetch(`${c.bridgeBaseUrl}/controller`, {
        headers: h(c),
        signal: AbortSignal.timeout(SHORT_TIMEOUT_MS),
      }),
      "Controller load",
    )
  ).json()) as { controllerId?: unknown };
  return String(r.controllerId || "");
}
export async function saveController(c: GatewayConfig, id: string) {
  await ok(
    await fetch(`${c.bridgeBaseUrl}/controller`, {
      method: "PUT",
      headers: h(c),
      body: JSON.stringify({ controllerId: id }),
      signal: AbortSignal.timeout(SHORT_TIMEOUT_MS),
    }),
    "Controller save",
  );
}
export async function clearController(c: GatewayConfig) {
  await ok(
    await fetch(`${c.bridgeBaseUrl}/controller`, {
      method: "DELETE",
      headers: h(c),
      signal: AbortSignal.timeout(SHORT_TIMEOUT_MS),
    }),
    "Controller clear",
  );
}
export async function callBridge(c: GatewayConfig, m: IncomingDirectMessage) {
  return (await (
    await ok(
      await fetch(`${c.bridgeBaseUrl}/message`, {
        method: "POST",
        headers: h(c),
        body: JSON.stringify({
          account_id: c.accountId,
          sender_id: m.senderId,
          sender_name: m.senderName,
          conversation_id: m.conversationId,
          message_id: m.messageId,
          text: m.text,
        }),
        signal: AbortSignal.timeout(BRIDGE_TIMEOUT_MS),
      }),
      "Bridge",
    )
  ).json()) as BridgeResponse;
}
export async function callImageBridge(
  c: GatewayConfig,
  senderId: string,
  messageId: string,
  caption: string,
  mime: string,
  image: Uint8Array,
) {
  const body = image.buffer.slice(
    image.byteOffset,
    image.byteOffset + image.byteLength,
  ) as ArrayBuffer;
  const r = await fetch(`${c.bridgeBaseUrl}/image-prompt`, {
    method: "POST",
    headers: {
      "content-type": mime,
      "x-zalo-bridge-secret": c.bridgeSecret,
      "x-zalo-account-id": c.accountId,
      "x-zalo-sender-id": senderId,
      "x-zalo-message-id": messageId,
      "x-zalo-caption": encodeURIComponent(caption.slice(0, 500)),
    },
    body,
    signal: AbortSignal.timeout(BRIDGE_TIMEOUT_MS),
  });
  return (await (await ok(r, "Image bridge")).json()) as BridgeResponse;
}
export async function fetchAllowedGroups(c: GatewayConfig) {
  const g = (await (
    await ok(
      await fetch(`${c.bridgeBaseUrl}/groups/${encodeURIComponent(c.accountId)}`, {
        headers: h(c),
        signal: AbortSignal.timeout(SHORT_TIMEOUT_MS),
      }),
      "Groups",
    )
  ).json()) as GroupRow[];
  return new Set(g.map((x) => String(x.group_id)));
}
export async function fetchFacebookGroups(c: GatewayConfig) {
  const g = (await (
    await ok(
      await fetch(`${c.bridgeBaseUrl}/facebook-groups/${encodeURIComponent(c.accountId)}`, {
        headers: h(c),
        signal: AbortSignal.timeout(SHORT_TIMEOUT_MS),
      }),
      "Facebook groups",
    )
  ).json()) as GroupRow[];
  return new Set(g.map((x) => String(x.group_id)));
}
export async function storeGroupMessage(c: GatewayConfig, m: IncomingGroupMessage) {
  await ok(
    await fetch(`${c.bridgeBaseUrl}/group-message`, {
      method: "POST",
      headers: h(c),
      body: JSON.stringify({
        account_id: c.accountId,
        group_id: m.groupId,
        message_id: m.messageId,
        sender_id: m.senderId,
        sender_name: m.senderName,
        text: m.text,
        sent_at_ms: m.sentAtMs,
      }),
      signal: AbortSignal.timeout(SHORT_TIMEOUT_MS),
    }),
    "Group message",
  );
}
export async function storeFacebookGroupPost(c: GatewayConfig, m: IncomingFacebookPost) {
  await ok(
    await fetch(`${c.bridgeBaseUrl}/facebook-group-post`, {
      method: "POST",
      headers: h(c),
      body: JSON.stringify({
        account_id: c.accountId,
        group_id: m.groupId,
        message_ids: m.messageIds,
        sender_id: m.senderId,
        sender_name: m.senderName,
        text: m.text,
        media: m.media.map((x) => ({
          mime_type: x.mime,
          data_b64: Buffer.from(x.bytes).toString("base64"),
        })),
      }),
      signal: AbortSignal.timeout(BRIDGE_TIMEOUT_MS),
    }),
    "Facebook group post",
  );
}
export async function fetchOutbox(c: GatewayConfig) {
  return (await (
    await ok(
      await fetch(`${c.bridgeBaseUrl}/outbox/${encodeURIComponent(c.accountId)}`, {
        headers: h(c),
        signal: AbortSignal.timeout(SHORT_TIMEOUT_MS),
      }),
      "Outbox",
    )
  ).json()) as OutboxItem[];
}
export async function ackOutbox(c: GatewayConfig, id: number) {
  await ok(
    await fetch(`${c.bridgeBaseUrl}/outbox/${id}/ack`, {
      method: "POST",
      headers: h(c),
      signal: AbortSignal.timeout(SHORT_TIMEOUT_MS),
    }),
    "Ack",
  );
}
/**
 * Trạng thái gateway báo về Python (để báo chủ bot qua Telegram):
 * - connected: đã đăng nhập lại sau khi mất kết nối;
 * - reconnecting: đang thử đăng nhập lại từ phiên đã lưu;
 * - closed: listener Zalo đóng, gateway sắp thử đăng nhập lại;
 * - login_required: hết lượt thử / phiên hỏng, cần quét QR lại (/zalo);
 * - send_failed: gửi trả lời cho 1 tin riêng thất bại sau khi đã thử lại.
 */
export type GatewayState =
  | "connected"
  | "reconnecting"
  | "closed"
  | "login_required"
  | "send_failed";
export type GatewayStateReport = { account_id: string; state: GatewayState; detail?: string };
const MAX_STATE_DETAIL_CHARS = 300;
/** POST /internal/zalo/gateway-state: {account_id, state, detail?}. Nơi gọi tự log lỗi, không chờ. */
export async function reportGatewayState(c: GatewayConfig, report: GatewayStateReport) {
  await ok(
    await fetch(`${c.bridgeBaseUrl}/gateway-state`, {
      method: "POST",
      headers: h(c),
      body: JSON.stringify({
        account_id: report.account_id,
        state: report.state,
        detail: report.detail?.slice(0, MAX_STATE_DETAIL_CHARS),
      }),
      signal: AbortSignal.timeout(SHORT_TIMEOUT_MS),
    }),
    "Gateway state report",
  );
}
/** Báo Python 1 tin outbox gửi lỗi: Python tăng số lần thử, hẹn giờ thử lại hoặc bỏ cuộc. */
export async function reportOutboxFailure(
  c: GatewayConfig,
  id: number,
  error: string,
  chunksSent: number,
) {
  await ok(
    await fetch(`${c.bridgeBaseUrl}/outbox/${id}/failed`, {
      method: "POST",
      headers: h(c),
      body: JSON.stringify({ error: error.slice(0, 300), chunks_sent: chunksSent }),
      signal: AbortSignal.timeout(SHORT_TIMEOUT_MS),
    }),
    "Outbox failure report",
  );
}
