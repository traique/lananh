import test from "node:test";
import assert from "node:assert/strict";
import {
  BOOTSTRAP_MAX_ATTEMPTS,
  ZaloSession,
  bootstrapDelayMs,
  retryableBootstrapError,
} from "./dist/session.js";

const tick = async () => {
  for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r));
};

const refused = () =>
  Object.assign(new TypeError("fetch failed"), { cause: { code: "ECONNREFUSED" } });
const rejected = () =>
  Object.assign(new Error("Đăng nhập thất bại"), { name: "ZcaApiError", code: -1 });

function fakeListener() {
  const handlers = {};
  return {
    handlers,
    started: 0,
    stopped: 0,
    on(event, fn) {
      (handlers[event] ??= []).push(fn);
    },
    emit(event, ...args) {
      for (const fn of handlers[event] ?? []) fn(...args);
    },
    start() {
      this.started++;
    },
    stop() {
      this.stopped++;
    },
  };
}

function fakeApi(id = "bot-1") {
  return {
    listener: fakeListener(),
    getOwnId: () => id,
    getContext: () => ({
      cookie: { serializeSync: () => [{ key: "zpw_sek", value: "x" }] },
      imei: "imei-1",
      userAgent: "ua-1",
    }),
  };
}

/** Phiên với Python/zca-js/timer giả; `python` đổi được giữa chừng (Python lên/xuống). */
function harness({ attempts = 2 } = {}) {
  const reports = [];
  const messages = [];
  const groupEvents = [];
  const bootTimers = [];
  const reconnectTimers = [];
  const saved = [];
  const python = {
    session: async () => ({ cookie: [{ key: "zpw_sek" }], imei: "imei-1", userAgent: "ua-1" }),
    login: async () => fakeApi(),
  };
  let attached = 0;
  let loggedOut = 0;
  const config = {
    cookie: undefined,
    imei: undefined,
    userAgent: "ua",
    controllerId: "",
    accountId: "zalo-bot",
  };
  const session = new ZaloSession({
    config,
    zalo: {
      login: (credentials) => python.login(credentials),
      loginQR: (options, callback) => python.loginQR(options, callback),
    },
    bridge: {
      loadSession: () => python.session(),
      saveSession: async (value) => void saved.push(value),
      clearSession: async () => void saved.push("cleared"),
      loadController: async () => "owner-zalo",
      clearController: async () => void saved.push("controller-cleared"),
      reportState: (state, detail) => reports.push([state, detail]),
    },
    onAttach: async () => void attached++,
    onMessage: (message) => messages.push(message),
    onGroupEvent: (event) => groupEvents.push(event),
    onLogout: () => void loggedOut++,
    bootstrapAttempts: attempts,
    setTimer: (fn, ms) => bootTimers.push({ fn, ms }),
    reconnect: {
      setTimer: (fn, ms) => {
        const handle = { fn, ms, cancelled: false };
        reconnectTimers.push(handle);
        return handle;
      },
      clearTimer: (handle) => {
        handle.cancelled = true;
      },
    },
    logger: { log() {}, warn() {}, error() {} },
  });
  return {
    session,
    python,
    config,
    reports,
    messages,
    groupEvents,
    bootTimers,
    reconnectTimers,
    saved,
    attached: () => attached,
    loggedOut: () => loggedOut,
    /** Chạy timer khởi động kế tiếp. */
    async fireBoot() {
      const next = bootTimers.shift();
      assert.ok(next, "không có lần thử khởi động nào đang hẹn");
      next.fn();
      await tick();
      return next.ms;
    },
    async fireReconnect() {
      const next = reconnectTimers.find((t) => !t.cancelled && !t.fired);
      assert.ok(next, "không có lần đăng nhập lại nào đang hẹn");
      next.fired = true;
      next.fn();
      await tick();
      return next.ms;
    },
  };
}

