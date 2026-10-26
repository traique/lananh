/** Bound waiting events and streamed downloads; the V8 heap cap excludes buffers. */
export class QueueFullError extends Error {
  constructor(readonly scope: "key" | "global") {
    super(
      scope === "key"
        ? "Sender backlog full; event rejected"
        : "Zalo queue overloaded; event rejected",
    );
    this.name = "QueueFullError";
  }
}

export class BoundedSerialQueue {
  private tail: Promise<void> = Promise.resolve();
  private pending = 0;
  constructor(private readonly capacity: number) {}
  add(work: () => Promise<unknown>): Promise<void> {
    if (this.pending >= this.capacity) return Promise.reject(new QueueFullError("global"));
    this.pending++;
    const job = this.tail
      .then(work)
      .then(() => undefined)
      .finally(() => {
        this.pending--;
      });
    this.tail = job.catch(() => undefined);
    return job;
  }
}

type KeyedJob = {
  work: () => Promise<unknown>;
  resolve: () => void;
  reject: (error: unknown) => void;
};
type Lane = { jobs: KeyedJob[]; running: boolean };

export type KeyedQueueOptions = {
  /** Số việc chạy song song tối đa (mọi khoá cộng lại). */
  concurrency: number;
  /** Số việc tối đa của 1 khoá (đang chạy + đang chờ). */
  perKeyCapacity: number;
  /** Tổng số việc đang chờ (chưa chạy) của mọi khoá. */
  capacity: number;
};

/**
 * Hàng đợi theo khoá (người gửi): việc cùng khoá chạy tuần tự đúng thứ tự
 * nhận, khác khoá chạy song song tối đa `concurrency` việc, luân phiên giữa
 * các khoá để 1 người gửi dồn dập không chiếm hết lượt. Có trần việc chờ cho
 * từng khoá và tổng; khoá rảnh bị xoá ngay nên Map không phình theo số người gửi.
 */
export class KeyedSerialQueue {
  private readonly lanes = new Map<string, Lane>();
  /** Khoá có việc chờ và chưa chạy, theo thứ tự tới lượt. */
  private readonly ready: string[] = [];
  private active = 0;
  private waiting = 0;
  private readonly options: KeyedQueueOptions;

  constructor(options: KeyedQueueOptions) {
    this.options = {
      concurrency: Math.max(1, Math.floor(options.concurrency)),
      perKeyCapacity: Math.max(1, Math.floor(options.perKeyCapacity)),
      capacity: Math.max(1, Math.floor(options.capacity)),
    };
  }

  /** Số khoá đang có việc (đang chạy hoặc chờ). */
  get keys(): number {
    return this.lanes.size;
  }

  get pending(): number {
    return this.waiting;
  }

  get running(): number {
    return this.active;
  }

  /** Khoá này đang có việc CHẠY (tin mới của người gửi sẽ phải chờ sau nó). */
  isRunning(key: string): boolean {
    return this.lanes.get(key)?.running === true;
  }

  add(key: string, work: () => Promise<unknown>): Promise<void> {
    const existing = this.lanes.get(key);
    const queued = existing ? existing.jobs.length + (existing.running ? 1 : 0) : 0;
    if (queued >= this.options.perKeyCapacity) return Promise.reject(new QueueFullError("key"));
    if (this.waiting >= this.options.capacity) return Promise.reject(new QueueFullError("global"));
    return new Promise<void>((resolve, reject) => {
      let lane = existing;
      if (!lane) {
        lane = { jobs: [], running: false };
        this.lanes.set(key, lane);
      }
      lane.jobs.push({ work, resolve, reject });
      this.waiting++;
      if (!lane.running && lane.jobs.length === 1) this.ready.push(key);
      this.pump();
    });
  }

  private pump(): void {
    while (this.active < this.options.concurrency && this.ready.length) {
      const key = this.ready.shift() as string;
      const lane = this.lanes.get(key);
      const job = lane && !lane.running ? lane.jobs.shift() : undefined;
      if (!lane || !job) continue;
      this.waiting--;
      lane.running = true;
      this.active++;
      void this.run(key, lane, job);
    }
  }

  private async run(key: string, lane: Lane, job: KeyedJob): Promise<void> {
    try {
      await job.work();
      job.resolve();
    } catch (error) {
      job.reject(error);
    } finally {
      lane.running = false;
      this.active--;
      if (lane.jobs.length) this.ready.push(key);
      else this.lanes.delete(key);
      this.pump();
    }
  }
}

