import { createServer, type Server } from "node:http";

export type ControlHandlers = {
  status(): Record<string, unknown>;
  startQr(): Promise<string>;
  startPairing(): { code: string; expiresAt: number } | null;
  logout(): Promise<void>;
};

/** HTTP server điều khiển nội bộ - CHỈ bind 127.0.0.1, Python gọi qua loopback. */
export function startControlServer(port: number, handlers: ControlHandlers): Server {
  const server = createServer(async (req, res) => {
    res.setHeader("content-type", "application/json");
    const reply = (status: number, body: unknown) => {
      res.statusCode = status;
      res.end(JSON.stringify(body));
    };
    try {
      if (req.method === "GET" && req.url === "/status") return reply(200, handlers.status());
      if (req.method === "POST" && req.url === "/login/qr") {
        return reply(200, { ok: true, state: await handlers.startQr() });
      }
      if (req.method === "POST" && req.url === "/pairing/start") {
        const pairing = handlers.startPairing();
        if (!pairing) return reply(409, { error: "Zalo is not connected" });
        return reply(200, pairing);
      }
      if (req.method === "POST" && req.url === "/logout") {
        await handlers.logout();
        return reply(200, { ok: true });
      }
      return reply(404, { error: "not found" });
    } catch (error) {
      return reply(500, { error: error instanceof Error ? error.message : "error" });
    }
  });
  server.listen(port, "127.0.0.1");
  return server;
}