test("bootstrapDelayMs 1s, 2s... tối đa 10s; lỗi tạm gồm ECONNREFUSED, timeout, 5xx", () => {
  assert.equal(BOOTSTRAP_MAX_ATTEMPTS, 30);
  assert.deepEqual([0, 1, 9, 29].map(bootstrapDelayMs), [1000, 2000, 10000, 10000]);
  assert.equal(retryableBootstrapError(refused()), true);
  assert.equal(retryableBootstrapError({ name: "TimeoutError" }), true);
  assert.equal(retryableBootstrapError({ status: 503 }), true);
  assert.equal(retryableBootstrapError({ status: 403 }), false);
  assert.equal(retryableBootstrapError(rejected()), false);
});

test("khởi động: khôi phục phiên đã lưu, nạp controller, gắn listener", async () => {
  const h = harness();
  await h.session.bootstrap();
  assert.equal(h.session.state, "connected");
  assert.equal(h.config.controllerId, "owner-zalo");
  assert.equal(h.config.accountId, "bot-1");
  assert.equal(h.attached(), 1);
  assert.equal(h.session.api.listener.started, 1);
  assert.deepEqual(h.reports, []);
});

test("khởi động: không có phiên nào -> chờ đăng nhập, không báo chủ bot", async () => {
  const h = harness();
  h.python.session = async () => ({});
  await h.session.bootstrap();
  assert.equal(h.session.state, "awaiting_login");
  assert.deepEqual(h.reports, []);
  assert.deepEqual(h.bootTimers, []);
});

test("khởi động: Zalo từ chối phiên đã lưu -> báo login_required (trước đây chỉ ghi log)", async () => {
  const h = harness();
  h.python.login = async () => {
    throw rejected();
  };
  await h.session.bootstrap();
  assert.equal(h.session.state, "login_required");
  assert.equal(h.reports.length, 1);
  assert.equal(h.reports[0][0], "login_required");
  assert.match(h.reports[0][1], /bị từ chối.*Đăng nhập thất bại/);
  assert.deepEqual(h.bootTimers, [], "phiên hỏng: không tự thử lại");
  assert.equal(h.reconnectTimers.length, 0);
});

test("khởi động: Python chưa lên quá lâu -> báo chủ bot và giao cho lịch đăng nhập lại", async () => {
  const h = harness({ attempts: 2 });
  h.python.session = async () => {
    throw refused();
  };
  await h.session.bootstrap();
  assert.equal(h.session.state, "waiting_backend");
  assert.equal(await h.fireBoot(), 1000);
  assert.equal(await h.fireBoot(), 2000);
  // Hết lượt thử lúc khởi động: không bỏ cuộc như trước.
  assert.deepEqual(h.bootTimers, []);
  assert.equal(h.session.state, "reconnecting");
  assert.equal(h.reports.length, 1);
  assert.equal(h.reports[0][0], "closed", "đang tự kết nối lại, chưa cần quét QR");
  assert.match(h.reports[0][1], /sau 3 lần/);
  assert.equal(h.session.reconnecting, true);
  // Python lên lại: lịch đăng nhập lại khôi phục được phiên và báo đã kết nối lại.
  assert.equal(await h.fireReconnect(), 5000);
  assert.equal(h.session.reconnecting, true, "lần đầu vẫn lỗi");
  h.python.session = async () => ({ cookie: [{ key: "zpw_sek" }], imei: "imei-1" });
  assert.equal(await h.fireReconnect(), 15000);
  assert.equal(h.session.state, "connected");
  assert.ok(h.session.api);
  assert.equal(h.session.reconnecting, false);
  assert.deepEqual(h.reports[1], ["connected", "kết nối lại sau 2 lần thử"]);
});

test("lịch đăng nhập lại hết lượt thì báo login_required", async () => {
  const h = harness({ attempts: 0 });
  h.python.session = async () => {
    throw refused();
  };
  await h.session.bootstrap();
  assert.equal(h.reports[0][0], "closed");
  for (let i = 0; i < 10; i++) await h.fireReconnect();
  assert.equal(h.session.state, "login_required");
  assert.deepEqual(h.reports.at(-1), ["login_required", "không kết nối lại được sau 10 lần thử"]);
});

