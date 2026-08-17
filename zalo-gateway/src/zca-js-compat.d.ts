/*
 * zca-js 2.1.2 publishes index.d.ts with `export * from "./dist"`, but the
 * published package does not expose declarations from that path correctly to
 * TypeScript NodeNext. Runtime ESM exports are present; this shim describes only
 * the public surface used by the gateway until upstream fixes its package types.
 */
declare module "zca-js" {
  export type Credentials = {
    cookie: unknown;
    imei: string;
    userAgent: string;
    language?: string;
  };

  export enum ThreadType {
    User = 0,
    Group = 1,
  }

  export enum LoginQRCallbackEventType {
    QRCodeGenerated = 0,
    QRCodeExpired = 1,
    QRCodeScanned = 2,
    QRCodeDeclined = 3,
    GotLoginInfo = 4,
  }

  export class Zalo {
    constructor(options?: Record<string, unknown>);
    login(credentials: Credentials): Promise<any>;
    loginQR(
      options?: { userAgent?: string; language?: string; qrPath?: string },
      callback?: (event: any) => unknown,
    ): Promise<any>;
  }
}
