/**
 * Phiên Zalo của tài khoản bot: khôi phục phiên đã lưu lúc khởi động, đăng nhập QR,
 * gắn listener, tự đăng nhập lại khi listener đóng hẳn, đăng xuất, và dừng nhận tin
 * lúc tắt. zca-js, Python và timer được tiêm vào để test bằng fake.
 */
import { LoginQRCallbackEventType, type Credentials } from "zca-js";
import type { GatewayState } from "./bridge.js";
import type { GatewayConfig } from "./config.js";
import { ReconnectScheduler, type ReconnectDeps } from "./reconnect.js";
import { errorMessage, isConnectionRefused, isTransientNetworkError } from "./text.js";
import type { ZaloApi, ZaloGroupEvent, ZaloMessage } from "./types.js";

/** Lệnh gọi Python của phiên (index.ts nối tới bridge.ts kèm config). */
export type SessionBridge = {
  loadSession(): Promise<unknown>;
  saveSession(session: unknown): Promise<void>;
  clearSession(): Promise<void>;
  loadController(): Promise<string>;
  clearController(): Promise<void>;
  /** Báo trạng thái gateway về Python (chủ bot được báo), không chờ. */
  reportState(state: GatewayState, detail?: string): void;
};

export type QrEvent = { type: number; data?: { image?: string; qrData?: string } };

/** Phần lớp Zalo của zca-js mà phiên dùng. */
export type ZaloLogin = {
  login(credentials: Credentials): Promise<unknown>;
  loginQR(options: { userAgent?: string }, callback: (event: QrEvent) => unknown): Promise<unknown>;
};

export type SessionLogger = Pick<Console, "log" | "warn" | "error">;

export type SessionDeps = {
  /** Cấu hình dùng chung: accountId/controllerId được GHI khi gắn phiên / đăng xuất. */
  config: Pick<GatewayConfig, "cookie" | "imei" | "userAgent" | "controllerId" | "accountId">;
  zalo: ZaloLogin;
  bridge: SessionBridge;
  /** Sau khi gắn phiên mới, trước khi listener chạy (nạp nhóm theo dõi, người quen...). */
  onAttach?(api: ZaloApi): Promise<void>;
  onMessage(message: ZaloMessage): void;
  onGroupEvent?(event: ZaloGroupEvent): void;
  /** Đăng xuất (/zalologout): bỏ mã ghép đôi đang chờ... */
  onLogout?(): void;
  /** Số lần thử khôi phục phiên lúc khởi động (lỗi tạm) trước khi giao cho lịch đăng nhập lại. */
  bootstrapAttempts?: number;
  /** Cho test: thay setTimeout của các lần thử lúc khởi động (mặc định unref). */
  setTimer?(fn: () => void, ms: number): unknown;
  /** Cho test: bảng chờ / timer của lịch đăng nhập lại. */
  reconnect?: Pick<ReconnectDeps, "delaysMs" | "maxAttempts" | "setTimer" | "clearTimer">;
  logger?: SessionLogger;
};

/** Lần thử khôi phục phiên lúc khởi động khi Python/Zalo chưa sẵn sàng (~4,5 phút). */
export const BOOTSTRAP_MAX_ATTEMPTS = 30;
/** Số lần liên tiếp Zalo từ chối phiên đã lưu (không phải lỗi mạng) trước khi coi là phiên hỏng. */
export const RECONNECT_PERMANENT_FAILURES = 2;

type SavedSession = { cookie?: unknown; imei?: string; userAgent?: string };

/** Chờ trước lần thử khởi động kế: 1s, 2s, ... tối đa 10s. */
export function bootstrapDelayMs(attempt: number): number {
  return Math.min(10000, 1000 * (attempt + 1));
}

/** Lỗi tạm khi khôi phục phiên: Python chưa lên/đang khởi động, mạng tới Zalo chập chờn. */
export function retryableBootstrapError(error: unknown): boolean {
  if (isConnectionRefused(error) || isTransientNetworkError(error)) return true;
  const e = error as { status?: unknown; name?: unknown } | null;
  return e?.name === "TimeoutError" || (typeof e?.status === "number" && e.status >= 500);
}

