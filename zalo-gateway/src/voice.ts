/**
 * Tin thoại trả lời /noi (Python services/voice_reply.py, channels/tts_media.py).
 *
 * Python trả `voice: {path, url, duration_ms, mime}` kèm câu trả lời chữ; gateway gửi
 * SAU các tin chữ:
 * 1. Tải clip M4A qua loopback `${bridgeBaseUrl}/${path}` (bridge secret, trần byte).
 * 2. Tải lên CDN Zalo (zca-js uploadAttachment, file .m4a) rồi sendVoice bằng link
 *    CDN - link bền, người nhận mở muộn vẫn nghe được.
 * 3. Tải/tải lên lỗi mà có `url` công khai (link ký HMAC, sống
 *    VOICE_REPLY_URL_TTL_MIN phút): sendVoice thẳng link đó (zca-js HEAD lấy
 *    Content-Length, app Zalo tự tải file).
 * Mọi bước có timeout ngắn; lỗi chỉ log - tin chữ đã gửi rồi, không bao giờ ném.
 */
import { readLimitedBody } from "./limits.js";

export type BridgeVoice = {
  path: string;
  url?: string | null;
  duration_ms?: number;
  mime?: string;
};

/** Phần zca-js gateway dùng cho tin thoại (dist/apis/uploadAttachment, sendVoice). */
export type VoiceClient = {
  uploadAttachment?(
    sources: { data: Buffer; filename: string; metadata: { totalSize: number } }[],
    threadId: string,
    type?: number,
  ): Promise<{ fileUrl?: string; fileType?: string }[]>;
  sendVoice?(
    options: { voiceUrl: string; ttl?: number },
    threadId: string,
    type?: number,
  ): Promise<unknown>;
};

export type VoiceDeps = {
  bridgeBaseUrl: string;
  bridgeSecret: string;
  threadType: number;
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
  log?: (message: string) => void;
};

export const VOICE_MAX_BYTES = 8 * 1024 * 1024;
const DEFAULT_TIMEOUT_MS = 20000;
const PATH_RE = /^tts\/[A-Za-z0-9_.-]{1,200}$/;

/** Python trả `voice` hợp lệ không (đúng dạng path tts/<tên>; url chỉ https). */
export function parseVoice(value: unknown): BridgeVoice | null {
  if (!value || typeof value !== "object") return null;
  const raw = value as Record<string, unknown>;
  const path = typeof raw.path === "string" ? raw.path : "";
  if (!PATH_RE.test(path)) return null;
  const url = typeof raw.url === "string" && raw.url.startsWith("https://") ? raw.url : null;
  const duration = typeof raw.duration_ms === "number" ? raw.duration_ms : 0;
  return { path, url, duration_ms: duration, mime: typeof raw.mime === "string" ? raw.mime : "" };
}

function timed<T>(promise: Promise<T>, ms: number, label: string): Promise<T> {
  let timer: NodeJS.Timeout | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label}: quá ${ms}ms`)), ms);
    timer.unref?.();
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** Tải clip qua loopback (route /internal/zalo/tts/<tên> của Python). */
export async function fetchVoiceClip(voice: BridgeVoice, deps: VoiceDeps): Promise<Buffer> {
  const fetcher = deps.fetchImpl ?? fetch;
  const response = await fetcher(`${deps.bridgeBaseUrl}/${voice.path}`, {
    headers: { "x-zalo-bridge-secret": deps.bridgeSecret },
    signal: AbortSignal.timeout(deps.timeoutMs ?? DEFAULT_TIMEOUT_MS),
  });
  if (!response.ok) throw new Error(`tải tin thoại trả về ${response.status}`);
  const body = await readLimitedBody(response, VOICE_MAX_BYTES);
  if (!body.length) throw new Error("tin thoại rỗng");
  return Buffer.from(body);
}

/**
 * Gửi 1 tin thoại tới `target`. Trả "cdn" | "url" | null (không gửi được). Không ném.
 */
export async function sendVoiceReply(
  client: VoiceClient,
  target: string,
  voice: BridgeVoice,
  deps: VoiceDeps,
): Promise<"cdn" | "url" | null> {
  const log = deps.log ?? ((text: string) => console.warn(text));
  const timeoutMs = deps.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  if (typeof client.sendVoice !== "function") {
    log("[zalo] zca-js không có sendVoice - bỏ tin thoại trả lời.");
    return null;
  }
  if (typeof client.uploadAttachment === "function") {
    try {
      const data = await fetchVoiceClip(voice, deps);
      const uploaded = await timed(
        client.uploadAttachment(
          [{ data, filename: `lan-anh-${Date.now()}.m4a`, metadata: { totalSize: data.length } }],
          target,
          deps.threadType,
        ),
        timeoutMs,
        "tải tin thoại lên Zalo",
      );
      const fileUrl = uploaded?.[0]?.fileUrl;
      if (!fileUrl) throw new Error("Zalo không trả link file");
      await timed(
        client.sendVoice({ voiceUrl: fileUrl, ttl: 0 }, target, deps.threadType),
        timeoutMs,
        "sendVoice",
      );
      return "cdn";
    } catch (error) {
      log(`[zalo] gửi tin thoại qua CDN lỗi: ${message(error)}`);
    }
  }
  if (!voice.url) return null;
  try {
    await timed(
      client.sendVoice({ voiceUrl: voice.url, ttl: 0 }, target, deps.threadType),
      timeoutMs,
      "sendVoice",
    );
    return "url";
  } catch (error) {
    log(`[zalo] gửi tin thoại bằng link công khai lỗi: ${message(error)}`);
    return null;
  }
}
