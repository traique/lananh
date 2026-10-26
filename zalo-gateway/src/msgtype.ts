import { isFastLaneCommand } from "./commands.js";
import { allowedMediaHost, imageCandidates, linkHref } from "./media.js";
import type { ZaloMessage, ZaloQuote, ZaloSeenParams } from "./types.js";

/**
 * Loại tin riêng theo data.msgType của zca-js (dist/utils.js getClientMessageType):
 * - text: chữ (webchat), hoặc loại object lạ - chỉ lấy title làm chữ, KHÔNG coi là ảnh;
 * - link: chia sẻ link (chat.recommended);
 * - image: chat.photo, chat.doodle (ảnh vẽ), share.file đuôi ảnh;
 * - voice: chat.voice -> POST /internal/zalo/voice;
 * - file: share.file PDF/Word/TXT -> POST /internal/zalo/file;
 * - video / location / file_unsupported: 1 câu "chưa hỗ trợ", không vào đường ảnh;
 * - silent: sticker và ảnh động (chat.gif - thường là biểu cảm, chỉ có thumb tĩnh,
 *   viết prompt cho nó chỉ tốn lượt AI) - im lặng như trước.
 */
export type DirectKind =
  | "text"
  | "link"
  | "image"
  | "voice"
  | "file"
  | "video"
  | "location"
  | "file_unsupported"
  | "silent";

/** Đuôi file gửi dạng share.file vẫn đi đường ảnh (khớp ALLOWED_MIME của media.ts). */
export const IMAGE_FILE_EXTS = ["jpg", "jpeg", "png", "webp"];
/** Đuôi file Python đọc được (services/document_service.py: PDF, DOCX, TXT/MD/CSV/LOG). */
export const DOCUMENT_FILE_EXTS = ["pdf", "docx", "txt", "md", "markdown", "csv", "log"];
/** Trần tin được trích dẫn gửi sang Python (channels/contracts.QUOTE_TEXT_MAX_CHARS). */
export const QUOTE_MAX_CHARS = 600;

const AUDIO_EXT = /\.(m4a|aac|mp3|ogg|oga|opus|wav|webm|mp4|amr|3gp)$/i;
const IMAGE_EXT = /\.(jpe?g|png|webp|gif|bmp|heic)$/i;

export type FileInfo = {
  /** Tên file người gửi thấy (title của share.file). */
  name: string;
  /** Đuôi viết thường, không dấu chấm ("" nếu không rõ). */
  ext: string;
  /** Dung lượng Zalo khai báo trong params (byte), null nếu không có. */
  size: number | null;
};

export type ParsedMessage = {
  text: string;
  caption: string;
  /** URL media (domain Zalo) như trước - nhóm/Facebook dùng nguyên danh sách này. */
  urls: string[];
  /** Link của tin "chia sẻ link" (rỗng nếu không phải). */
  link: string;
  sender: string;
  senderName: string;
  id: string;
  threadId: string;
  msgType: string;
  kind: DirectKind;
  /** Chữ tin được trích dẫn (zca-js data.quote), tối đa QUOTE_MAX_CHARS ký tự. */
  quoteText: string;
  /** Tham số đầy đủ cho sendSeenEvent/addReaction; null nếu thiếu id. */
  seen: ZaloSeenParams | null;
  /** chat.voice: URL ghi âm theo thứ tự ưu tiên (m4a trước). */
  voiceUrls: string[];
  /** chat.voice: thời lượng Zalo khai báo (ms), null nếu không có. */
  durationMs: number | null;
  /** share.file: tên/đuôi/dung lượng. */
  file: FileInfo | null;
  /** share.file tài liệu: URL tải file (href trước, bỏ thumb/icon). */
  fileUrls: string[];
};

type Record_ = Record<string, unknown>;

function asRecord(value: unknown): Record_ | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record_)
    : null;
}

/** params của tin đính kèm là chuỗi JSON (đôi khi lồng thêm 1 lớp chuỗi). */
function parseParams(value: unknown): Record_ {
  let current: unknown = value;
  for (let depth = 0; depth < 2 && typeof current === "string"; depth++) {
    const raw = current.trim();
    if (!raw.startsWith("{") || raw.length > 20000) return {};
    try {
      current = JSON.parse(raw);
    } catch {
      return {};
    }
  }
  return asRecord(current) ?? {};
}

function extOf(name: string): string {
  const match = /\.([a-z0-9]{1,10})$/i.exec(name.trim());
  return match ? match[1].toLowerCase() : "";
}

