import test from "node:test";
import assert from "node:assert/strict";
import { GroupRoles, ROLE_EVENTS, ROLE_MAX_ADMINS } from "./dist/group-roles.js";
import { startControlServer } from "./dist/control-server.js";
import { BoundedSerialQueue, QueueFullError } from "./dist/limits.js";

// Group ID Zalo thật dài 19 chữ số (vượt Number.MAX_SAFE_INTEGER) nên dựng bằng chuỗi.
const GID = "7212000000000000001";

function fakeApi(detail, { fail = false } = {}) {
  const calls = [];
  return {
    calls,
    getGroupInfo: async (id) => {
      calls.push(id);
      if (fail) throw new Error("zalo lỗi");
      return {
        gridInfoMap: { [id]: typeof detail === "function" ? detail(calls.length) : detail },
      };
    },
  };
}

test("GroupRoles: trưởng nhóm = creatorId, phó nhóm = adminIds, còn lại là thành viên", async () => {
  const api = fakeApi({ creatorId: "boss", adminIds: ["mod1", 42] });
  const roles = new GroupRoles(() => api);
  assert.equal(await roles.roleOf(GID, "boss"), "owner");
  assert.equal(await roles.roleOf(GID, "mod1"), "admin");
  assert.equal(await roles.roleOf(GID, "42"), "admin", "id số được đổi thành chuỗi");
  assert.equal(await roles.roleOf(GID, "ai-do"), "member");
  assert.equal(await roles.roleOf(GID, ""), "unknown");
  assert.deepEqual(api.calls, [GID], "cache: chỉ gọi Zalo 1 lần trong TTL");
});

test("GroupRoles: hết TTL hoặc có sự kiện đổi admin thì lấy lại", async () => {
  let now = 1_000_000;
  const api = fakeApi((call) =>
    call === 1 ? { creatorId: "boss", adminIds: [] } : { creatorId: "boss", adminIds: ["mod"] },
  );
  const roles = new GroupRoles(() => api, { ttlMs: 60_000, now: () => now });
  assert.equal(await roles.roleOf(GID, "mod"), "member");
  roles.invalidate(GID);
  assert.equal(await roles.roleOf(GID, "mod"), "admin", "sự kiện add_admin làm mới ngay");
  now += 59_000;
  await roles.roleOf(GID, "mod");
  assert.equal(api.calls.length, 2);
  now += 2_000;
  await roles.roleOf(GID, "mod");
  assert.equal(api.calls.length, 3, "quá TTL thì lấy lại");
  assert.ok(ROLE_EVENTS.has("add_admin") && ROLE_EVENTS.has("remove_admin"));
  assert.ok(!ROLE_EVENTS.has("join"), "người vào nhóm không cần lấy lại admin");
});

test("GroupRoles: lấy lỗi -> unknown, không gọi lại trước retryMs, giữ bản cũ khi đã có", async () => {
  let now = 0;
  let fail = true;
  const calls = [];
  const api = {
    getGroupInfo: async (id) => {
      calls.push(id);
      if (fail) throw new Error("zalo lỗi");
      return { gridInfoMap: { [id]: { creatorId: "boss", adminIds: [] } } };
    },
  };
  const warn = console.warn;
  console.warn = () => undefined;
  try {
    const roles = new GroupRoles(() => api, { ttlMs: 1_000, retryMs: 60_000, now: () => now });
    assert.equal(await roles.roleOf(GID, "boss"), "unknown");
    assert.equal(await roles.roleOf(GID, "boss"), "unknown");
    assert.equal(calls.length, 1, "trong retryMs không gọi lại Zalo");
    now += 60_000;
    fail = false;
    assert.equal(await roles.roleOf(GID, "boss"), "owner");
    now += 2_000; // quá TTL, Zalo lại lỗi: vẫn dùng bản cũ
    fail = true;
    assert.equal(await roles.roleOf(GID, "boss"), "owner");
    assert.equal(calls.length, 3);
  } finally {
    console.warn = warn;
  }
});

