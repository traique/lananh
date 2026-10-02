/** Bound waiting events and streamed downloads; the V8 heap cap excludes buffers. */
export class BoundedSerialQueue {
  private tail: Promise<void> = Promise.resolve();
  private pending = 0;
  constructor(private readonly capacity: number) {}
  add(work: () => Promise<unknown>): Promise<void> {
    if (this.pending >= this.capacity) return Promise.reject(new Error("Zalo queue overloaded; event rejected"));
    this.pending++;
    const job = this.tail.then(work).then(() => undefined).finally(() => { this.pending--; });
    this.tail = job.catch(() => undefined);
    return job;
  }
}

export async function readLimitedBody(response: Response, maxBytes: number): Promise<Uint8Array> {
  if (!response.body) throw new Error("Empty image response");
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const {value, done} = await reader.read();
      if (done) break;
      size += value.length;
      if (size > maxBytes) throw new Error("Image exceeds download limit");
      chunks.push(value);
    }
    const result = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) { result.set(chunk, offset); offset += chunk.length; }
    return result;
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}