function positiveInt(value: unknown): number | null {
  const n = typeof value === "string" ? Number(value.trim()) : value;
  return typeof n === "number" && Number.isFinite(n) && n > 0 ? Math.floor(n) : null;
}

/** Tên, đuôi, dung lượng của tin share.file. */
export function fileInfo(content: unknown): FileInfo {
  const record = asRecord(content) ?? {};
  const params = parseParams(record.params);
  const name = String(record.title || "").trim();
  const declaredExt = typeof params.fileExt === "string" ? params.fileExt : "";
  const ext = (declaredExt.replace(/^\./, "").trim() || extOf(name)).toLowerCase();
  return { name, ext, size: positiveInt(params.fileSize) };
}

/** Phân loại tin riêng theo msgType (xem DirectKind). */
export function classifyDirect(msgType: unknown, content: unknown): DirectKind {
  if (typeof content === "string") return "text";
  if (asRecord(content) === null) return "text";
  if (linkHref(msgType, content)) return "link";
  switch (msgType) {
    case "chat.photo":
    case "chat.doodle":
      return "image";
    case "chat.sticker":
    case "chat.gif":
      return "silent";
    case "chat.voice":
      return "voice";
    case "chat.video.msg":
      return "video";
    case "chat.location.new":
      return "location";
    case "share.file": {
      const { ext } = fileInfo(content);
      if (IMAGE_FILE_EXTS.includes(ext)) return "image";
      if (DOCUMENT_FILE_EXTS.includes(ext)) return "file";
      return "file_unsupported";
    }
    default:
      return "text";
  }
}

/**
 * Mọi URL thuộc domain Zalo trong content kèm tên khoá chứa nó (đọc cả JSON lồng
 * trong chuỗi như params). Dùng để chọn đúng file ghi âm thay vì thumb.
 */
function keyedMediaUrls(content: unknown): { key: string; url: string }[] {
  const found: { key: string; url: string }[] = [];
  const visit = (value: unknown, key: string, depth: number): void => {
    if (depth > 4 || value == null) return;
    if (typeof value === "string") {
      if (/^https?:\/\//i.test(value)) {
        try {
          if (allowedMediaHost(new URL(value).hostname)) found.push({ key, url: value });
        } catch {
          // URL hỏng: bỏ qua.
        }
      } else if ((value.startsWith("{") || value.startsWith("[")) && value.length < 20000) {
        try {
          visit(JSON.parse(value), key, depth + 1);
        } catch {
          // Không phải JSON: bỏ qua.
        }
      }
      return;
    }
    if (Array.isArray(value)) {
      for (const item of value) visit(item, key, depth + 1);
      return;
    }
    const record = asRecord(value);
    if (record) for (const [k, v] of Object.entries(record)) visit(v, k, depth + 1);
  };
  visit(content, "", 0);
  return found;
}

/**
 * URL ghi âm của chat.voice theo thứ tự thử: bản m4a trước (zca-js sendVoice gửi
 * khoá m4aUrl; Groq nhận m4a nhưng không nhận AAC thô), rồi URL có đuôi âm thanh,
 * rồi href. Bỏ URL thumb/ảnh.
 */
export function voiceCandidates(content: unknown): string[] {
  const scored: { url: string; score: number; order: number }[] = [];
  keyedMediaUrls(content).forEach(({ key, url }, order) => {
    let path = "";
    try {
      path = new URL(url).pathname;
    } catch {
      return;
    }
    if (/thumb/i.test(key) || IMAGE_EXT.test(path)) return;
    let score = 3;
    if (/m4a/i.test(key) || /\.m4a$/i.test(path)) score = 0;
    else if (AUDIO_EXT.test(path)) score = 1;
    else if (key === "href" || key === "url") score = 2;
    scored.push({ url, score, order });
  });
  scored.sort((a, b) => a.score - b.score || a.order - b.order);
  return [...new Set(scored.map((item) => item.url))];
}

/** URL tải file của share.file: href/url trước, bỏ thumb và URL ảnh xem trước. */
export function fileCandidates(content: unknown): string[] {
  const scored: { url: string; score: number; order: number }[] = [];
  keyedMediaUrls(content).forEach(({ key, url }, order) => {
    if (/thumb/i.test(key)) return;
    scored.push({ url, score: key === "href" || key === "url" ? 0 : 1, order });
  });
  scored.sort((a, b) => a.score - b.score || a.order - b.order);
  return [...new Set(scored.map((item) => item.url))];
}

/** Thời lượng tin thoại Zalo khai báo trong params ("duration", ms). */
export function voiceDurationMs(content: unknown): number | null {
  const record = asRecord(content);
  if (!record) return null;
  return positiveInt(parseParams(record.params).duration);
}

