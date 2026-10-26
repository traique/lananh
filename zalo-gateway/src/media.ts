import { BodyTooLargeError, readLimitedBody } from "./limits.js";
import type { MediaItem } from "./types.js";

export const MAX_IMAGE_BYTES = 8 * 1024 * 1024;

const ALLOWED_MEDIA_DOMAINS = [
  "zalo.me",
  "zaloapp.com",
  "zadn.vn",
  "zdn.vn",
  "zalo.cloud",
  "znews.vn",
];
const PREFERRED_URL_KEYS = ["hdUrl", "originUrl", "href", "url", "thumb", "thumbUrl", "params"];
const ALLOWED_MIME = ["image/jpeg", "image/png", "image/webp"];

/** Chỉ tải media từ domain Zalo (chặn SSRF qua payload tin nhắn). */
export function allowedMediaHost(host: string): boolean {
  const h = host.toLowerCase();
  return ALLOWED_MEDIA_DOMAINS.some((domain) => h === domain || h.endsWith(`.${domain}`));
}

/** Tìm URL ảnh (thuộc domain Zalo) trong content tin nhắn, kể cả JSON lồng trong chuỗi. */
export function imageCandidates(content: unknown): string[] {
  const found: string[] = [];
  const visit = (value: unknown, depth: number): void => {
    if (depth > 4 || value == null) return;
    if (typeof value === "string") {
      if (/^https?:\/\//i.test(value)) {
        try {
          if (allowedMediaHost(new URL(value).hostname)) found.push(value);
        } catch {
          // URL hỏng: bỏ qua.
        }
      } else if ((value.startsWith("{") || value.startsWith("[")) && value.length < 20000) {
        try {
          visit(JSON.parse(value), depth + 1);
        } catch {
          // Không phải JSON: bỏ qua.
        }
      }
      return;
    }
    if (Array.isArray(value)) {
      for (const item of value) visit(item, depth + 1);
      return;
    }
    if (typeof value === "object") {
      const record = value as Record<string, unknown>;
      for (const key of [...PREFERRED_URL_KEYS, ...Object.keys(record)]) {
        if (key in record) visit(record[key], depth + 1);
      }
    }
  };
  visit(content, 0);
  return [...new Set(found)];
}

/**
 * Link http(s) của tin dạng "chia sẻ link" (msgType chat.recommended), rỗng nếu
 * không phải. Tin link có thumb trên CDN Zalo nên không được coi là tin ảnh.
 */
export function linkHref(msgType: unknown, content: unknown): string {
  if (typeof content !== "object" || content === null) return "";
  const record = content as Record<string, unknown>;
  const isLink = msgType === "chat.recommended" || record.action === "recommened.link";
  const href = typeof record.href === "string" ? record.href.trim() : "";
  return isLink && /^https?:\/\//i.test(href) ? href : "";
}

/** Nhận diện định dạng ảnh qua magic bytes (không tin Content-Type). */
export function sniffImage(bytes: Uint8Array): string | null {
  if (bytes.length >= 3 && bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff) {
    return "image/jpeg";
  }
  if (
    bytes.length >= 8 &&
    bytes[0] === 0x89 &&
    bytes[1] === 0x50 &&
    bytes[2] === 0x4e &&
    bytes[3] === 0x47
  ) {
    return "image/png";
  }
  if (
    bytes.length >= 12 &&
    String.fromCharCode(...bytes.slice(0, 4)) === "RIFF" &&
    String.fromCharCode(...bytes.slice(8, 12)) === "WEBP"
  ) {
    return "image/webp";
  }
  return null;
}

export type DownloadOptions = {
  userAgent: string;
  cookieFor?: (url: string) => string;
  fetchImpl?: typeof fetch;
};

/** Lỗi tải media; `tooLarge` = có URL bị loại vì vượt trần dung lượng. */
export class MediaDownloadError extends Error {
  constructor(
    message: string,
    readonly tooLarge = false,
  ) {
    super(message);
    this.name = "MediaDownloadError";
  }
}

