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

/** Tin được trích dẫn (zca-js dist/models/Message.d.ts TQuote). */
export type ZaloQuote = {
  ownerId?: string | number;
  cliMsgId?: string | number;
  globalMsgId?: string | number;
  cliMsgType?: number;
  ts?: string | number;
  /** Chữ của tin được trích (rỗng nếu tin gốc là ảnh/file). */
  msg?: unknown;
  /** JSON đính kèm của tin gốc (ảnh, file...) dạng chuỗi. */
  attach?: unknown;
  fromD?: string;
};

export type ZaloMessage = {
  isSelf?: boolean;
  type: number;
  threadId: string | number;
  data?: {
    content?: unknown;
    uidFrom?: string | number;
    idTo?: string | number;
    msgId?: string | number;
    cliMsgId?: string | number;
    dName?: string;
    ts?: string | number;
    msgType?: string;
    st?: number;
    at?: number;
    cmd?: number;
    quote?: ZaloQuote;
  };
};

export type ZaloListener = {
  on(event: "message", handler: (message: ZaloMessage) => void): void;
  on(event: "connected", handler: () => void): void;
  on(event: "disconnected" | "closed", handler: (code?: number, reason?: string) => void): void;
  on(event: "error", handler: (error: unknown) => void): void;
  /** retryOnClose: zca-js tự kết nối lại khi socket đóng với mã Zalo cho phép thử lại. */
  start(options?: { retryOnClose?: boolean }): void;
  stop?(): void;
};

export type ZaloAttachment = {
  data: Buffer;
  filename: string;
  metadata: { totalSize: number };
};

/**
 * Giá trị TextStyle của zca-js 2.1.2 (dist/apis/sendMessage.d.ts) mà Python gửi
 * kèm câu trả lời (channels/contracts.ZaloTextStyleName): đậm, nghiêng, gạch
 * chân, gạch ngang, đỏ, cam, vàng, xanh lá, chữ nhỏ, chữ to. Không dùng lst_*
 * và ind_$ (gạch đầu dòng vẫn là ký tự "• " trong chữ).
 */
export const ZALO_TEXT_STYLES = [
  "b",
  "i",
  "u",
  "s",
  "c_db342e",
  "c_f27806",
  "c_f7b503",
  "c_15a85f",
  "f_13",
  "f_18",
] as const;
export type ZaloTextStyleName = (typeof ZALO_TEXT_STYLES)[number];

/** 1 style zca-js: start/len theo đơn vị UTF-16 (String.length) trong chính tin đó. */
export type ZaloTextStyle = { start: number; len: number; st: ZaloTextStyleName };

/** Tham số sendSeenEvent (zca-js dist/apis/sendSeenEvent.d.ts SendSeenEventMessageParams). */
export type ZaloSeenParams = {
  msgId: string;
  cliMsgId: string;
  uidFrom: string;
  idTo: string;
  msgType: string;
  st: number;
  at: number;
  cmd: number;
  ts: string | number;
};

/** Đích của addReaction (zca-js dist/apis/addReaction.d.ts AddReactionDestination). */
export type ZaloReactionTarget = {
  data: { msgId: string; cliMsgId: string };
  threadId: string;
  type: number;
};

/** DestType.User của zca-js (dist/models/Enum.d.ts) - đích sendTypingEvent là người dùng. */
export const DEST_TYPE_USER = 3;
/**
 * Reactions.OK ("/-ok", 👌) của zca-js (dist/models/Reaction.d.ts): thả vào tin
 * đang phải chờ sau lượt trước của cùng người gửi - "em thấy rồi, đợi em xíu".
 */
export const REACTION_QUEUED = "/-ok";

export type ZaloSendContent = {
  msg: string;
  styles?: ZaloTextStyle[];
  attachments?: ZaloAttachment[];
};

export type ZaloApi = {
  listener: ZaloListener;
  getOwnId(): string | number;
  getContext(): ZaloContext;
  sendMessage(message: ZaloSendContent, threadId: string, type: number): Promise<unknown>;
  /** sendTypingEvent(threadId, type = ThreadType.User, destType = DestType.User). */
  sendTypingEvent(threadId: string, type?: number, destType?: number): Promise<unknown>;
  /** Tối đa 50 tin, cùng 1 thread. */
  sendSeenEvent(messages: ZaloSeenParams | ZaloSeenParams[], type?: number): Promise<unknown>;
  addReaction(
    icon: string | { rType: number; source: number; icon: string },
    dest: ZaloReactionTarget,
  ): Promise<unknown>;
  getAllGroups(): Promise<{ gridVerMap?: Record<string, unknown> }>;
  getGroupInfo(groupId: string): Promise<{
    changed_groups?: Record<string, { name?: string }>;
    gridInfoMap?: Record<string, { name?: string }>;
  }>;
};

export type MediaItem = { mime: string; bytes: Uint8Array };
