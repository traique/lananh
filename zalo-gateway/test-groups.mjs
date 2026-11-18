import test from "node:test";
import assert from "node:assert/strict";
import { INFO_BATCH, MAX_GROUPS, collectGroups } from "./dist/groups.js";
import { startControlServer } from "./dist/control-server.js";

// Group ID Zalo thật dài 19 chữ số (vượt Number.MAX_SAFE_INTEGER) nên dựng bằng chuỗi.
const groupId = (i) => `72120000000000${String(i).padStart(5, "0")}`;

function fakeApi(ids, { failCall = -1 } = {}) {
  const calls = [];
  return {
    calls,
    getAllGroups: async () => ({ gridVerMap: Object.fromEntries(ids.map((id) => [id, "1"])) }),
    getGroupInfo: async (batch) => {
      calls.push(batch);
      if (calls.length - 1 === failCall) throw new Error("zalo lỗi");
      const map = Object.fromEntries(
        batch.map((id) => [id, { name: `  Nhóm ${id.slice(-2)} `, totalMember: 7 }]),
      );
      return { gridInfoMap: map };
    },
  };
}

test("collectGroups lấy tên theo lô thay vì 1 lệnh gọi/nhóm; lô lỗi vẫn trả id", async () => {
  const ids = Array.from({ length: 45 }, (_, i) => groupId(i));
  const api = fakeApi(ids, { failCall: 1 });
  const { total, groups } = await collectGroups(api);
  assert.equal(INFO_BATCH, 20);
  assert.deepEqual(
    api.calls.map((batch) => batch.length),
    [20, 20, 5],
  );
  assert.equal(total, 45);
  assert.equal(groups.length, 45);
  assert.deepEqual(groups[0], { id: ids[0], name: "Nhóm 00", members: 7 });
  // Lô thứ 2 lỗi: người dùng vẫn thấy group ID, tên để trống.
  assert.deepEqual(groups[20], { id: ids[20], name: "", members: null });
  assert.deepEqual(groups[44], { id: ids[44], name: "Nhóm 44", members: 7 });
});

test("collectGroups có trần số nhóm, total vẫn là số nhóm thật", async () => {
  assert.equal(MAX_GROUPS, 200);
  const api = fakeApi(["a", "b", "c"]);
  const { total, groups } = await collectGroups(api, 2, 20);
  assert.equal(total, 3);
  assert.deepEqual(
    groups.map((group) => group.id),
    ["a", "b"],
  );
  const empty = await collectGroups({
    getAllGroups: async () => ({}),
    getGroupInfo: api.getGroupInfo,
  });
  assert.deepEqual(empty, { total: 0, groups: [] });
});

test("control server GET /groups: 200 kèm danh sách, 409 khi Zalo chưa kết nối", async () => {
  let connected = true;
  const server = startControlServer(0, {
    status: () => ({}),
    startQr: async () => "idle",
    startPairing: () => null,
    logout: async () => undefined,
    forgetSender: () => false,
    groups: async () =>
      connected ? { total: 1, groups: [{ id: groupId(1), name: "Gia đình", members: 5 }] } : null,
  });
  try {
    if (!server.listening) await new Promise((resolve) => server.once("listening", resolve));
    const url = `http://127.0.0.1:${server.address().port}/groups`;
    const ok = await fetch(url);
    assert.equal(ok.status, 200);
    assert.deepEqual(await ok.json(), {
      total: 1,
      groups: [{ id: groupId(1), name: "Gia đình", members: 5 }],
    });
    connected = false;
    const offline = await fetch(url);
    assert.equal(offline.status, 409);
    assert.deepEqual(await offline.json(), { error: "Zalo is not connected" });
    assert.equal((await fetch(url, { method: "POST" })).status, 404);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});