const noop = () => undefined;

/** Chờ `promise` (không bao giờ reject) tối đa `ms`; ms <= 0 = chờ tới khi xong. */
function waitAtMost(promise: Promise<void>, ms: number): Promise<void> {
  if (ms <= 0) return promise;
  let timer: NodeJS.Timeout | undefined;
  const timeout = new Promise<void>((resolve) => {
    timer = setTimeout(resolve, ms);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

/**
 * Khoá theo khoá (người nhận): việc cùng khoá chạy lần lượt, không chồng nhau -
 * dùng quanh việc GỬI tin để các đoạn của 1 câu trả lời không xen với câu trả lời
 * khác cho cùng người. Khác khoá chạy song song. Map chỉ giữ khoá đang có việc
 * (xoá khi việc cuối xong). Việc trước treo (zca-js không trả lời) thì việc sau
 * chỉ chờ tối đa `maxWaitMs` rồi chạy tiếp - không kẹt người nhận đó mãi.
 */
export class KeyedMutex {
  private readonly tails = new Map<string, Promise<void>>();
  constructor(private readonly maxWaitMs = 60000) {}

  /** Số khoá đang có việc (đang chạy hoặc chờ). */
  get size(): number {
    return this.tails.size;
  }

  run<T>(key: string, work: () => Promise<T>): Promise<T> {
    const previous = this.tails.get(key);
    const ready = previous ? waitAtMost(previous, this.maxWaitMs) : Promise.resolve();
    const result = ready.then(work);
    const tail = result.then(noop, noop);
    this.tails.set(key, tail);
    void tail.then(() => {
      if (this.tails.get(key) === tail) this.tails.delete(key);
    });
    return result;
  }
}

/** Cho phép mỗi khoá tối đa 1 lần / `intervalMs`; nhớ tối đa `capacity` khoá gần nhất. */
export class KeyedRateLimiter {
  private readonly last = new Map<string, number>();
  constructor(
    private readonly intervalMs: number,
    private readonly capacity = 256,
  ) {}

  allow(key: string, now = Date.now()): boolean {
    const previous = this.last.get(key);
    if (previous !== undefined && now - previous < this.intervalMs) return false;
    this.last.delete(key);
    this.last.set(key, now);
    while (this.last.size > this.capacity) {
      const oldest = this.last.keys().next().value;
      if (oldest === undefined) break;
      this.last.delete(oldest);
    }
    return true;
  }

  get size(): number {
    return this.last.size;
  }
}

/**
 * Ngân sách theo cửa sổ trượt: tối đa `limit` lượt trong `windowMs` gần nhất
 * (limit <= 0 = không giới hạn). Hết lượt thì việc được HOÃN (nơi gọi để lại cho
 * lần sau), không bao giờ bỏ. Chỉ nhớ tối đa `limit` mốc thời gian.
 */
export class SlidingWindowBudget {
  private readonly stamps: number[] = [];
  constructor(
    private readonly limit: number,
    private readonly windowMs: number,
  ) {}

  private prune(now: number): void {
    while (this.stamps.length && now - this.stamps[0] >= this.windowMs) this.stamps.shift();
  }

  /** Lấy 1 lượt nếu còn; false = đã hết ngân sách trong cửa sổ hiện tại. */
  tryTake(now = Date.now()): boolean {
    if (this.limit <= 0) return true;
    this.prune(now);
    if (this.stamps.length >= this.limit) return false;
    this.stamps.push(now);
    return true;
  }

  /** Số lượt còn lại trong cửa sổ (Infinity nếu không giới hạn). */
  remaining(now = Date.now()): number {
    if (this.limit <= 0) return Number.POSITIVE_INFINITY;
    this.prune(now);
    return this.limit - this.stamps.length;
  }
}

/** Body tải về vượt trần byte (readLimitedBody). */
export class BodyTooLargeError extends Error {
  constructor() {
    super("Body exceeds download limit");
    this.name = "BodyTooLargeError";
  }
}

export async function readLimitedBody(response: Response, maxBytes: number): Promise<Uint8Array> {
  if (!response.body) throw new Error("Empty media response");
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.length;
      if (size > maxBytes) throw new BodyTooLargeError();
      chunks.push(value);
    }
    const result = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) {
      result.set(chunk, offset);
      offset += chunk.length;
    }
    return result;
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}