export class ZaloSession {
  /** Trạng thái hiện trên /status (handlers/zalo_login.py đọc để dẫn chủ bot). */
  state = "idle";
  qr: string | null = null;
  private current: ZaloApi | null = null;
  private loginPromise: Promise<void> | null = null;
  private permanentFailures = 0;
  private stopped = false;
  private readonly log: SessionLogger;
  private readonly reconnect: ReconnectScheduler;

  constructor(private readonly deps: SessionDeps) {
    this.log = deps.logger ?? console;
    /**
     * Listener đóng hẳn (zca-js không tự thử lại) hoặc khởi động mãi chưa khôi phục được
     * phiên: đăng nhập lại từ phiên đã lưu với backoff 5s/15s/60s/5 phút, tối đa
     * MAX_RECONNECT_ATTEMPTS lần. Lỗi tạm (Python đang khởi động lại, mạng) thử tiếp; Zalo
     * từ chối cookie 2 lần liên tiếp hoặc không còn phiên -> "login_required" để chủ bot
     * quét QR lại.
     */
    this.reconnect = new ReconnectScheduler({
      run: (attempt) => this.reconnectOnce(attempt),
      onSchedule: (attempt, delayMs) => {
        if (this.current) return;
        this.state = "reconnecting";
        this.log.warn(`[zalo] thử đăng nhập lại lần ${attempt + 1} sau ${delayMs}ms`);
      },
      onConnected: (attempts) => {
        this.permanentFailures = 0;
        this.log.log(`[zalo] đã kết nối lại sau ${attempts} lần thử`);
        deps.bridge.reportState("connected", `kết nối lại sau ${attempts} lần thử`);
      },
      onGiveUp: (attempts) => {
        this.permanentFailures = 0;
        if (this.current) return;
        this.state = "login_required";
        this.log.error(`[zalo] bỏ cuộc sau ${attempts} lần kết nối lại; cần đăng nhập lại (/zalo)`);
        deps.bridge.reportState("login_required", `không kết nối lại được sau ${attempts} lần thử`);
      },
      ...deps.reconnect,
    });
  }

  /** Phiên zca-js đang gắn; null = chưa đăng nhập hoặc mất kết nối. */
  get api(): ZaloApi | null {
    return this.current;
  }

  /** Đang chạy lịch đăng nhập lại (sau khi listener đóng / khởi động chưa được). */
  get reconnecting(): boolean {
    return this.reconnect.running;
  }

  /**
   * Khởi động: khôi phục phiên đã lưu (env hoặc Python). Python (uvicorn) có thể khởi
   * động chậm hơn Node trong cùng container (migration, DB Supabase đang ngủ...): lỗi
   * tạm thử lại ~4,5 phút; hết lượt thì KHÔNG bỏ cuộc mà báo chủ bot và giao cho lịch
   * đăng nhập lại (5s/15s/60s rồi mỗi 5 phút, tối đa MAX_RECONNECT_ATTEMPTS lần). Zalo từ
   * chối phiên đã lưu -> báo "login_required" để chủ bot quét QR lại (/zalo) - trước đây
   * cả hai trường hợp chỉ ghi log và đứng yên tới lần khởi động lại sau.
   */
  async bootstrap(attempt = 0): Promise<void> {
    // Đã đăng nhập bằng QR trong lúc chờ thử lại: không đăng nhập chồng phiên.
    if (this.stopped || this.current || this.loginPromise) return;
    const maxAttempts = this.deps.bootstrapAttempts ?? BOOTSTRAP_MAX_ATTEMPTS;
    try {
      if (!(await this.restoreSession())) this.state = "awaiting_login";
    } catch (error) {
      if (!retryableBootstrapError(error)) {
        this.state = "login_required";
        this.log.error("[zalo] restore failed", error);
        this.deps.bridge.reportState(
          "login_required",
          `phiên đã lưu bị từ chối lúc khởi động: ${errorMessage(error, String(error))}`,
        );
        return;
      }
      if (attempt < maxAttempts) {
        this.state = "waiting_backend";
        const delay = bootstrapDelayMs(attempt);
        this.log.warn(`[zalo] restore chưa được (lần ${attempt + 1}); thử lại sau ${delay}ms`);
        this.setTimer(() => void this.bootstrap(attempt + 1), delay);
        return;
      }
      // Lỗi tạm kéo dài (Python/Zalo chưa sẵn sàng): báo "closed" (= "đang tự kết nối lại
      // từ session đã lưu") thay vì "login_required" - quét QR không giúp gì khi phiên còn
      // tốt; lịch đăng nhập lại tự báo "login_required" nếu cuối cùng vẫn không được.
      const detail = `khởi động chưa khôi phục được phiên sau ${attempt + 1} lần (${errorMessage(error, String(error))})`;
      this.log.error(`[zalo] ${detail}; chuyển sang tự đăng nhập lại theo lịch`, error);
      this.state = "reconnecting";
      this.deps.bridge.reportState("closed", detail);
      this.reconnect.start();
    }
  }

