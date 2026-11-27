/**
 * Danh sách nhóm tài khoản bot đang tham gia cho control server GET /groups
 * (Python dựng lệnh /nhomzalo cho Telegram, Zoom và Zalo từ đây).
 */

import { withTimeout } from "./feedback.js";

export type GroupSummary = { id: string; name: string; members: number | null };
export type GroupList = { total: number; groups: GroupSummary[] };

type GroupDetail = { name?: string; totalMember?: number };

/** Phần zca-js dùng ở đây: getGroupInfo nhận 1 id hoặc mảng id (zca-js 2.1.2). */
export type GroupApi = {
  getAllGroups(): Promise<{ gridVerMap?: Record<string, unknown> }>;
  getGroupInfo(groupId: string | string[]): Promise<{
    changed_groups?: Record<string, GroupDetail>;
    gridInfoMap?: Record<string, GroupDetail>;
  }>;
};

/** Trần số nhóm trả về: danh sách trong chat vẫn đọc được, Python lọc theo tên khi nhiều. */
export const MAX_GROUPS = 200;
/** Số id mỗi lần getGroupInfo: phản hồi kèm memVerList (mọi thành viên) nên lô nhỏ giữ heap thấp. */
export const INFO_BATCH = 20;
/** Mỗi người gọi GET /groups chờ tối đa chừng này (Python chờ 30s). */
export const SCAN_TIMEOUT_MS = 25_000;

/**
 * getAllGroups rồi getGroupInfo theo lô (thay vì 1 lệnh gọi/nhóm). Lô lỗi vẫn trả id,
 * tên để trống - người dùng vẫn thấy group ID. ``total`` là số nhóm thật, kể cả phần
 * vượt ``maxGroups``.
 */
export async function collectGroups(
  api: GroupApi,
  maxGroups = MAX_GROUPS,
  batchSize = INFO_BATCH,
): Promise<GroupList> {
  const all = await api.getAllGroups();
  const ids = Object.keys(all?.gridVerMap || {});
  const wanted = ids.slice(0, maxGroups);
  const groups: GroupSummary[] = [];
  for (let start = 0; start < wanted.length; start += batchSize) {
    const batch = wanted.slice(start, start + batchSize);
    let info: Awaited<ReturnType<GroupApi["getGroupInfo"]>> | null = null;
    try {
      info = await api.getGroupInfo(batch);
    } catch (error) {
      console.warn(`[zalo] getGroupInfo lỗi cho ${batch.length} nhóm`, error);
    }
    for (const id of batch) {
      const detail = info?.gridInfoMap?.[id] || info?.changed_groups?.[id];
      const members = detail?.totalMember;
      groups.push({
        id,
        name: (detail?.name || "").trim(),
        members: typeof members === "number" && Number.isFinite(members) ? members : null,
      });
    }
  }
  return { total: ids.length, groups };
}

export type TrackedGroupsDeps = {
  /** Nhóm /tongket + tín hiệu cổ phiếu (bridge.fetchAllowedGroups). */
  fetchSummary(): Promise<Set<string>>;
  /** Nhóm nguồn Facebook (bridge.fetchFacebookGroups). */
  fetchFacebook(): Promise<Set<string>>;
  /** Zalo còn đăng nhập không (chưa đăng nhập thì không nạp). */
  connected(): boolean;
  /** Sau khi nạp xong nhóm /tongket (vd bỏ cache vai trò của nhóm đã gỡ). */
  onSummary?(groupIds: Set<string>): void;
  log?(message: string, error?: unknown): void;
};

/**
 * Nhóm Zalo đang theo dõi theo Python. Tin của nhóm KHÁC bị bỏ ngay ở cửa vào (không
 * parse, không ghi SeenIds). Nạp lỗi thì giữ danh sách cũ. RAM: 2 Set id nhóm.
 */
export class TrackedGroups {
  private summary = new Set<string>();
  private facebook = new Set<string>();

  constructor(private readonly deps: TrackedGroupsDeps) {}

  isSummary(groupId: string): boolean {
    return this.summary.has(groupId);
  }

  isFacebook(groupId: string): boolean {
    return this.facebook.has(groupId);
  }

  /** Có cần xử lý tin của nhóm này không (/tongket, tín hiệu hoặc nguồn Facebook). */
  isTracked(groupId: string): boolean {
    return this.summary.has(groupId) || this.facebook.has(groupId);
  }

  async refresh(): Promise<void> {
    if (!this.deps.connected()) return;
    const log = this.deps.log ?? console.error;
    try {
      this.summary = await this.deps.fetchSummary();
      this.deps.onSummary?.(this.summary);
    } catch (error) {
      log("[zalo] summary group refresh failed", error);
    }
    try {
      this.facebook = await this.deps.fetchFacebook();
    } catch (error) {
      log("[zalo] Facebook group refresh failed", error);
    }
  }
}

/**
 * Chỉ 1 lượt quét tại 1 thời điểm: gõ lại sau khi hết giờ chờ hay nhiều admin cùng gõ
 * đều dùng chung lượt đang chạy, không chồng nhiều loạt gọi Zalo (rate limit, heap).
 * Mỗi người gọi chờ tối đa ``timeoutMs``; lượt quét chỉ được nhả khi CHÍNH nó xong.
 */
export function sharedScan(
  scan: () => Promise<GroupList>,
  timeoutMs = SCAN_TIMEOUT_MS,
): () => Promise<GroupList> {
  let inflight: Promise<GroupList> | null = null;
  return () => {
    if (!inflight) {
      inflight = scan().finally(() => {
        inflight = null;
      });
    }
    return withTimeout(inflight, timeoutMs, "zalo groups");
  };
}
