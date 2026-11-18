"""Narrow Zalo handoff: one owner reply may answer one pending private customer question.

This is a server operation, never an MCP tool exposed to the customer-facing model.
The owner must use the case code; ordinary conversation with the owner is untouched.
"""
from __future__ import annotations

import re
import secrets
import sqlite3
import time

from config import STATE_DIR

DB = STATE_DIR / "chatbot-relay.sqlite3"
TTL = 48 * 3600
CODE = re.compile(r"^#TL-([0-9A-F]{8})\s+(.+)$", re.DOTALL)


def _db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB, timeout=10)
    conn.execute("""CREATE TABLE IF NOT EXISTS relay (
        code TEXT PRIMARY KEY, bot_id TEXT NOT NULL, account_id TEXT NOT NULL,
        owner_id TEXT NOT NULL, customer_id TEXT NOT NULL, customer_name TEXT,
        question TEXT NOT NULL, note TEXT, created_at REAL NOT NULL,
        status TEXT NOT NULL, answer TEXT)""")
    return conn


def owner_id(cfg):
    """An explicit private Zalo thread, never inferred from a name or a model answer."""
    value = str((cfg or {}).get("relay_owner_id") or "").strip()
    return value if re.fullmatch(r"\d{3,25}", value) else ""


def create(bot_id, account_id, owner, customer_id, customer_name, question, note):
    if not all((bot_id, account_id, owner, customer_id, question)) or owner == customer_id:
        return ""
    with _db() as db:
        # A fresh question supersedes the previous unanswered question in this customer chat.
        db.execute("""UPDATE relay SET status='superseded' WHERE bot_id=? AND account_id=?
                      AND customer_id=? AND status='pending'""", (bot_id, account_id, customer_id))
        for _ in range(5):
            code = secrets.token_hex(4).upper()
            try:
                db.execute("""INSERT INTO relay VALUES (?,?,?,?,?,?,?,?,?,'pending',NULL)""",
                           (code, bot_id, account_id, owner, customer_id, customer_name[:80],
                            question[:1000], note[:500], time.time()))
                return code
            except sqlite3.IntegrityError:
                continue
    return ""


def set_status(code, status):
    with _db() as db:
        db.execute("UPDATE relay SET status=? WHERE code=?", (status, code))


def claim(bot_id, account_id, sender_id, text):
    """Atomically claim one exact case. Invalid/expired/already used codes send nothing."""
    m = CODE.fullmatch(str(text or "").strip())
    if not m:
        return None
    code, answer = m.groups()
    answer = answer.strip()
    if not answer or len(answer) > 1500 or "[BAO_CHU]" in answer.upper():
        return None
    with _db() as db:
        row = db.execute("""SELECT customer_id, customer_name, created_at FROM relay
                            WHERE code=? AND bot_id=? AND account_id=? AND owner_id=?
                            AND status='pending'""", (code, bot_id, account_id, sender_id)).fetchone()
        if not row or time.time() - row[2] > TTL:
            return None
        changed = db.execute("""UPDATE relay SET status='sending', answer=?
                                WHERE code=? AND status='pending'""", (answer, code)).rowcount
        if changed != 1:
            return None
    return {"code": code, "customer_id": row[0], "customer_name": row[1], "answer": answer}


async def handle_owner_reply(transport, ev, text):
    """Consume only a valid owner case reply; other owner messages reach the normal bot."""
    cfg = transport.cfg_fn() if transport.cfg_fn else {}
    owner = owner_id(cfg)
    if (not owner or ev.get("chat_type") != "private"
            or ev.get("sender_type") != "customer"
            or str(ev.get("account_id") or "") != transport.conn_id
            or str(ev.get("external_chat_id") or "") != owner
            or str(ev.get("sender_id") or "") != owner):
        return False
    case = claim(str(cfg.get("id") or ""), transport.conn_id, owner, text)
    if case is None:
        if str(text or "").strip().upper().startswith("#TL-"):
            await transport._gui(owner, "Mã chuyển tiếp không còn hiệu lực hoặc không thuộc chat này. Anh xem lại mã trong tin em đã gửi.", "private")
            return True
        return False
    # The owner sees the explicit instruction at escalation time: text after the code is sent verbatim.
    ok = await transport._gui(case["customer_id"], case["answer"], "private")
    set_status(case["code"], "sent" if ok else "failed")
    if ok:
        import chatbot_runtime
        chatbot_runtime.ghi_tin_bot(cfg, {"account_id": transport.conn_id, "chat_id": case["customer_id"],
                                      "chat_type": "private", "user_id": case["customer_id"],
                                      "user_name": case["customer_name"]}, case["answer"])
        await transport._gui(owner, f"Đã chuyển câu trả lời #{'TL-' + case['code']} cho {case['customer_name'] or case['customer_id']}.", "private")
    else:
        await transport._gui(owner, f"Chưa gửi được câu trả lời #TL-{case['code']} cho khách. Anh kiểm tra và nhắn trực tiếp giúp em.", "private")
    return True


async def escalate(transport, cfg, meta, question, note):
    """Open a case only after the owner DM was accepted by Zalo."""
    owner = owner_id(cfg)
    account = str(meta.get("account_id") or "")
    customer = str(meta.get("chat_id") or "")
    if (not owner or not transport or account != transport.conn_id or meta.get("chat_type") != "private"
            or not customer or customer == owner):
        return False
    code = create(str(cfg.get("id") or ""), account, owner, customer,
                  str(meta.get("user_name") or ""), str(question or ""), str(note or ""))
    if not code:
        return False
    msg = (f"Khách {meta.get('user_name') or customer} hỏi: {str(question or '')[:700]}\n"
           + (f"Ghi chú bot: {str(note)[:300]}\n" if note else "")
           + f"Trả lời bằng: #TL-{code} <nội dung gửi cho khách>. Phần sau mã sẽ gửi nguyên văn tới đúng chat khách này.")
    if not await transport._gui(owner, msg, "private"):
        set_status(code, "failed")
        return False
    return True