  async startQr(): Promise<string> {
    if (this.stopped || this.current || this.loginPromise) return this.state;
    // Người dùng chủ động quét QR: thôi tự kết nối lại từ phiên cũ.
    this.reconnect.cancel();
    this.state = "waiting_qr";
    this.qr = null;
    this.loginPromise = (async () => {
      try {
        const next = (await this.deps.zalo.loginQR(
          { userAgent: this.deps.config.userAgent },
          (event) => this.onQrEvent(event),
        )) as ZaloApi;
        this.state = "saving_session";
        await this.persist(next);
        await this.attach(next);
      } catch (error) {
        this.qr = null;
        this.state = "error";
        this.log.error("[zalo] QR login failed", error);
      } finally {
        this.loginPromise = null;
      }
    })();
    return this.state;
  }

  async logout(): Promise<void> {
    this.reconnect.cancel();
    try {
      this.current?.listener?.stop?.();
    } catch {
      // Listener đã đóng: bỏ qua.
    }
    this.current = null;
    this.qr = null;
    this.deps.onLogout?.();
    this.deps.config.controllerId = "";
    this.state = "awaiting_login";
    await this.deps.bridge.clearSession();
    await this.deps.bridge.clearController();
  }

  /**
   * Lúc tắt (SIGTERM): dừng listener (thôi nhận tin), huỷ mọi lần đăng nhập lại. Phiên
   * vẫn gắn để các câu trả lời đang gửi dở gửi nốt (API gửi tin của zca-js là HTTP,
   * không cần listener); listener đóng lúc này không bị coi là mất kết nối.
   */
  stopListening(): void {
    this.stopped = true;
    this.reconnect.cancel();
    this.state = "stopping";
    try {
      this.current?.listener?.stop?.();
    } catch {
      // Listener đã đóng: bỏ qua.
    }
  }

  private setTimer(fn: () => void, ms: number): void {
    if (this.deps.setTimer) {
      this.deps.setTimer(fn, ms);
      return;
    }
    setTimeout(fn, ms).unref();
  }

  private async reconnectOnce(attempt: number): Promise<"connected" | "retry" | "give_up"> {
    // Đã đăng nhập (QR) trong lúc chờ: dừng, không đăng nhập chồng phiên.
    if (this.current) return "connected";
    if (this.loginPromise) return "retry";
    try {
      if (await this.restoreSession()) return "connected";
      this.log.error("[zalo] không còn phiên đã lưu để kết nối lại; cần quét QR");
      return "give_up";
    } catch (error) {
      if (retryableBootstrapError(error)) {
        this.permanentFailures = 0;
        this.log.warn(`[zalo] kết nối lại lần ${attempt} chưa được (lỗi tạm)`, error);
        return "retry";
      }
      this.permanentFailures++;
      this.log.error(`[zalo] kết nối lại lần ${attempt} bị Zalo từ chối`, error);
      return this.permanentFailures >= RECONNECT_PERMANENT_FAILURES ? "give_up" : "retry";
    }
  }