test("GroupRoles: phản hồi thiếu creatorId lẫn adminIds -> unknown, không đoán là thành viên", async () => {
  const warn = console.warn;
  console.warn = () => undefined;
  try {
    const roles = new GroupRoles(() => fakeApi({ name: "Room", totalMember: 9 }));
    assert.equal(await roles.roleOf(GID, "boss"), "unknown");
    assert.equal(roles.size, 0);
    const onlyAdmins = new GroupRoles(() => fakeApi({ adminIds: ["mod"] }));
    assert.equal(await onlyAdmins.roleOf(GID, "mod"), "admin");
    assert.equal(await onlyAdmins.roleOf(GID, "x"), "member");
  } finally {
    console.warn = warn;
  }
});

test("GroupRoles: Zalo chưa kết nối -> unknown; chưa trả kịp thì không chặn quá timeout", async () => {
  const offline = new GroupRoles(() => null);
  assert.equal(await offline.roleOf(GID, "boss"), "unknown");

  let release;
  const slow = {
    getGroupInfo: (id) =>
      new Promise((resolve) => {
        release = () => resolve({ gridInfoMap: { [id]: { creatorId: "boss", adminIds: [] } } });
      }),
  };
  const roles = new GroupRoles(() => slow, { timeoutMs: 20 });
  // Timer chờ của withTimeout là unref: giữ event loop sống trong lúc chờ.
  const keepAlive = setInterval(() => undefined, 1000);
  try {
    assert.equal(await roles.roleOf(GID, "boss"), "unknown", "quá giờ chờ: không biết");
    release();
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.equal(await roles.roleOf(GID, "boss"), "owner", "lượt gọi chạy tiếp và cập nhật cache");
  } finally {
    clearInterval(keepAlive);
  }
});

test("GroupRoles: 1 lượt gọi/nhóm cùng lúc, có trần số nhóm và số phó nhóm, retain bỏ nhóm cũ", async () => {
  const calls = [];
  const api = {
    getGroupInfo: async (id) => {
      calls.push(id);
      await new Promise((resolve) => setTimeout(resolve, 5));
      const adminIds = Array.from({ length: ROLE_MAX_ADMINS + 50 }, (_, i) => `m${i}`);
      return { gridInfoMap: { [id]: { creatorId: "boss", adminIds } } };
    },
  };
  const roles = new GroupRoles(() => api, { maxGroups: 2 });
  await Promise.all([roles.roleOf("g1", "x"), roles.roleOf("g1", "y"), roles.roleOf("g1", "z")]);
  assert.deepEqual(calls, ["g1"]);
  assert.equal(await roles.roleOf("g1", `m${ROLE_MAX_ADMINS - 1}`), "admin");
  assert.equal(await roles.roleOf("g1", `m${ROLE_MAX_ADMINS}`), "member", "quá trần phó nhóm");
  await roles.roleOf("g2", "x");
  await roles.roleOf("g3", "x");
  assert.equal(roles.size, 2, "trần số nhóm: bỏ nhóm làm mới lâu nhất");
  roles.retain(new Set(["g3"]));
  assert.equal(roles.size, 1);
});

test("BoundedSerialQueue.pending đếm việc đang chờ/chạy", async () => {
  const queue = new BoundedSerialQueue(1);
  let release;
  const first = queue.add(() => new Promise((resolve) => (release = resolve)));
  assert.equal(queue.pending, 1);
  await assert.rejects(
    queue.add(async () => undefined),
    QueueFullError,
  );
  release();
  await first;
  assert.equal(queue.pending, 0);
});

test("control server POST /groups/refresh: 200 khi nạp lại được, 409 khi Zalo chưa kết nối", async () => {
  let connected = true;
  let refreshed = 0;
  const server = startControlServer(0, {
    status: () => ({}),
    startQr: async () => "idle",
    startPairing: () => null,
    logout: async () => undefined,
    forgetSender: () => false,
    groups: async () => null,
    refreshGroups: async () => {
      if (!connected) return false;
      refreshed++;
      return true;
    },
  });
  try {
    if (!server.listening) await new Promise((resolve) => server.once("listening", resolve));
    const url = `http://127.0.0.1:${server.address().port}/groups/refresh`;
    const ok = await fetch(url, { method: "POST" });
    assert.equal(ok.status, 200);
    assert.deepEqual(await ok.json(), { ok: true });
    connected = false;
    assert.equal((await fetch(url, { method: "POST" })).status, 409);
    assert.equal((await fetch(url)).status, 404, "chỉ nhận POST");
    assert.equal(refreshed, 1);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});
