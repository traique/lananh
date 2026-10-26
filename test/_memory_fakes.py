"""DB giả trong RAM cho test trí nhớ (services/memory_service.py, memory_control...).

Mô phỏng đúng ngữ nghĩa các hàm core.database mà trí nhớ v2 dùng: fact hiện
hành/giá trị cũ (invalid_at), bảo vệ fact ghim/người dùng nhập, highlight có
trần, ghi chú phiên, ghi chú, lịch sử chat có kind, settings.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone


class FakeMemoryDB:
    def __init__(self) -> None:
        self.settings: dict[str, str] = {}
        self.facts: list[dict] = []
        self.highlights: list[dict] = []
        self.session_notes: dict[tuple[int, int], dict] = {}
        self.notes: list[dict] = []
        self.chat: list[dict] = []
        self.summary: dict[int, str] = {}
        self.events: list[dict] = []
        self._ids = itertools.count(1)
        self.now = datetime.now(timezone.utc)

    # ─── helpers cho test ───
    def add_turn(self, uid, user_text, reply, *, kind="chat", minutes_ago=0.0):
        when = self.now - timedelta(minutes=minutes_ago)
        for role, content in (("user", user_text), ("model", reply)):
            self.chat.append(
                {
                    "id": next(self._ids),
                    "uid": uid,
                    "role": role,
                    "kind": kind,
                    "content": content,
                    "created_at": when,
                }
            )

    def current(self, uid):
        rows = [f for f in self.facts if f["uid"] == uid and f["invalid_at"] is None]
        rows.sort(
            key=lambda f: (
                not f["pinned"],
                -f["importance"],
                -f["updated_at"].timestamp(),
                -f["id"],
            )
        )
        return rows

    def install(self, monkeypatch, db_module) -> None:
        for name in (
            "get_setting",
            "set_setting",
            "get_recent_chat_rows",
            "get_chat_rows_after",
            "get_fact_rows",
            "get_facts",
            "save_fact",
            "invalidate_fact",
            "delete_fact",
            "set_fact_pinned",
            "trim_facts",
            "clear_facts",
            "add_highlight",
            "get_highlight_rows",
            "get_highlights",
            "delete_highlights",
            "clear_highlights",
            "get_session_note",
            "get_session_notes",
            "upsert_session_note",
            "clear_session_notes",
            "get_recent_notes",
            "get_notes",
            "add_note",
            "delete_note",
            "clear_notes",
            "get_summary",
            "set_summary",
            "get_superseded_facts",
            "users_with_chat_since",
            "add_chat_turn",
            "get_last_model_row",
            "clear_chat",
            "get_session_messages",
            "answer_feedback_clear",
            "rewrite_session_note",
            "list_user_events",
            "finish_user_event_ids",
            "clear_user_events",
        ):
            monkeypatch.setattr(db_module, name, getattr(self, name))

    # ─── settings ───
    async def get_setting(self, key):
        return self.settings.get(key)

    async def set_setting(self, key, value):
        self.settings[key] = value

    # ─── chat ───
    async def get_recent_chat_rows(self, uid, limit=40, max_chars=1500):
        rows = [r for r in self.chat if r["uid"] == uid][::-1][:limit]
        return [{**r, "content": r["content"][:max_chars]} for r in rows]

    async def get_chat_rows_after(self, uid, after_id, limit=60, max_chars=1500):
        rows = [r for r in self.chat if r["uid"] == uid and r["id"] > after_id][:limit]
        return [{**r, "content": r["content"][:max_chars]} for r in rows]

    async def add_chat_turn(self, uid, user_text, model_text, *, kind="chat"):
        self.add_turn(uid, user_text, model_text, kind=kind)

    async def get_last_model_row(self, uid, max_chars=4000):
        rows = [r for r in self.chat if r["uid"] == uid and r["role"] == "model"]
        return dict(rows[-1]) if rows else None

    async def clear_chat(self, uid):
        self.chat = [r for r in self.chat if r["uid"] != uid]

    async def answer_feedback_clear(self, uid):
        # Đánh giá 👍/👎 (migrations/021) - fake không giữ dòng nào.
        return None

    async def get_session_messages(self, uid, k, session_timeout_sec):
        rows = [r for r in self.chat if r["uid"] == uid][-k:]
        return [(r["role"], r["content"]) for r in rows]

    async def users_with_chat_since(self, since, limit=50):
        return sorted({r["uid"] for r in self.chat if r["created_at"] >= since})[:limit]

    # ─── facts ───
    async def get_fact_rows(self, uid):
        return [dict(f) for f in self.current(uid)]

    async def get_facts(self, uid):
        return [(f["key"], f["value"]) for f in self.current(uid)]

    async def get_superseded_facts(self, uid, limit=20):
        rows = [f for f in self.facts if f["uid"] == uid and f["invalid_at"] is not None]
        rows.sort(key=lambda f: f["invalid_at"], reverse=True)
        return [dict(f) for f in rows[:limit]]

    async def save_fact(
        self,
        uid,
        key,
        value,
        *,
        importance=2,
        category="",
        source="auto",
        pinned=False,
        protect=True,
    ):
        now = datetime.now(timezone.utc)
        cur = next((f for f in self.current(uid) if f["key"] == key), None)
        if cur is not None:
            if protect and (cur["pinned"] or cur["source"] == "user"):
                return "protected"
            keep_pinned = pinned or cur["pinned"]
            keep_source = "user" if "user" in (source, cur["source"]) else "auto"
            if cur["value"] == value:
                cur.update(
                    importance=max(cur["importance"], importance),
                    pinned=keep_pinned,
                    source=keep_source,
                    updated_at=now,
                )
                return "unchanged"
            cur["invalid_at"] = now
            self._insert(uid, key, value, importance, category, keep_source, keep_pinned)
            return "updated"
        self._insert(uid, key, value, importance, category, source, pinned)
        return "added"

    def _insert(self, uid, key, value, importance, category, source, pinned):
        now = datetime.now(timezone.utc)
        self.facts.append(
            {
                "id": next(self._ids),
                "uid": uid,
                "key": key,
                "value": value,
                "importance": importance,
                "category": category,
                "source": source,
                "pinned": pinned,
                "created_at": now,
                "valid_from": now,
                "updated_at": now,
                "invalid_at": None,
            }
        )

    async def invalidate_fact(self, uid, fact_id, *, protect=True):
        for f in self.current(uid):
            if f["id"] == fact_id:
                if protect and (f["pinned"] or f["source"] == "user"):
                    return None
                f["invalid_at"] = datetime.now(timezone.utc)
                return f["key"]
        return None

    async def delete_fact(self, uid, key):
        self.facts = [f for f in self.facts if not (f["uid"] == uid and f["key"] == key)]

    async def set_fact_pinned(self, uid, fact_id, pinned):
        for f in self.current(uid):
            if f["id"] == fact_id:
                f["pinned"] = pinned
                return True
        return False

    async def trim_facts(self, uid, keep_n, **kwargs):
        keep = {f["id"] for f in self.current(uid)[:keep_n]}
        self.facts = [
            f
            for f in self.facts
            if f["uid"] != uid or f["invalid_at"] is not None or f["pinned"] or f["id"] in keep
        ]

    async def clear_facts(self, uid):
        self.facts = [f for f in self.facts if f["uid"] != uid]

    # ─── highlights ───
    async def add_highlight(self, uid, content, keep_n):
        self.highlights.append(
            {
                "id": next(self._ids),
                "uid": uid,
                "content": content,
                "created_at": datetime.now(timezone.utc),
            }
        )
        mine = [h for h in self.highlights if h["uid"] == uid]
        drop = {h["id"] for h in mine[:-keep_n]} if len(mine) > keep_n else set()
        self.highlights = [h for h in self.highlights if h["id"] not in drop]

    async def get_highlight_rows(self, uid, limit):
        return [dict(h) for h in self.highlights if h["uid"] == uid][::-1][:limit]

    async def get_highlights(self, uid, limit):
        return [h["content"] for h in self.highlights if h["uid"] == uid][-limit:]

    async def delete_highlights(self, uid, ids):
        before = len(self.highlights)
        self.highlights = [h for h in self.highlights if not (h["uid"] == uid and h["id"] in ids)]
        return before - len(self.highlights)

    async def clear_highlights(self, uid):
        self.highlights = [h for h in self.highlights if h["uid"] != uid]

    # ─── session notes ───
    async def upsert_session_note(
        self, uid, session_key, *, title="", bullets="", summary="", started_at
    ):
        note = self.session_notes.setdefault(
            (uid, session_key),
            {
                "session_key": session_key,
                "title": "",
                "bullets": "",
                "summary": "",
                "started_at": started_at,
            },
        )
        for name, value in (("title", title), ("bullets", bullets), ("summary", summary)):
            if value:
                note[name] = value
        note["updated_at"] = datetime.now(timezone.utc)

    async def get_session_note(self, uid, session_key):
        note = self.session_notes.get((uid, session_key))
        return dict(note) if note else None

    async def get_session_notes(self, uid, limit=10, *, exclude_session_key=None):
        rows = [
            dict(n)
            for (u, k), n in self.session_notes.items()
            if u == uid and k != exclude_session_key
        ]
        rows.sort(key=lambda n: n["updated_at"], reverse=True)
        return rows[:limit]

    async def clear_session_notes(self, uid):
        self.session_notes = {k: v for k, v in self.session_notes.items() if k[0] != uid}

    async def rewrite_session_note(self, uid, session_key, *, title, bullets, summary):
        if not (title or bullets or summary):
            self.session_notes.pop((uid, session_key), None)
            return
        note = self.session_notes.get((uid, session_key))
        if note is not None:
            note.update(title=title, bullets=bullets, summary=summary)

    # ─── sự kiện hỏi thăm ───
    def add_event(self, uid, title, status="pending"):
        self.events.append({"id": next(self._ids), "uid": uid, "title": title, "status": status})

    async def list_user_events(self, uid, limit=20):
        return [dict(e) for e in self.events if e["uid"] == uid][::-1][:limit]

    async def finish_user_event_ids(self, uid, ids):
        count = 0
        for e in self.events:
            if e["uid"] == uid and e["id"] in ids and e["status"] == "pending":
                e["status"] = "done"
                count += 1
        return count

    async def clear_user_events(self, uid):
        self.events = [e for e in self.events if e["uid"] != uid]

    # ─── notes ───
    async def add_note(self, uid, content, *, event_key=None):
        self.notes.append(
            {
                "id": next(self._ids),
                "uid": uid,
                "content": content,
                "created_at": datetime.now(timezone.utc),
            }
        )

    async def get_recent_notes(self, uid, limit=200, max_chars=400):
        rows = [n for n in self.notes if n["uid"] == uid][::-1][:limit]
        return [{**n, "content": n["content"][:max_chars]} for n in rows]

    async def get_notes(self, uid, limit=10):
        rows = [n for n in self.notes if n["uid"] == uid][::-1][:limit]
        return [(n["content"], n["created_at"]) for n in rows]

    async def delete_note(self, uid, note_id):
        before = len(self.notes)
        self.notes = [n for n in self.notes if not (n["uid"] == uid and n["id"] == note_id)]
        return len(self.notes) < before

    async def clear_notes(self, uid):
        before = len(self.notes)
        self.notes = [n for n in self.notes if n["uid"] != uid]
        return before - len(self.notes)

    # ─── summary ───
    async def get_summary(self, uid):
        return self.summary.get(uid, "")

    async def set_summary(self, uid, text):
        self.summary[uid] = text