test("listener đóng hẳn -> báo closed và tự đăng nhập lại từ phiên đã lưu", async () => {
  const h = harness();
  await h.session.bootstrap();
  const api = h.session.api;
  api.listener.emit("closed", 3000, "duplicate");
  assert.equal(h.session.api, null);
  assert.equal(h.session.state, "reconnecting");
  assert.deepEqual(h.reports, [["closed", "listener closed code=3000 duplicate"]]);
  assert.equal(await h.fireReconnect(), 5000);
  assert.equal(h.session.state, "connected");
  assert.notEqual(h.session.api, api, "phiên mới");
});

test("lúc tắt: dừng listener, listener đóng không bị coi là mất kết nối, tin tới muộn bị bỏ", async () => {
  const h = harness();
  await h.session.bootstrap();
  const api = h.session.api;
  api.listener.emit("message", { id: 1 });
  api.listener.emit("group_event", { threadId: "g", type: "add_admin" });
  h.session.stopListening();
  assert.equal(api.listener.stopped, 1);
  assert.equal(h.session.state, "stopping");
  api.listener.emit("closed", 1000, "");
  api.listener.emit("message", { id: 2 });
  assert.equal(h.session.api, api, "phiên vẫn gắn để gửi nốt trả lời");
  assert.deepEqual(h.reports, []);
  assert.deepEqual(h.reconnectTimers, []);
  assert.deepEqual(h.messages, [{ id: 1 }]);
  assert.equal(h.groupEvents.length, 1);
  // zca-js tự mở lại socket lúc đang tắt: đóng ngay.
  api.listener.emit("connected");
  assert.equal(api.listener.stopped, 2);
  await h.session.bootstrap();
  assert.equal(await h.session.startQr(), "stopping", "không đăng nhập lại lúc đang tắt");
});

test("listener cũ (sau đăng nhập lại) báo muộn không động tới phiên mới", async () => {
  const h = harness();
  await h.session.bootstrap();
  const old = h.session.api;
  old.listener.emit("closed", 1006, "");
  await h.fireReconnect();
  const current = h.session.api;
  old.listener.emit("message", { id: "muộn" });
  old.listener.emit("closed", 1006, "");
  assert.equal(h.session.api, current);
  assert.deepEqual(h.messages, []);
  old.listener.emit("connected");
  assert.equal(old.listener.stopped, 1, "listener cũ tự nối lại: đóng ngay");
});

test("startQr: hiện QR, lưu phiên rồi gắn listener; logout xoá phiên và controller", async () => {
  const h = harness();
  let finish;
  h.python.loginQR = (_options, callback) =>
    new Promise((resolve) => {
      setImmediate(() => callback({ type: 0, data: { image: "data:image/png;base64,QUJD" } }));
      finish = () => resolve(fakeApi("bot-qr"));
    });
  assert.equal(await h.session.startQr(), "waiting_qr");
  await tick();
  assert.equal(h.session.state, "qr_ready");
  assert.equal(h.session.qr, "QUJD");
  assert.equal(await h.session.startQr(), "qr_ready", "đang chờ quét: không mở QR thứ 2");
  finish();
  await tick();
  assert.equal(h.session.state, "connected");
  assert.equal(h.session.qr, null);
  assert.equal(h.saved[0].accountId, "bot-qr");
  assert.equal(h.saved[0].imei, "imei-1");
  h.config.controllerId = "owner-zalo";
  await h.session.logout();
  assert.equal(h.session.api, null);
  assert.equal(h.session.state, "awaiting_login");
  assert.equal(h.config.controllerId, "");
  assert.equal(h.loggedOut(), 1);
  assert.deepEqual(h.saved.slice(1), ["cleared", "controller-cleared"]);
});
