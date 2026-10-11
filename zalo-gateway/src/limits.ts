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

export async function readLimitedBody(response: Response, maxBytes: number): Promise<Uint8Array> {
  if (!response.body) throw new Error("Empty image response");
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.length;
      if (size > maxBytes) throw new Error("Image exceeds download limit");
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
