import test from "node:test";
import assert from "node:assert/strict";
import { SEND_RETRY_DELAYS_MS, STORE_RETRY_DELAYS_MS, retry } from "./dist/retry.js";

test("bảng thử lại: gửi 1s/3s/8s, lưu tin nhóm 1s/3s", () => {
  assert.deepEqual(SEND_RETRY_DELAYS_MS, [1000, 3000, 8000]);
  assert.deepEqual(STORE_RETRY_DELAYS_MS, [1000, 3000]);
});

test("retry: thành công ngay thì không chờ", async () => {
  const slept = [];
  const value = await retry(async () => 42, {
    delaysMs: [1, 2],
    sleep: async (ms) => slept.push(ms),
  });
  assert.equal(value, 42);
  assert.deepEqual(slept, []);
});

test("retry: chờ đúng bảng delay, trả kết quả lần thành công, báo onRetry", async () => {
  const slept = [];
  const retries = [];
  let calls = 0;
  const value = await retry(
    async (attempt) => {
      calls++;
      if (attempt < 2) throw new Error(`lỗi ${attempt}`);
      return "ok";
    },
    {
      delaysMs: [1000, 3000, 8000],
      sleep: async (ms) => slept.push(ms),
      onRetry: (error, attempt, delay) => retries.push([error.message, attempt, delay]),
    },
  );
  assert.equal(value, "ok");
  assert.equal(calls, 3);
  assert.deepEqual(slept, [1000, 3000]);
  assert.deepEqual(retries, [
    ["lỗi 0", 1, 1000],
    ["lỗi 1", 2, 3000],
  ]);
});

test("retry: hết lượt thì ném lỗi cuối cùng, tổng số lần = delays + 1", async () => {
  const slept = [];
  let calls = 0;
  await assert.rejects(
    retry(
      async () => {
        calls++;
        throw new Error(`lần ${calls}`);
      },
      { delaysMs: [1, 3, 8], sleep: async (ms) => slept.push(ms) },
    ),
    /lần 4/,
  );
  assert.equal(calls, 4);
  assert.deepEqual(slept, [1, 3, 8]);
});

test("retry: shouldRetry trả false thì dừng ngay không chờ", async () => {
  const slept = [];
  let calls = 0;
  await assert.rejects(
    retry(
      async () => {
        calls++;
        throw Object.assign(new Error("not connected"), { permanent: true });
      },
      {
        delaysMs: [1, 2, 3],
        sleep: async (ms) => slept.push(ms),
        shouldRetry: (error) => !error.permanent,
      },
    ),
    /not connected/,
  );
  assert.equal(calls, 1);
  assert.deepEqual(slept, []);
});

test("retry: không có delay nào thì chỉ thử 1 lần", async () => {
  let calls = 0;
  await assert.rejects(
    retry(
      async () => {
        calls++;
        throw new Error("x");
      },
      { delaysMs: [] },
    ),
    /x/,
  );
  assert.equal(calls, 1);
});
