/**
 * Làn nhanh tin riêng: /huy và lệnh chỉ đọc (commands.ts) gửi thẳng Python NGOÀI
 * hàng đợi theo người gửi, timeout ngắn - dùng được khi lượt dài của chính người
 * đó đang chạy. Có trần để 1 người (hay người lạ) gửi dồn dập không mở hàng chục
 * request song song tranh pool DB của Python:
 *
 * - lệnh chỉ đọc: tối đa `commandLimit` lệnh chờ Python cùng lúc; vượt thì vào
 *   hàng đợi theo người gửi như lệnh thường;
 * - /huy: mỗi người tối đa 1 lệnh chờ Python (gửi thêm khi đang chờ thì chỉ bỏ
 *   tin chữ chưa gửi tại gateway), tổng tối đa `cancelLimit` người; vượt thì BỎ
 *   (/huy xếp hàng chỉ chạy sau chính lượt nó định huỷ - vô ích).
 *
 * RAM: tập người đang chờ /huy tối đa `cancelLimit` phần tử.
 */

/** Phần DirectTextBatcher làn nhanh cần (burst.ts). */
export type FastLaneBatcher = {
  flush(sender: string): boolean;
  close(sender: string): void;
  discardBuffered(sender: string): number;
};

export type FastLaneOptions<M> = {
  /** Số lệnh chỉ đọc chờ Python cùng lúc; vượt thì vào hàng đợi theo người gửi. */
  commandLimit: number;
  /** Số người có /huy chờ Python cùng lúc (mỗi người tối đa 1); vượt thì bỏ. */
  cancelLimit: number;
  isCancel(msg: M): boolean;
  batcher: FastLaneBatcher;
  /** Gọi Python và gửi trả lời (nơi gọi tự báo lỗi cho người gửi). */
  call(msg: M): Promise<void>;
  /** Làn nhanh đầy: đưa lệnh chỉ đọc vào hàng đợi theo người gửi. */
  enqueue(msg: M): void;
  log?(message: string): void;
};

/**
 * sent = đã gửi Python; queued = làn nhanh đầy, vào hàng đợi; coalesced = /huy
 * của người này đang chờ Python, không gửi thêm; dropped = quá trần /huy, bỏ.
 */
export type FastLaneResult = "sent" | "queued" | "coalesced" | "dropped";

export class FastLane<M extends { sender: string }> {
  private commands = 0;
  private readonly cancels = new Set<string>();
  private readonly options: FastLaneOptions<M>;

  constructor(options: FastLaneOptions<M>) {
    this.options = {
      ...options,
      commandLimit: Math.max(1, Math.floor(options.commandLimit)),
      cancelLimit: Math.max(1, Math.floor(options.cancelLimit)),
    };
  }

  /** Số lệnh làn nhanh đang chờ Python (lệnh chỉ đọc + /huy). */
  get active(): number {
    return this.commands + this.cancels.size;
  }

  submit(msg: M): FastLaneResult {
    const o = this.options;
    if (o.isCancel(msg)) return this.cancel(msg);
    if (this.commands >= o.commandLimit) {
      // Như lệnh thường (routeDirect "queue"): close() chứ không chỉ flush() - tin
      // chữ gõ SAU lệnh phải mở lô mới phía sau lệnh, không gộp vào lô trước nó.
      o.batcher.close(msg.sender);
      o.enqueue(msg);
      return "queued";
    }
    // Trả lời ngay: tin chữ đang debounce vào hàng đợi trước (lô vẫn nhận thêm,
    // lệnh không đứng giữa chúng trong hàng đợi).
    o.batcher.flush(msg.sender);
    this.commands++;
    void this.run(msg, () => {
      this.commands--;
    });
    return "sent";
  }

  private cancel(msg: M): FastLaneResult {
    const o = this.options;
    const log = o.log ?? console.log;
    const dropped = o.batcher.discardBuffered(msg.sender);
    if (dropped) log(`[zalo] /huy: bỏ ${dropped} tin chữ chưa gửi của ${msg.sender}`);
    if (this.cancels.has(msg.sender)) return "coalesced";
    if (this.cancels.size >= o.cancelLimit) {
      log(`[zalo] /huy của ${msg.sender} bị bỏ: đã có ${this.cancels.size} lệnh huỷ đang chờ`);
      return "dropped";
    }
    this.cancels.add(msg.sender);
    void this.run(msg, () => {
      this.cancels.delete(msg.sender);
    });
    return "sent";
  }

  private async run(msg: M, release: () => void): Promise<void> {
    try {
      await this.options.call(msg);
    } catch (error) {
      const reason = error instanceof Error ? error.message : String(error);
      (this.options.log ?? console.error)(`[zalo] lệnh làn nhanh của ${msg.sender} lỗi: ${reason}`);
    } finally {
      release();
    }
  }
}
