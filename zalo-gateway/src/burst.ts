/**
 * Gộp tin chữ liên tiếp của cùng 1 người gửi thành 1 lượt Python ("em ơi" /
 * "mai trời mưa ko" / "ở Đà Lạt" -> 1 câu trả lời thay vì 3, đỡ tốn quota AI):
 *
 * - debounce: tin chữ đầu tiên mở 1 "lô" và chờ `debounceMs` (mỗi tin mới gia
 *   hạn lại), tối đa `maxBufferItems` tin hoặc `maxWaitMs` tính từ tin đầu thì
 *   đưa lô vào hàng đợi ngay;
 * - gộp lúc tới lượt: lô đã vào hàng đợi nhưng CHƯA chạy (đang chờ lượt trước
 *   của chính người đó) vẫn nhận thêm tin chữ, tới `maxMergeItems` tin /
 *   `maxMergeChars` ký tự - các dòng gõ trong lúc lượt trước đang chạy (5-60s)
 *   thành 1 lượt thay vì mỗi dòng 1 lượt;
 * - tin không phải chữ thường (lệnh, link, ảnh, ghi âm, file, ghép đôi) gọi
 *   close(): lô đang chờ debounce vào hàng đợi NGAY và lô đó thôi nhận thêm, để
 *   giữ đúng thứ tự tin.
 *
 * RAM: mỗi lô tối đa maxMergeItems tin / maxMergeChars ký tự, tối đa
 * `maxSenders` lô đang debounce (vượt thì lô mới vào hàng đợi ngay).
 */

export type BatchState = "buffering" | "queued" | "started";

type Batch<T> = {
  items: T[];
  chars: number;
  state: BatchState;
  firstAt: number;
  timer: NodeJS.Timeout | null;
  /** Đã close(): không nhận thêm tin (có tin khác loại xếp sau nó). */
  closed: boolean;
};

export type BatcherOptions<T> = {
  debounceMs: number;
  maxWaitMs: number;
  maxBufferItems: number;
  maxMergeItems: number;
  maxMergeChars: number;
  maxSenders?: number;
  charsOf: (item: T) => number;
  /**
   * Đưa việc vào hàng đợi theo người gửi (KeyedSerialQueue.add). Promise reject
   * (vd hàng đợi đầy) được chuyển cho `onRejected` cùng các tin của lô.
   */
  enqueue: (sender: string, work: () => Promise<void>) => Promise<void>;
  /** Chạy 1 lô (đã đủ tin) khi tới lượt. */
  run: (sender: string, items: T[]) => Promise<void>;
  onRejected?: (sender: string, items: T[], error: unknown) => void;
  now?: () => number;
};

export type AddResult = "buffered" | "merged" | "queued";

export class DirectTextBatcher<T> {
  private readonly open = new Map<string, Batch<T>>();
  private readonly options: Required<Omit<BatcherOptions<T>, "onRejected">> &
    Pick<BatcherOptions<T>, "onRejected">;

  constructor(options: BatcherOptions<T>) {
    this.options = {
      maxSenders: 256,
      now: Date.now,
      ...options,
      debounceMs: Math.max(0, options.debounceMs),
      maxWaitMs: Math.max(0, options.maxWaitMs),
      maxBufferItems: Math.max(1, Math.floor(options.maxBufferItems)),
      maxMergeItems: Math.max(1, Math.floor(options.maxMergeItems)),
      maxMergeChars: Math.max(1, Math.floor(options.maxMergeChars)),
    };
  }

  /** Số lô đang chờ debounce (chưa vào hàng đợi). */
  get buffering(): number {
    let count = 0;
    for (const batch of this.open.values()) if (batch.state === "buffering") count++;
    return count;
  }

  /** Số người gửi đang có lô mở (debounce hoặc đã vào hàng đợi, chưa chạy). */
  get size(): number {
    return this.open.size;
  }

  add(sender: string, item: T): AddResult {
    const o = this.options;
    const chars = Math.max(0, o.charsOf(item));
    const batch = this.open.get(sender);
    if (batch && !batch.closed && batch.state !== "started") {
      const fits = batch.items.length < o.maxMergeItems && batch.chars + chars <= o.maxMergeChars;
      if (fits && batch.state === "queued") {
        batch.items.push(item);
        batch.chars += chars;
        return "merged";
      }
      if (fits) {
        batch.items.push(item);
        batch.chars += chars;
        if (batch.items.length >= o.maxBufferItems || o.now() - batch.firstAt >= o.maxWaitMs) {
          this.dispatch(sender, batch);
          return "queued";
        }
        this.schedule(sender, batch);
        return "buffered";
      }
      // Lô hiện tại đã đầy: đưa nó đi (nếu còn debounce) rồi mở lô mới phía sau.
      if (batch.state === "buffering") this.dispatch(sender, batch);
    }
    const next: Batch<T> = {
      items: [item],
      chars,
      state: "buffering",
      firstAt: o.now(),
      timer: null,
      closed: false,
    };
    this.open.set(sender, next);
    if (o.debounceMs <= 0 || o.maxBufferItems <= 1 || this.buffering > o.maxSenders) {
      this.dispatch(sender, next);
      return "queued";
    }
    this.schedule(sender, next);
    return "buffered";
  }

  /** Đưa lô đang debounce của người gửi vào hàng đợi ngay (lô vẫn nhận thêm khi chờ lượt). */
  flush(sender: string): boolean {
    const batch = this.open.get(sender);
    if (!batch || batch.state !== "buffering") return false;
    this.dispatch(sender, batch);
    return true;
  }

  /**
   * Có tin khác loại sắp xếp hàng sau: đưa lô đang debounce đi ngay và đóng lô
   * (tin chữ tới sau mở lô mới, đứng sau tin khác loại đó).
   */
  close(sender: string): void {
    const batch = this.open.get(sender);
    if (!batch) return;
    if (batch.state === "buffering") this.dispatch(sender, batch);
    batch.closed = true;
    if (this.open.get(sender) === batch) this.open.delete(sender);
  }

  /** Bỏ lô CHƯA vào hàng đợi (vd "/huy"); trả số tin đã bỏ. */
  discardBuffered(sender: string): number {
    const batch = this.open.get(sender);
    if (!batch || batch.state !== "buffering") return 0;
    if (batch.timer) clearTimeout(batch.timer);
    batch.timer = null;
    batch.closed = true;
    this.open.delete(sender);
    return batch.items.length;
  }

  private schedule(sender: string, batch: Batch<T>): void {
    const o = this.options;
    if (batch.timer) clearTimeout(batch.timer);
    const left = Math.max(0, batch.firstAt + o.maxWaitMs - o.now());
    batch.timer = setTimeout(() => this.dispatch(sender, batch), Math.min(o.debounceMs, left));
    batch.timer.unref?.();
  }

  private dispatch(sender: string, batch: Batch<T>): void {
    if (batch.state !== "buffering") return;
    if (batch.timer) clearTimeout(batch.timer);
    batch.timer = null;
    batch.state = "queued";
    this.options
      .enqueue(sender, async () => {
        batch.state = "started";
        if (this.open.get(sender) === batch) this.open.delete(sender);
        await this.options.run(sender, batch.items);
      })
      .catch((error) => {
        // Hàng đợi từ chối (đầy) hoặc lượt lỗi: lô không còn nhận thêm tin.
        batch.closed = true;
        if (this.open.get(sender) === batch) this.open.delete(sender);
        this.options.onRejected?.(sender, batch.items, error);
      });
  }
}
