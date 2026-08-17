import fs from "node:fs/promises";
import path from "node:path";
import { LoginQRCallbackEventType, Zalo } from "zca-js";

const outputDir = process.env.ZALO_QR_OUTPUT_DIR?.trim() || process.cwd();
const userAgent =
  process.env.ZALO_USER_AGENT?.trim() ||
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/145.0.0.0 Safari/537.36";

async function main(): Promise<void> {
  await fs.mkdir(outputDir, { recursive: true });
  const qrPath = path.join(outputDir, "zalo-qr.png");
  const sessionPath = path.join(outputDir, "zalo-session.json");
  const zalo = new Zalo({ selfListen: false, checkUpdate: false, logging: false });

  console.log(`[zalo-login] waiting for QR; image will be written to ${qrPath}`);
  const api = await zalo.loginQR({ userAgent }, async (event: any) => {
    if (event.type === LoginQRCallbackEventType.QRCodeGenerated) {
      const raw = String(event.data?.image || event.data?.qrData || "").replace(/^data:image\/png;base64,/, "");
      if (raw) {
        await fs.writeFile(qrPath, Buffer.from(raw, "base64"));
        console.log(`[zalo-login] QR updated: ${qrPath}`);
      }
    } else if (event.type === LoginQRCallbackEventType.QRCodeScanned) {
      console.log("[zalo-login] QR scanned; approve the login in Zalo");
    } else if (event.type === LoginQRCallbackEventType.QRCodeExpired) {
      console.log("[zalo-login] QR expired; waiting for a new one");
    } else if (event.type === LoginQRCallbackEventType.QRCodeDeclined) {
      console.error("[zalo-login] login declined");
    }
  });

  const context: any = api.getContext();
  const session = {
    accountId: api.getOwnId(),
    cookie: context.cookie.serializeSync(),
    imei: context.imei,
    userAgent: context.userAgent,
  };
  await fs.writeFile(sessionPath, JSON.stringify(session, null, 2), { mode: 0o600 });
  await fs.rm(qrPath, { force: true });
  console.log(`[zalo-login] session saved locally to ${sessionPath}`);
  console.log("[zalo-login] copy each value to Render environment variables; never commit this file");
}

main().catch((error) => {
  console.error("[zalo-login] failed", error instanceof Error ? error.message : error);
  process.exit(1);
});
