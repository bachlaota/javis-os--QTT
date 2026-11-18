"""Narrow owner relay across private Zalo chats; no network or model access."""
from _paths import ROOT, SERVER  # noqa: E402,F401
import asyncio
import os
import tempfile

os.environ["JAVIS_STATE_DIR"] = tempfile.mkdtemp(prefix="javis-relay-")

import chatbot_relay as relay  # noqa: E402


class Transport:
    conn_id = "account-1"

    def __init__(self):
        self.sent = []
        self.fail = False
        self.cfg_fn = lambda: {"id": "bot-1", "relay_owner_id": "123456789"}

    async def _gui(self, thread, text, chat_type):
        self.sent.append((thread, text, chat_type))
        return not self.fail


def event(sender="123456789", account="account-1", chat="123456789"):
    return {"sender_type": "customer", "account_id": account, "external_chat_id": chat,
            "sender_id": sender, "chat_type": "private"}


async def main():
    t = Transport()
    cfg = t.cfg_fn()
    meta = {"account_id": t.conn_id, "chat_id": "555555555", "chat_type": "private", "user_name": "Chị Linh"}
    assert await relay.escalate(t, cfg, meta, "Vé nào phù hợp?", "Kiểm tra giá")
    assert len(t.sent) == 1 and t.sent[0][0] == "123456789"
    owner_message = t.sent[0][1]
    code = owner_message.split("#TL-")[1].split()[0]
    answer = f"#TL-{code} Giá vé là 990.000đ ạ."
    assert not await relay.handle_owner_reply(t, event(sender="999999999", chat="999999999"), answer)
    assert not await relay.handle_owner_reply(t, event(account="other"), answer)
    assert not await relay.handle_owner_reply(t, event(), "Anh xem giúp khách này")
    assert len(t.sent) == 1
    assert await relay.handle_owner_reply(t, event(), answer)
    assert t.sent[1] == ("555555555", "Giá vé là 990.000đ ạ.", "private")
    assert t.sent[2][0] == "123456789"
    assert await relay.handle_owner_reply(t, event(), answer)
    assert len(t.sent) == 4 and t.sent[-1][0] == "123456789"

    # A newer question makes the old code stale in this chat.
    older = relay.create("bot-1", "account-1", "123456789", "777777777", "Anh Nam", "Câu cũ", "")
    newer = relay.create("bot-1", "account-1", "123456789", "777777777", "Anh Nam", "Câu mới", "")
    assert not relay.claim("bot-1", "account-1", "123456789", f"#TL-{older} Đáp án cũ")
    assert relay.claim("bot-1", "account-1", "123456789", f"#TL-{newer} Đáp án mới")

    # A failed send is not retried automatically, to avoid duplicate messages after ambiguous Zalo errors.
    t.fail = True
    failed = relay.create("bot-1", "account-1", "123456789", "888888888", "Khách", "Câu hỏi", "")
    assert await relay.handle_owner_reply(t, event(), f"#TL-{failed} Xin chào")
    assert await relay.handle_owner_reply(t, event(), f"#TL-{failed} Xin chào")
    assert len([x for x in t.sent if x[0] == "888888888"]) == 1

    # Replying to the bot's quoted question works without typing a code.
    t.fail = False
    quoted = relay.create("bot-1", "account-1", "123456789", "666666666", "Khách mới", "Câu hỏi", "")
    ev = event()
    ev["metadata"] = {"replyTo": {"text": f"#TL-{quoted} | Khách mới hỏi: Câu hỏi"}}
    assert await relay.handle_owner_reply(t, ev, "Em xác nhận giá là 500.000đ ạ")
    assert t.sent[-2] == ("666666666", "Em xác nhận giá là 500.000đ ạ", "private")


asyncio.run(main())
print("PASS chatbot relay")
