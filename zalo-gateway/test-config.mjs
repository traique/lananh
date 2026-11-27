import test from "node:test";
import assert from "node:assert/strict";
import { loadConfig } from "./dist/config.js";

const KEYS = [
  "ZALO_BRIDGE_SECRET",
  "ZALO_BRIDGE_BASE_URL",
  "PORT",
  "ZALO_COOKIE_JSON",
  "ZALO_GROUP_REFRESH_MS",
  "ZALO_OUTBOX_POLL_MS",
  "ZALO_CONTROL_PORT",
  "ZALO_FB_MERGE_WINDOW_MS",
  "ZALO_DIRECT_CONCURRENCY",
  "ZALO_DIRECT_SENDER_BACKLOG",
  "ZALO_DIRECT_DEBOUNCE_MS",
  "ZALO_DIRECT_DEBOUNCE_MAX_MS",
  "ZALO_DIRECT_DEBOUNCE_MAX_MESSAGES",
  "ZALO_SEND_GAP_MIN_MS",
  "ZALO_SEND_GAP_MAX_MS",
  "ZALO_OUTBOX_MAX_PER_HOUR",
  "ZALO_PRESENCE_EVENTS",
  "VOICE_MAX_BYTES",
  "DOCUMENT_MAX_BYTES",
];

/** Chạy loadConfig với đúng bộ biến môi trường `env` (biến khác trong KEYS bị xoá). */
function configWith(env) {
  const saved = Object.fromEntries(KEYS.map((key) => [key, process.env[key]]));
  try {
    for (const key of KEYS) delete process.env[key];
    Object.assign(process.env, { ZALO_BRIDGE_SECRET: "s", ...env });
    return loadConfig();
  } finally {
    for (const [key, value] of Object.entries(saved)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
}

test("mặc định: poll outbox 90s và nạp nhóm 5 phút (Python kick/refresh khi có thay đổi)", () => {
  const config = configWith({});
  assert.equal(config.outboxPollMs, 90000);
  assert.equal(config.groupRefreshMs, 300000);
  assert.equal(config.controlPort, 9901);
  assert.equal(config.bridgeBaseUrl, "http://127.0.0.1:8000/internal/zalo");
  assert.equal(config.directConcurrency, 1);
  assert.equal(config.directPerSenderBacklog, 8);
  assert.equal(config.directDebounceMs, 2000);
  assert.equal(config.outboxMaxPerHour, 60);
  assert.equal(config.presenceEvents, true);
  assert.equal(config.voiceMaxBytes, 5 * 1024 * 1024);
});

test("thiếu ZALO_BRIDGE_SECRET hoặc cookie JSON hỏng: dừng ngay với lỗi rõ ràng", () => {
  assert.throws(() => configWith({ ZALO_BRIDGE_SECRET: "  " }), /ZALO_BRIDGE_SECRET/);
  assert.throws(() => configWith({ ZALO_COOKIE_JSON: "{hỏng" }), /ZALO_COOKIE_JSON/);
  assert.deepEqual(configWith({ ZALO_COOKIE_JSON: '[{"key":"a"}]' }).cookie, [{ key: "a" }]);
});

test("chu kỳ poll/nạp nhóm: env ghi đè được, tối thiểu 1s, rác thì về mặc định", () => {
  assert.equal(configWith({ ZALO_OUTBOX_POLL_MS: "30000" }).outboxPollMs, 30000);
  assert.equal(configWith({ ZALO_OUTBOX_POLL_MS: "10" }).outboxPollMs, 1000);
  assert.equal(configWith({ ZALO_OUTBOX_POLL_MS: "abc" }).outboxPollMs, 90000);
  assert.equal(configWith({ ZALO_GROUP_REFRESH_MS: "60000" }).groupRefreshMs, 60000);
  assert.equal(configWith({ ZALO_GROUP_REFRESH_MS: "-5" }).groupRefreshMs, 1000);
  assert.equal(configWith({ ZALO_CONTROL_PORT: "80" }).controlPort, 1024);
});

test("trần hàng đợi tin riêng và gộp Facebook", () => {
  assert.equal(configWith({ ZALO_DIRECT_CONCURRENCY: "50" }).directConcurrency, 8);
  assert.equal(configWith({ ZALO_DIRECT_CONCURRENCY: "0" }).directConcurrency, 1);
  assert.equal(configWith({ ZALO_DIRECT_CONCURRENCY: "2.7" }).directConcurrency, 2);
  assert.equal(configWith({ ZALO_DIRECT_SENDER_BACKLOG: "999" }).directPerSenderBacklog, 50);
  assert.equal(configWith({ ZALO_FB_MERGE_WINDOW_MS: "999999" }).facebookMergeWindowMs, 120000);
  assert.equal(configWith({ ZALO_FB_MERGE_WINDOW_MS: "10" }).facebookMergeWindowMs, 3000);
});

test("biến cho phép 0 (tắt): debounce, nghỉ giữa tin, ngân sách outbox; trần trên vẫn áp", () => {
  const off = configWith({
    ZALO_DIRECT_DEBOUNCE_MS: "0",
    ZALO_SEND_GAP_MIN_MS: "0",
    ZALO_SEND_GAP_MAX_MS: "0",
    ZALO_OUTBOX_MAX_PER_HOUR: "0",
    ZALO_PRESENCE_EVENTS: "off",
  });
  assert.equal(off.directDebounceMs, 0);
  assert.equal(off.sendGapMinMs, 0);
  assert.equal(off.sendGapMaxMs, 0);
  assert.equal(off.outboxMaxPerHour, 0);
  assert.equal(off.presenceEvents, false);
  const big = configWith({
    ZALO_DIRECT_DEBOUNCE_MS: "99999",
    ZALO_DIRECT_DEBOUNCE_MAX_MS: "-1",
    ZALO_DIRECT_DEBOUNCE_MAX_MESSAGES: "0",
    ZALO_SEND_GAP_MAX_MS: "1e9",
  });
  assert.equal(big.directDebounceMs, 10000);
  assert.equal(big.directDebounceMaxMs, 0);
  assert.equal(big.directDebounceMaxMessages, 1);
  assert.equal(big.sendGapMaxMs, 10000);
  assert.equal(configWith({ ZALO_SEND_GAP_MIN_MS: "x" }).sendGapMinMs, 600);
});

test("trần media dùng chung với Python: 64 KiB - 8 MiB", () => {
  assert.equal(configWith({ VOICE_MAX_BYTES: "1" }).voiceMaxBytes, 64 * 1024);
  assert.equal(configWith({ DOCUMENT_MAX_BYTES: "999999999" }).documentMaxBytes, 8 * 1024 * 1024);
  assert.equal(configWith({ DOCUMENT_MAX_BYTES: "hỏng" }).documentMaxBytes, 5 * 1024 * 1024);
});

test("ZALO_BRIDGE_BASE_URL bỏ dấu / cuối; PORT đổi cổng loopback mặc định", () => {
  assert.equal(
    configWith({ ZALO_BRIDGE_BASE_URL: "http://web:9000/internal/zalo/" }).bridgeBaseUrl,
    "http://web:9000/internal/zalo",
  );
  assert.equal(configWith({ PORT: "10000" }).bridgeBaseUrl, "http://127.0.0.1:10000/internal/zalo");
});
