import { readLimitedBody } from "./limits.js";
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

/** Thử lần lượt các URL, trả về ảnh hợp lệ đầu tiên (≤ 8 MB, đúng định dạng). */
export async function downloadImage(urls: string[], options: DownloadOptions): Promise<MediaItem> {
  if (!urls.length) throw new Error("Payload ảnh không có URL media được hỗ trợ");
  const doFetch = options.fetchImpl ?? fetch;
  const reasons: string[] = [];
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
      if (declared > MAX_IMAGE_BYTES) throw new Error("Ảnh lớn hơn 8 MB");
      const bytes = await readLimitedBody(response, MAX_IMAGE_BYTES);
      if (!bytes.length || bytes.length > MAX_IMAGE_BYTES) {
        throw new Error("Ảnh trống hoặc lớn hơn 8 MB");
      }
      const headerMime = (response.headers.get("content-type") || "")
        .split(";", 1)[0]
        .toLowerCase();
      const mime = sniffImage(bytes) || (ALLOWED_MIME.includes(headerMime) ? headerMime : null);
      if (!mime) {
        reasons.push(`${host}: MIME ${headerMime || "không rõ"}`);
        continue;
      }
      return { bytes, mime };
    } catch (error) {
      reasons.push(`${host}: ${error instanceof Error ? error.message : "lỗi tải"}`);
    }
  }
  throw new Error(`Không tải được ảnh từ Zalo (${reasons.slice(0, 3).join("; ")})`);
}