/**
 * Cắt theo ký tự (code point), không theo đơn vị UTF-16: không bao giờ để lại nửa
 * cặp surrogate (emoji) - encodeURIComponent ném URIError với nửa cặp đó. Khớp
 * cách Python cắt str ([:n]).
 */
export function clipChars(text: string, max: number): string {
  const chars = Array.from(text);
  return chars.length > max ? chars.slice(0, max).join("") : text;
}

/**
 * Chữ của tin được trích dẫn: quote.msg; tin gốc là ảnh/file (msg rỗng) thì lấy
 * title trong attach nếu có. Tối đa QUOTE_MAX_CHARS ký tự.
 */
export function quoteText(quote: ZaloQuote | undefined | null): string {
  if (!quote || typeof quote !== "object") return "";
  let text = typeof quote.msg === "string" ? quote.msg.trim() : "";
  if (!text) text = String(parseParams(quote.attach).title || "").trim();
  return clipChars(text, QUOTE_MAX_CHARS);
}

/** Tham số đầy đủ cho sendSeenEvent (và msgId/cliMsgId cho addReaction). */
export function seenParams(data: ZaloMessage["data"]): ZaloSeenParams | null {
  if (!data) return null;
  const msgId = String(data.msgId ?? "");
  const cliMsgId = String(data.cliMsgId ?? "");
  if (!msgId || !cliMsgId) return null;
  return {
    msgId,
    cliMsgId,
    uidFrom: String(data.uidFrom ?? ""),
    idTo: String(data.idTo ?? ""),
    msgType: String(data.msgType ?? ""),
    st: Number(data.st ?? 0) || 0,
    at: Number(data.at ?? 0) || 0,
    cmd: Number(data.cmd ?? 0) || 0,
    ts: data.ts ?? 0,
  };
}

export function parseMessage(message: ZaloMessage): ParsedMessage {
  const data = message.data;
  const content = data?.content as string | Record_ | undefined;
  const record = asRecord(content);
  const msgType = String(data?.msgType || "");
  const kind = classifyDirect(msgType, content);
  return {
    text: typeof content === "string" ? content.trim() : String(record?.title || "").trim(),
    // share.file: title là TÊN FILE, không phải chú thích người gửi gõ kèm.
    caption:
      record && msgType !== "share.file"
        ? String(record.description || record.title || "").trim()
        : "",
    urls: record ? imageCandidates(content) : [],
    link: linkHref(data?.msgType, content),
    sender: String(data?.uidFrom || ""),
    senderName: String(data?.dName || ""),
    id: String(data?.msgId || data?.cliMsgId || ""),
    threadId: String(message.threadId),
    msgType,
    kind,
    quoteText: quoteText(data?.quote),
    seen: seenParams(data),
    voiceUrls: kind === "voice" ? voiceCandidates(content) : [],
    durationMs: kind === "voice" ? voiceDurationMs(content) : null,
    file: kind === "file" || kind === "file_unsupported" ? fileInfo(content) : null,
    fileUrls: kind === "file" ? fileCandidates(content) : [],
  };
}

/** Tin chữ thường gộp được (không lệnh "/", không link, không media). */
export function isPlainText(msg: ParsedMessage): boolean {
  return (
    msg.kind === "text" && !!msg.text && !msg.text.startsWith("/") && !/https?:\/\//i.test(msg.text)
  );
}

/**
 * Đường đi của 1 tin riêng:
 * - drop: sticker, ảnh động, tin rỗng - im lặng;
 * - notice: video, vị trí, file lạ - 1 câu "chưa hỗ trợ" (Python dựng câu, người lạ im lặng);
 * - fast_lane: /huy và lệnh chỉ đọc - gửi Python ngoài hàng đợi theo người gửi;
 * - batch: tin chữ thường - debounce + gộp với tin chữ liền sau;
 * - queue: còn lại (lệnh, link, ảnh, ghi âm, file) - 1 việc trong hàng đợi theo người gửi.
 */
export type DirectRoute = "drop" | "notice" | "fast_lane" | "batch" | "queue";

export function directRoute(msg: ParsedMessage): DirectRoute {
  switch (msg.kind) {
    case "silent":
      return "drop";
    case "video":
    case "location":
    case "file_unsupported":
      return "notice";
    case "image":
      if (!msg.text && !msg.urls.length) return "drop";
      break;
    case "text":
      if (!msg.text) return "drop";
      if (isFastLaneCommand(msg.text)) return "fast_lane";
      break;
  }
  return isPlainText(msg) ? "batch" : "queue";
}
