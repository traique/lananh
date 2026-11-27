import { createHash, timingSafeEqual } from "node:crypto";
import { createServer, type IncomingMessage, type Server } from "node:http";
import type { GroupList } from "./groups.js";

export type ControlHandlers = {
  status(): Record<string, unknown>;
  startQr(): Promise<string>;
  startPairing(): { code: string; expiresAt: number } | null;
  logout(): Promise<void>;
  /** Quên 1 người gửi (Python gỡ pairing: /zaloxoa); true nếu gateway đang nhớ người đó. */
  forgetSender(id: string): boolean;
  /** Mọi nhóm bot đang tham gia (Python /nhomzalo); null khi Zalo chưa kết nối. */
  groups(): Promise<GroupList | null>;
  /** Nạp lại danh sách nhóm theo dõi từ Python ngay; false khi Zalo chưa kết nối. */
  refreshGroups(): Promise<boolean>;
  /** Python vừa thêm tin outbox: gửi sớm (có gộp); false khi gateway đang tắt. */
  kickOutbox(): boolean;
};

/**
 * Header xác thực mọi route: cùng ZALO_BRIDGE_SECRET mà gateway gửi kèm khi gọi Python.
 * Bind 127.0.0.1 chưa đủ: /logout xoá phiên Zalo và /status trả mã QR đăng nhập, nên
 * mọi tiến trình khác trong container (hay SSRF qua loopback) không được gọi tự do.
 */
export const CONTROL_SECRET_HEADER = "x-zalo-bridge-secret";

/** Body JSON tối đa của control server (chỉ chứa 1 id Zalo). */
const MAX_BODY_BYTES = 4096;

class BodyError extends Error {
  constructor(readonly status: number) {
    super(status === 413 ? "body too large" : "invalid body");
  }
}

/** Đọc body JSON nhỏ (trần MAX_BODY_BYTES); sai/quá lớn -> BodyError 400/413. */
async function readJson(req: IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of req) {
    const buffer = chunk as Buffer;
    size += buffer.length;
    if (size > MAX_BODY_BYTES) throw new BodyError(413);
    chunks.push(buffer);
  }
  try {
    return JSON.parse(Buffer.concat(chunks).toString("utf8")) as unknown;
  } catch {
    throw new BodyError(400);
  }
}

const sha256 = (value: string) => createHash("sha256").update(value, "utf8").digest();

/**
 * So khớp secret không lộ qua thời gian: băm cả hai trước (độ dài bằng nhau) rồi
 * timingSafeEqual. Thiếu/rỗng/lặp header hoặc gateway chưa có secret -> false (fail closed).
 */
export function secretMatches(provided: string | string[] | undefined, expected: string): boolean {
  if (!expected || typeof provided !== "string" || !provided) return false;
  return timingSafeEqual(sha256(provided), sha256(expected));
}

/**
 * HTTP server điều khiển nội bộ - CHỈ bind 127.0.0.1, Python gọi qua loopback kèm
 * header CONTROL_SECRET_HEADER (channels/zalo_gateway_status.py, handlers/zalo_login.py).
 */
export function startControlServer(
  port: number,
  handlers: ControlHandlers,
  secret: string,
): Server {
  const server = createServer(async (req, res) => {
    res.setHeader("content-type", "application/json");
    const reply = (status: number, body: unknown) => {
      res.statusCode = status;
      res.end(JSON.stringify(body));
    };
    // Xác thực trước khi xét route: không lộ cả danh sách route cho người gọi lạ.
    if (!secretMatches(req.headers[CONTROL_SECRET_HEADER], secret)) {
      res.setHeader("connection", "close");
      return reply(403, { error: "forbidden" });
    }
    try {
      if (req.method === "GET" && req.url === "/status") return reply(200, handlers.status());
      if (req.method === "GET" && req.url === "/groups") {
        const result = await handlers.groups();
        if (!result) return reply(409, { error: "Zalo is not connected" });
        return reply(200, result);
      }
      if (req.method === "POST" && req.url === "/groups/refresh") {
        if (!(await handlers.refreshGroups()))
          return reply(409, { error: "Zalo is not connected" });
        return reply(200, { ok: true });
      }
      if (req.method === "POST" && req.url === "/outbox/kick") {
        return reply(200, { ok: true, scheduled: handlers.kickOutbox() });
      }
      if (req.method === "POST" && req.url === "/login/qr") {
        return reply(200, { ok: true, state: await handlers.startQr() });
      }
      if (req.method === "POST" && req.url === "/pairing/start") {
        const pairing = handlers.startPairing();
        if (!pairing) return reply(409, { error: "Zalo is not connected" });
        return reply(200, pairing);
      }
      if (req.method === "POST" && req.url === "/logout") {
        await handlers.logout();
        return reply(200, { ok: true });
      }
      if (req.method === "POST" && req.url === "/known/forget") {
        const body = (await readJson(req)) as { id?: unknown } | null;
        const id = typeof body?.id === "string" ? body.id.trim() : "";
        if (!id) return reply(400, { error: "missing id" });
        return reply(200, { ok: true, forgotten: handlers.forgetSender(id) });
      }
      return reply(404, { error: "not found" });
    } catch (error) {
      if (error instanceof BodyError) {
        // Body quá lớn: trả lời rồi đóng kết nối, không đọc tiếp phần còn lại.
        if (error.status === 413) res.setHeader("connection", "close");
        return reply(error.status, { error: error.message });
      }
      return reply(500, { error: error instanceof Error ? error.message : "error" });
    }
  });
  server.listen(port, "127.0.0.1");
  return server;
}
