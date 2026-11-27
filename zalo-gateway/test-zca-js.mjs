/**
 * Smoke test zca-js (thư viện KHÔNG chính thức, gateway khai báo kiểu `any` trong
 * src/zca-js-compat.d.ts): nạp module thật, dựng 1 API offline (không đăng nhập, không
 * mạng) và khẳng định mọi method/enum gateway dùng vẫn còn và khớp hằng số của gateway.
 * Nâng zca-js mà test này đỏ = đọc checklist "Nâng cấp zca-js" trong README.md.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import * as zca from "zca-js";
import { ROLE_EVENTS } from "./dist/group-roles.js";
import { DEST_TYPE_USER, REACTION_QUEUED, ZALO_TEXT_STYLES } from "./dist/types.js";

const pkg = JSON.parse(readFileSync(new URL("./package.json", import.meta.url), "utf8"));
const installed = JSON.parse(
  readFileSync(new URL("./node_modules/zca-js/package.json", import.meta.url), "utf8"),
);

/** API offline: context giả đủ để các factory dựng hàm, không request nào được gửi. */
function offlineApi() {
  const ctx = {
    secretKey: "AAAAAAAAAAAAAAAAAAAAAA==",
    uid: "1234567890",
    imei: "imei",
    cookie: { getCookieStringSync: () => "" },
    userAgent: "ua",
    language: "vi",
    options: { selfListen: false, checkUpdate: false, logging: false },
    settings: {
      features: { socket: { retries: {}, close_and_retry_codes: [], rotate_error_codes: [] } },
    },
    API_TYPE: 30,
    API_VERSION: 671,
  };
  const serviceMap = new Proxy({}, { get: () => ["https://zca-js.invalid"] });
  return new zca.API(ctx, serviceMap, ["wss://zca-js.invalid/ws"]);
}

test("đúng bản zca-js đã ghim trong package.json", () => {
  assert.equal(installed.version, pkg.dependencies["zca-js"]);
});

test("Zalo: login (cookie) và loginQR còn nguyên; cookie jar có các hàm gateway gọi", () => {
  assert.equal(typeof zca.Zalo, "function");
  const zalo = new zca.Zalo({ selfListen: false, checkUpdate: false, logging: false });
  assert.equal(typeof zalo.login, "function");
  assert.equal(typeof zalo.loginQR, "function");
  // session.ts persist(): ctx.cookie.serializeSync(); dispatcher tải media: getCookieStringSync.
  const jar = zalo.parseCookies([{ key: "zpw_sek", value: "x", domain: ".chat.zalo.me" }]);
  assert.equal(typeof jar.serializeSync, "function");
  assert.equal(typeof jar.getCookieStringSync, "function");
});

test("API: mọi method gateway gọi đều còn (types.ts ZaloApi, voice.ts, groups.ts)", () => {
  const api = offlineApi();
  for (const name of [
    "sendMessage",
    "sendSeenEvent",
    "sendTypingEvent",
    "addReaction",
    "uploadAttachment",
    "sendVoice",
    "getGroupInfo",
    "getAllGroups",
    "getOwnId",
    "getContext",
  ]) {
    assert.equal(typeof api[name], "function", `zca-js thiếu api.${name}`);
  }
  assert.equal(api.getOwnId(), "1234567890");
  const ctx = api.getContext();
  for (const key of ["cookie", "imei", "userAgent"]) assert.ok(key in ctx, `ctx thiếu ${key}`);
});

test("listener: on/start/stop và các sự kiện session.ts đăng ký", () => {
  const listener = offlineApi().listener;
  assert.equal(typeof listener.on, "function");
  assert.equal(typeof listener.start, "function");
  assert.equal(typeof listener.stop, "function");
  // stop() khi chưa start: không ném (gateway gọi lúc tắt/đăng xuất).
  listener.stop();
  assert.equal(zca.CloseReason.ManualClosure, 1000, "stop() đóng với mã 1000");
});

test("enum khớp shim zca-js-compat.d.ts và hằng số của gateway", () => {
  assert.equal(zca.ThreadType.User, 0);
  assert.equal(zca.ThreadType.Group, 1);
  assert.equal(zca.LoginQRCallbackEventType.QRCodeGenerated, 0);
  assert.equal(zca.LoginQRCallbackEventType.QRCodeExpired, 1);
  assert.equal(zca.LoginQRCallbackEventType.QRCodeScanned, 2);
  assert.equal(zca.LoginQRCallbackEventType.QRCodeDeclined, 3);
  assert.equal(zca.LoginQRCallbackEventType.GotLoginInfo, 4);
  assert.equal(zca.DestType.User, DEST_TYPE_USER);
  assert.equal(zca.Reactions.OK, REACTION_QUEUED);
  const styles = new Set(Object.values(zca.TextStyle));
  for (const style of ZALO_TEXT_STYLES) assert.ok(styles.has(style), `TextStyle thiếu ${style}`);
  const groupEvents = new Set(Object.values(zca.GroupEventType));
  for (const event of ROLE_EVENTS)
    assert.ok(groupEvents.has(event), `GroupEventType thiếu ${event}`);
});

test("ZaloApiError: Zalo từ chối mang name + code số (sender.isZaloRejection dựa vào đây)", () => {
  const error = new zca.ZaloApiError("bị từ chối", 114);
  assert.equal(error.name, "ZcaApiError");
  assert.equal(error.code, 114);
});
