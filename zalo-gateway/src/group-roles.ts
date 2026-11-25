/**
 * Vai trò người gửi trong nhóm Zalo (trưởng nhóm / phó nhóm / thành viên), lưu kèm
 * từng tin nhóm để Python lọc tín hiệu cổ phiếu "chỉ admin nhóm" đúng vai trò TẠI
 * LÚC NHẮN (services/zalo_signals.py).
 *
 * - Nguồn: getGroupInfo của zca-js (creatorId = trưởng nhóm, adminIds = phó nhóm).
 *   Bot không cần là admin vẫn đọc được.
 * - Cache theo nhóm, chỉ giữ id trưởng/phó nhóm (vài chục chuỗi/nhóm), trần số nhóm.
 *   Làm mới khi cũ hơn ``ttlMs`` hoặc khi Zalo báo sự kiện đổi admin (invalidate).
 * - Lấy lỗi: giữ bản cũ nếu có, không thử lại trước ``retryMs``; chưa từng lấy được
 *   thì vai trò là "unknown" (Python coi như không phải admin - không báo nhầm).
 * - Mỗi nhóm chỉ 1 lượt gọi Zalo tại 1 thời điểm; người chờ tối đa ``timeoutMs``.
 */

import { withTimeout } from "./feedback.js";

export type GroupRole = "owner" | "admin" | "member" | "unknown";

type RoleDetail = { creatorId?: unknown; adminIds?: unknown };

/** Phần zca-js dùng ở đây (cùng getGroupInfo với groups.ts). */
export type RoleApi = {
  getGroupInfo(groupId: string | string[]): Promise<{
    changed_groups?: Record<string, RoleDetail>;
    gridInfoMap?: Record<string, RoleDetail>;
  }>;
};

export type GroupRolesOptions = {
  ttlMs?: number;
  retryMs?: number;
  timeoutMs?: number;
  maxGroups?: number;
  now?: () => number;
};

/** Danh sách admin cũ hơn chừng này thì lấy lại (sự kiện đổi admin làm mới sớm hơn). */
export const ROLE_TTL_MS = 30 * 60_000;
/** Lấy lỗi: không gọi lại Zalo cho nhóm đó trước chừng này. */
export const ROLE_RETRY_MS = 60_000;
/** Lưu tin nhóm chờ lấy vai trò tối đa chừng này (lượt gọi vẫn chạy tiếp để lần sau dùng). */
export const ROLE_FETCH_TIMEOUT_MS = 10_000;
/** Trần số nhóm trong cache (khớp MAX_GROUPS của groups.ts). */
export const ROLE_MAX_GROUPS = 200;
/** Trần số phó nhóm giữ cho 1 nhóm. */
export const ROLE_MAX_ADMINS = 100;

/**
 * Sự kiện nhóm (listener "group_event" của zca-js) có thể đổi trưởng/phó nhóm:
 * thêm/gỡ phó nhóm, cập nhật nhóm (chuyển trưởng nhóm), admin rời/bị xoá.
 */
export const ROLE_EVENTS = new Set([
  "add_admin",
  "remove_admin",
  "update",
  "leave",
  "remove_member",
  "block_member",
]);

type Entry = { owner: string; admins: Set<string>; fetchedAt: number };

export class GroupRoles {
  private readonly entries = new Map<string, Entry>();
  private readonly failedAt = new Map<string, number>();
  private readonly inflight = new Map<string, Promise<void>>();
  private readonly ttlMs: number;
  private readonly retryMs: number;
  private readonly timeoutMs: number;
  private readonly maxGroups: number;
  private readonly now: () => number;

  constructor(
    private readonly api: () => RoleApi | null,
    options: GroupRolesOptions = {},
  ) {
    this.ttlMs = options.ttlMs ?? ROLE_TTL_MS;
    this.retryMs = options.retryMs ?? ROLE_RETRY_MS;
    this.timeoutMs = options.timeoutMs ?? ROLE_FETCH_TIMEOUT_MS;
    this.maxGroups = options.maxGroups ?? ROLE_MAX_GROUPS;
    this.now = options.now ?? Date.now;
  }

  /** Vai trò của người gửi trong nhóm; tự làm mới danh sách admin khi cũ. Không ném. */
  async roleOf(groupId: string, senderId: string): Promise<GroupRole> {
    await this.ensure(groupId);
    const entry = this.entries.get(groupId);
    if (!entry || !senderId) return "unknown";
    if (entry.owner && senderId === entry.owner) return "owner";
    return entry.admins.has(senderId) ? "admin" : "member";
  }

  /** Zalo báo đổi admin: lần tới lấy lại ngay (bỏ cả thời gian chờ sau lỗi). */
  invalidate(groupId: string): void {
    const entry = this.entries.get(groupId);
    if (entry) entry.fetchedAt = Number.NEGATIVE_INFINITY;
    this.failedAt.delete(groupId);
  }

  /** Bỏ nhóm không còn theo dõi khỏi cache. */
  retain(groupIds: Set<string>): void {
    for (const id of [...this.entries.keys()]) if (!groupIds.has(id)) this.entries.delete(id);
    for (const id of [...this.failedAt.keys()]) if (!groupIds.has(id)) this.failedAt.delete(id);
  }

  get size(): number {
    return this.entries.size;
  }

  private async ensure(groupId: string): Promise<void> {
    const now = this.now();
    const entry = this.entries.get(groupId);
    if (entry && now - entry.fetchedAt < this.ttlMs) return;
    const failed = this.failedAt.get(groupId);
    if (failed !== undefined && now - failed < this.retryMs) return;
    let job = this.inflight.get(groupId);
    if (!job) {
      job = this.fetch(groupId).finally(() => this.inflight.delete(groupId));
      this.inflight.set(groupId, job);
    }
    try {
      await withTimeout(job, this.timeoutMs, "zalo group roles");
    } catch {
      // Quá giờ chờ: dùng bản cũ (nếu có); lượt gọi vẫn chạy tiếp và cập nhật cache.
    }
  }

  private async fetch(groupId: string): Promise<void> {
    const client = this.api();
    if (!client) {
      this.failedAt.set(groupId, this.now());
      return;
    }
    try {
      const info = await client.getGroupInfo(groupId);
      const detail = info?.gridInfoMap?.[groupId] || info?.changed_groups?.[groupId];
      if (!detail) throw new Error("getGroupInfo không trả thông tin nhóm");
      // Thiếu hẳn cả 2 trường (phản hồi rút gọn): coi như chưa lấy được, không suy ra
      // "mọi người là thành viên" rồi lặng lẽ bỏ tin của admin.
      if (detail.creatorId == null && !Array.isArray(detail.adminIds)) {
        throw new Error("getGroupInfo không có creatorId/adminIds");
      }
      const owner = detail.creatorId == null ? "" : String(detail.creatorId);
      const admins = new Set(
        (Array.isArray(detail.adminIds) ? detail.adminIds : [])
          .slice(0, ROLE_MAX_ADMINS)
          .map((id) => String(id)),
      );
      if (!this.entries.has(groupId) && this.entries.size >= this.maxGroups) {
        const oldest = this.entries.keys().next().value;
        if (oldest !== undefined) this.entries.delete(oldest);
      }
      this.entries.delete(groupId); // chèn lại để thứ tự Map = thứ tự làm mới
      this.entries.set(groupId, { owner, admins, fetchedAt: this.now() });
      this.failedAt.delete(groupId);
    } catch (error) {
      this.failedAt.set(groupId, this.now());
      console.warn(`[zalo] không lấy được admin nhóm ${groupId}`, error);
    }
  }
}
