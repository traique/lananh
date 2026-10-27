/**
 * Lệnh đi "làn nhanh": gửi thẳng sang Python NGOÀI hàng đợi theo người gửi
 * (directQueue), timeout ngắn, để dùng được ngay cả khi lượt dài của chính người
 * đó đang chạy (hàng đợi tuần tự sẽ giữ chúng sau lượt đó tới vài phút).
 *
 * PHẢI khớp services/turn_control.py bên Python (test/test_zalo_gateway_contract.py
 * kiểm tra): /huy huỷ lượt đang chạy; lệnh chỉ đọc không gọi AI, Python chạy
 * chúng ngoài khoá lượt (services/channel_dispatch.py).
 */
export const CANCEL_COMMANDS = ["/huy"];
/** Lệnh chỉ đọc với mọi tham số (tính bằng code / đọc DB, không gọi AI). */
export const READONLY_ANY_COMMANDS = [
  "/help",
  "/start",
  "/notes",
  "/history",
  "/lich",
  "/tsh",
  "/tuvi",
  "/kymon",
  "/danhgia",
  "/chuy",
];
/** Chỉ đọc khi KHÔNG có tham số (/memory on|off, /nhac 8h mai... là lệnh ghi). */
export const READONLY_BARE_COMMANDS = ["/memory", "/nhac", "/yenlang", "/viec", "/lichtacvu"];

/**
 * ["/lenh" viết thường, bỏ @tên_bot; phần tham số] - ["", ""] nếu không phải lệnh.
 * Tách ở dấu cách ĐẦU TIÊN như turn_control.parse_command (str.partition(" ")):
 * lệch cách tách thì gateway có thể đẩy ra làn nhanh 1 tin mà Python vẫn bắt
 * chờ khoá lượt.
 */
export function parseCommand(text: string): [string, string] {
  const raw = (text || "").trim();
  if (!raw.startsWith("/")) return ["", ""];
  const space = raw.indexOf(" ");
  const head = space < 0 ? raw : raw.slice(0, space);
  const rest = space < 0 ? "" : raw.slice(space + 1).trim();
  return [head.toLowerCase().split("@", 1)[0], rest];
}

export function isCancelCommand(text: string): boolean {
  return CANCEL_COMMANDS.includes(parseCommand(text)[0]);
}

export function isReadonlyCommand(text: string): boolean {
  const [command, argument] = parseCommand(text);
  if (READONLY_ANY_COMMANDS.includes(command)) return true;
  return READONLY_BARE_COMMANDS.includes(command) && !argument;
}

export function isFastLaneCommand(text: string): boolean {
  return isCancelCommand(text) || isReadonlyCommand(text);
}

/** Lệnh gateway tự xử lý (không qua Python), CHỈ dành cho chủ bot (controller). */
export const CONTROLLER_COMMANDS = ["/nhomzalo"];

/**
 * "run" = chủ bot gửi lệnh riêng của gateway; "ignore" = người khác gửi lệnh đó
 * (người lạ, thành viên role=user): im lặng hoàn toàn - không lộ lệnh admin hay
 * luồng /pair, cũng không đẩy sang Python (lệnh lạ thành 1 lượt chat AI);
 * "none" = không phải lệnh riêng của gateway.
 */
export function controllerCommandAction(
  text: string,
  sender: string,
  controllerId: string,
): "run" | "ignore" | "none" {
  if (!CONTROLLER_COMMANDS.includes((text || "").toLowerCase())) return "none";
  return controllerId && sender === controllerId ? "run" : "ignore";
}
