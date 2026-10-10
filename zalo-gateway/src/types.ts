/** Phần API của zca-js mà gateway thực sự dùng (zca-js không xuất type đúng cho NodeNext). */
export type ZaloCookieJar = {
  serializeSync(): unknown;
  getCookieStringSync?(url: string): string;
};

export type ZaloContext = {
  cookie: ZaloCookieJar;
  imei: string;
  userAgent: string;
};

export type ZaloMessage = {
  isSelf?: boolean;
  type: number;
  threadId: string | number;
  data?: {
    content?: unknown;
    uidFrom?: string | number;
    msgId?: string | number;
    cliMsgId?: string | number;
    dName?: string;
    ts?: string | number;
  };
};

export type ZaloListener = {
  on(event: "message", handler: (message: ZaloMessage) => void): void;
  on(event: "disconnected" | "closed", handler: () => void): void;
  on(event: "error", handler: (error: unknown) => void): void;
  start(): void;
  stop?(): void;
};

export type ZaloAttachment = {
  data: Buffer;
  filename: string;
  metadata: { totalSize: number };
};

export type ZaloApi = {
  listener: ZaloListener;
  getOwnId(): string | number;
  getContext(): ZaloContext;
  sendMessage(
    message: { msg: string; attachments?: ZaloAttachment[] },
    threadId: string,
    type: number,
  ): Promise<unknown>;
  getAllGroups(): Promise<{ gridVerMap?: Record<string, unknown> }>;
  getGroupInfo(groupId: string): Promise<{
    changed_groups?: Record<string, { name?: string }>;
    gridInfoMap?: Record<string, { name?: string }>;
  }>;
};

export type MediaItem = { mime: string; bytes: Uint8Array };
