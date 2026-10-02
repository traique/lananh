"""Single-connection PGlite adapter for the opt-in SQL state-transition tests.

Not a production pool and not a concurrency/locking simulator.
"""

from pathlib import Path
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta


class Pool:
    @classmethod
    async def create(cls):
        s = cls()
        s.proc = await asyncio.create_subprocess_exec(
            "node",
            str(Path(__file__).with_name("pglite_rpc.mjs")),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return s

    async def close(self):
        self.proc.stdin.close()
        await self.proc.wait()

    @asynccontextmanager
    async def acquire(self):
        yield self

    @asynccontextmanager
    async def transaction(self):
        await self.execute("BEGIN")
        try:
            yield
        except BaseException:
            await self.execute("ROLLBACK")
            raise
        else:
            await self.execute("COMMIT")

    async def query(self, sql, params):
        def encode(v):
            if isinstance(v, datetime):
                return v.isoformat()
            if isinstance(v, timedelta):
                return f"{v.total_seconds()} seconds"
            if isinstance(v, bytes):
                return "\\x" + v.hex()
            return v

        self.proc.stdin.write(
            (
                json.dumps(dict(sql=sql, params=[encode(v) for v in params], exec=not params))
                + "\n"
            ).encode()
        )
        await self.proc.stdin.drain()
        line = await self.proc.stdout.readline()
        if not line:
            raise RuntimeError((await self.proc.stderr.read()).decode())
        r = json.loads(line)
        if not r["ok"]:
            raise RuntimeError(r["error"] + "\n" + sql)
        result = r["result"]
        result = result[-1] if isinstance(result, list) else result
        for row in result.get("rows", []):
            for k, v in row.items():
                if isinstance(v, str) and (k.endswith("_at") or k.endswith("_until")):
                    row[k] = datetime.fromisoformat(v.replace("Z", "+00:00"))
        return result

    async def execute(self, sql, *params):
        r = await self.query(sql, params)
        cmd = r["command"]
        n = r["affectedRows"]
        return f"INSERT 0 {n}" if cmd == "INSERT" else f"{cmd} {n}"

    async def fetch(self, sql, *params):
        return (await self.query(sql, params))["rows"]

    async def fetchrow(self, sql, *params):
        rows = await self.fetch(sql, *params)
        return rows[0] if rows else None

    async def fetchval(self, sql, *params):
        row = await self.fetchrow(sql, *params)
        return next(iter(row.values())) if row else None

    async def executemany(self, sql, args):
        for params in args:
            await self.execute(sql, *params)