class TooLargeError extends Error {}

type MediaSpec = {
  /** Tên loại media trong câu lỗi ("ảnh", "ghi âm", "file"). */
  label: string;
  maxBytes: number;
  /** MIME hợp lệ theo nội dung (magic bytes) hoặc null để thử URL kế. */
  detect(bytes: Uint8Array, headerMime: string): string | null;
};

const capitalize = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);
const mbText = (bytes: number) => `${Math.round((bytes / 1024 / 1024) * 10) / 10} MB`;

/**
 * Thử lần lượt các URL (chỉ domain Zalo, kiểm lại domain sau redirect), đọc tối
 * đa `maxBytes` (không tin Content-Length), trả media hợp lệ đầu tiên theo
 * `detect`. Không URL nào được -> MediaDownloadError tóm tắt tối đa 3 lý do.
 */
async function downloadMedia(
  urls: string[],
  options: DownloadOptions,
  spec: MediaSpec,
): Promise<MediaItem> {
  if (!urls.length) {
    throw new MediaDownloadError(`Payload ${spec.label} không có URL media được hỗ trợ`);
  }
  const doFetch = options.fetchImpl ?? fetch;
  const reasons: string[] = [];
  let tooLarge = false;
  const limitText = `${capitalize(spec.label)} lớn hơn ${mbText(spec.maxBytes)}`;
  for (const raw of urls) {
    let host = "unknown";
    try {
      host = new URL(raw).hostname;
      if (!allowedMediaHost(host)) {
        reasons.push(`${host}: domain bị chặn`);
        continue;
      }
      const cookie = options.cookieFor?.(raw) || "";
      const response = await doFetch(raw, {
        redirect: "follow",
        headers: {
          "user-agent": options.userAgent,
          referer: "https://chat.zalo.me/",
          ...(cookie ? { cookie } : {}),
        },
        signal: AbortSignal.timeout(30000),
      });
      if (!allowedMediaHost(new URL(response.url || raw).hostname)) {
        reasons.push(`${host}: redirect ngoài Zalo`);
        continue;
      }
      if (!response.ok) {
        reasons.push(`${host}: HTTP ${response.status}`);
        continue;
      }
      const declared = Number(response.headers.get("content-length") || 0);
      if (declared > spec.maxBytes) throw new TooLargeError(limitText);
      let bytes: Uint8Array;
      try {
        bytes = await readLimitedBody(response, spec.maxBytes);
      } catch (error) {
        if (error instanceof BodyTooLargeError) {
          throw new TooLargeError(limitText);
        }
        throw error;
      }
      if (!bytes.length)
        throw new Error(`${capitalize(spec.label)} trống hoặc lớn hơn ${mbText(spec.maxBytes)}`);
      const headerMime = (response.headers.get("content-type") || "")
        .split(";", 1)[0]
        .toLowerCase();
      const mime = spec.detect(bytes, headerMime);
      if (!mime) {
        reasons.push(`${host}: MIME ${headerMime || "không rõ"}`);
        continue;
      }
      return { bytes, mime };
    } catch (error) {
      if (error instanceof TooLargeError) tooLarge = true;
      reasons.push(`${host}: ${error instanceof Error ? error.message : "lỗi tải"}`);
    }
  }
  throw new MediaDownloadError(
    `Không tải được ${spec.label} từ Zalo (${reasons.slice(0, 3).join("; ")})`,
    tooLarge,
  );
}

/** Thử lần lượt các URL, trả về ảnh hợp lệ đầu tiên (≤ 8 MB, đúng định dạng). */
export async function downloadImage(urls: string[], options: DownloadOptions): Promise<MediaItem> {
  return downloadMedia(urls, options, {
    label: "ảnh",
    maxBytes: MAX_IMAGE_BYTES,
    detect: (bytes, headerMime) =>
      sniffImage(bytes) || (ALLOWED_MIME.includes(headerMime) ? headerMime : null),
  });
}