  /**
   * Đăng nhập lại từ phiên đã lưu (env hoặc Python) và gắn listener. Dùng chung
   * cho khởi động và cho kết nối lại sau khi listener đóng. Trả false nếu không
   * có phiên nào để khôi phục; ném lỗi nếu Python/Zalo lỗi.
   */
  private async restoreSession(): Promise<boolean> {
    const { config, bridge, zalo } = this.deps;
    this.state = "waiting_backend";
    if (!config.controllerId) config.controllerId = await bridge.loadController();
    const session: SavedSession =
      config.cookie && config.imei
        ? { cookie: config.cookie, imei: config.imei, userAgent: config.userAgent }
        : ((await bridge.loadSession()) as SavedSession);
    if (!session?.cookie || !session?.imei) return false;
    this.state = "restoring";
    const credentials: Credentials = {
      cookie: session.cookie,
      imei: session.imei,
      userAgent: session.userAgent || config.userAgent,
    };
    await this.attach((await zalo.login(credentials)) as ZaloApi);
    return true;
  }

  private async attach(next: ZaloApi): Promise<void> {
    const { config, bridge } = this.deps;
    this.current = next;
    this.state = "connected";
    this.qr = null;
    config.accountId = String(next.getOwnId() || config.accountId);
    await this.deps.onAttach?.(next);
    const listener = next.listener;
    // Mọi sự kiện chỉ áp dụng cho phiên hiện tại: listener cũ (sau logout/đăng
    // nhập lại) có báo muộn cũng không được xoá phiên mới.
    listener.on("message", (message) => {
      if (this.current === next && !this.stopped) this.deps.onMessage(message);
    });
    listener.on("group_event", (event) => {
      if (this.current === next && !this.stopped) this.deps.onGroupEvent?.(event);
    });
    listener.on("connected", () => {
      if (this.current === next && !this.stopped) this.state = "connected";
      // Listener cũ tự kết nối lại sau logout (hoặc lúc đang tắt): đóng ngay.
      else listener.stop?.();
    });
    listener.on("disconnected", (code, reason) => {
      // zca-js tự kết nối lại (retryOnClose) với mã đóng cho phép; API HTTP vẫn dùng được.
      if (this.current === next && !this.stopped) this.state = "reconnecting";
      this.log.warn(`[zalo] listener disconnected code=${code ?? "?"} ${reason ?? ""}`);
    });
    listener.on("closed", (code, reason) => {
      // Chính gateway dừng listener lúc tắt: không phải mất kết nối.
      if (this.stopped) return;
      const detail = `listener closed code=${code ?? "?"} ${reason ?? ""}`.trim();
      this.log.error(`[zalo] ${detail}; sẽ thử đăng nhập lại từ phiên đã lưu`);
      if (this.current !== next) return;
      this.current = null;
      this.state = "closed";
      bridge.reportState("closed", detail);
      this.reconnect.start();
    });
    listener.on("error", (error) => this.log.error(error));
    listener.start({ retryOnClose: true });
    this.log.log(`[zalo] listener started account=${config.accountId}`);
  }

  private async persist(next: ZaloApi): Promise<void> {
    const ctx = next.getContext();
    await this.deps.bridge.saveSession({
      cookie: ctx.cookie.serializeSync(),
      imei: ctx.imei,
      userAgent: ctx.userAgent,
      accountId: String(next.getOwnId()),
    });
  }

  private onQrEvent(event: QrEvent): void {
    if (event.type === LoginQRCallbackEventType.QRCodeGenerated) {
      this.qr = String(event.data?.image || event.data?.qrData || "").replace(
        /^data:image\/png;base64,/,
        "",
      );
      this.state = "qr_ready";
    } else if (event.type === LoginQRCallbackEventType.QRCodeScanned) {
      this.qr = null;
      this.state = "scanned";
    } else if (event.type === LoginQRCallbackEventType.QRCodeExpired) {
      this.qr = null;
      this.state = "expired";
    } else if (event.type === LoginQRCallbackEventType.QRCodeDeclined) {
      this.qr = null;
      this.state = "declined";
    }
  }
}