/**
 * Nhận diện file ghi âm qua magic bytes - cùng tập services/voice_service.sniff
 * nhận (m4a/mp4 "ftyp", Ogg, MP3/ID3, WAV, FLAC, WebM, AAC ADTS). Trang lỗi HTML
 * hay AMR (Groq lẫn Gemini đều không nhận) -> null.
 */
export function sniffAudio(bytes: Uint8Array): string | null {
  const ascii = (from: number, to: number) => String.fromCharCode(...bytes.slice(from, to));
  if (bytes.length >= 4 && ascii(0, 4) === "OggS") return "audio/ogg";
  if (bytes.length >= 12 && ascii(4, 8) === "ftyp") return "audio/mp4";
  if (bytes.length >= 3 && ascii(0, 3) === "ID3") return "audio/mpeg";
  if (bytes.length >= 12 && ascii(0, 4) === "RIFF" && ascii(8, 12) === "WAVE") return "audio/wav";
  if (bytes.length >= 4 && ascii(0, 4) === "fLaC") return "audio/flac";
  if (
    bytes.length >= 4 &&
    bytes[0] === 0x1a &&
    bytes[1] === 0x45 &&
    bytes[2] === 0xdf &&
    bytes[3] === 0xa3
  ) {
    return "audio/webm";
  }
  if (bytes.length >= 2 && bytes[0] === 0xff) {
    // ADTS (AAC): 12 bit đồng bộ 0xFFF + 2 bit layer luôn 00; MP3: 11 bit đồng bộ + layer khác 00.
    if ((bytes[1] & 0xf6) === 0xf0) return "audio/aac";
    if ((bytes[1] & 0xe0) === 0xe0 && bytes[1] & 0x06) return "audio/mpeg";
  }
  return null;
}

/** Tải tin thoại: URL đầu tiên là file âm thanh thật (magic bytes) và ≤ `maxBytes`. */
export async function downloadAudio(
  urls: string[],
  options: DownloadOptions,
  maxBytes: number,
): Promise<MediaItem> {
  return downloadMedia(urls, options, {
    label: "ghi âm",
    maxBytes,
    detect: (bytes) => sniffAudio(bytes),
  });
}

export const DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
const TEXT_EXTS = ["txt", "md", "markdown", "csv", "log"];

/**
 * MIME tài liệu theo đuôi + magic bytes, khớp services/document_service.detect_kind:
 * PDF "%PDF-", Word .docx là zip "PK\x03\x04", chữ thuần không có byte NUL ở
 * đầu file (trừ khi có BOM UTF-16). Không khớp -> null.
 */
export function sniffDocument(bytes: Uint8Array, ext: string): string | null {
  const head = bytes.slice(0, 8192);
  const ascii = (to: number) => String.fromCharCode(...head.slice(0, to));
  if (ext === "pdf") return ascii(5) === "%PDF-" ? "application/pdf" : null;
  if (ext === "docx") {
    return head[0] === 0x50 && head[1] === 0x4b && head[2] === 0x03 && head[3] === 0x04
      ? DOCX_MIME
      : null;
  }
  if (TEXT_EXTS.includes(ext)) {
    const utf16 = (head[0] === 0xff && head[1] === 0xfe) || (head[0] === 0xfe && head[1] === 0xff);
    if (!utf16 && head.includes(0)) return null;
    return ext === "md" || ext === "markdown" ? "text/markdown" : "text/plain";
  }
  return null;
}

/** Tải file tài liệu (PDF/DOCX/TXT) ≤ `maxBytes`, kiểm nội dung khớp đuôi. */
export async function downloadDocument(
  urls: string[],
  ext: string,
  options: DownloadOptions,
  maxBytes: number,
): Promise<MediaItem> {
  return downloadMedia(urls, options, {
    label: "file",
    maxBytes,
    detect: (bytes) => sniffDocument(bytes, ext),
  });
}
