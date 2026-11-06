"""Почта «младшего помощника»: чтение на моке IMAP по протоколу, черновик, отправка только с подтверждением.

The mock speaks IMAP to the real imaplib client: every command the tool sends is on the wire log, and the
mock behaves like a real server — a plain FETCH BODY[] or a STORE marks the letter \\Seen. A tool that
reads must leave every letter's flags as they were.
"""

from __future__ import annotations

import base64
import email
import email.message
import email.policy
import imaplib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.agent import executors, mail, tools
from app.agent.guard import Guard, risky_tool
from app.agent.loop import Budget, ToolResult, run_agent
from app.llm.client import Completion, ToolCall
from app.models import User
from app.vault import store_credential

KEY = base64.b64encode(b"k" * 32).decode()
LOGIN, PASSWORD = "owner@example.com", "app-password-1"


# --- IMAP mock ---------------------------------------------------------------------


def _letter(subject: str, sender: str, body: str, attachment: tuple[str, bytes] | None = None) -> bytes:
    msg = email.message.EmailMessage()
    msg["From"] = sender
    msg["To"] = LOGIN
    msg["Subject"] = subject
    msg["Date"] = "Thu, 05 Nov 2026 10:00:00 +0300"
    msg.set_content(body)
    if attachment:
        msg.add_attachment(attachment[1], maintype="text", subtype="csv", filename=attachment[0])
    return msg.as_bytes()


class ImapServer:
    """State of the mailbox and the log of every command received."""

    def __init__(self) -> None:
        self.folders = {
            "INBOX": [
                {"uid": 7, "flags": set(), "raw": _letter(
                    "Счёт №15 на перчатки", "Поставщик <supply@example.com>",
                    "Добрый день! Счёт на 12 500 ₽, оплатить до 10.11.2026.",
                    ("заказ.csv", "артикул;кол-во\nGL-M-100;20\n".encode()))},
                {"uid": 8, "flags": {"\\Seen"}, "raw": _letter(
                    "Новости склада", "Склад <news@example.com>", "Поступление 3 палет 12.11.")},
            ],
            # «Отправленные» as Yandex names it on the wire: modified UTF-7.
            "&BB4EQgQ,BEAEMAQyBDsENQQ9BD0ESwQ1-": [
                {"uid": 3, "flags": {"\\Seen"}, "raw": _letter(
                    "Прайс на упаковку", f"Я <{LOGIN}>", "Лента 48 мм — 95 руб.")},
            ],
        }
        self.log: list[str] = []
        self.selected: str | None = None
        self.readonly = True

    def flags(self) -> dict:
        return {(name, m["uid"]): set(m["flags"]) for name, box in self.folders.items() for m in box}

    def commands(self) -> list[str]:
        """Command names as sent, with UID unwrapped: ["CAPABILITY", "LOGIN", "EXAMINE", "UID SEARCH", ...]."""
        out = []
        for line in self.log:
            words = line.split(" ")
            out.append(" ".join(words[1:3]) if len(words) > 2 and words[1].upper() == "UID" else words[1])
        return [c.upper() for c in out]

    # -- protocol ----------------------------------------------------------------
    def handle(self, line: bytes) -> bytes:
        text = line.decode("utf-8", errors="replace").rstrip("\r\n")
        self.log.append(text)
        tag, _, rest = text.partition(" ")
        command, _, args = rest.partition(" ")
        command = command.upper()
        if command == "UID":
            sub, _, args = args.partition(" ")
            return self._uid(tag, sub.upper(), args)
        if command == "CAPABILITY":
            return f"* CAPABILITY IMAP4rev1 UIDPLUS\r\n{tag} OK done\r\n".encode()
        if command == "LOGIN":
            user, _, password = args.partition(" ")
            if (user.strip('"'), password.strip('"')) != (LOGIN, PASSWORD):
                return f"{tag} NO [AUTHENTICATIONFAILED] bad login\r\n".encode()
            return f"{tag} OK logged in\r\n".encode()
        if command == "LIST":
            lines = "".join(f'* LIST (\\HasNoChildren) "|" "{name}"\r\n' for name in self.folders)
            return f"{lines}{tag} OK done\r\n".encode()
        if command in {"SELECT", "EXAMINE"}:
            name = args.strip('"')
            if name not in self.folders:
                return f"{tag} NO no such folder\r\n".encode()
            self.selected, self.readonly = name, command == "EXAMINE"
            mode = "READ-ONLY" if self.readonly else "READ-WRITE"
            return (f"* {len(self.folders[name])} EXISTS\r\n* OK [UIDVALIDITY 1] ok\r\n"
                    f"{tag} OK [{mode}] {command} done\r\n").encode()
        if command == "STORE":
            return self._store(tag, args, by_uid=False)
        if command == "LOGOUT":
            return f"* BYE bye\r\n{tag} OK done\r\n".encode()
        return f"{tag} BAD unknown command\r\n".encode()

    def _box(self) -> list[dict]:
        return self.folders[self.selected or "INBOX"]

    def _uid(self, tag: str, sub: str, args: str) -> bytes:
        if sub == "SEARCH":
            found = [str(m["uid"]) for m in self._box() if self._matches(m, args)]
            return f"* SEARCH {' '.join(found)}\r\n{tag} OK done\r\n".encode()
        if sub == "FETCH":
            return self._fetch(tag, args)
        if sub == "STORE":
            return self._store(tag, args, by_uid=True)
        return f"{tag} BAD unknown UID command\r\n".encode()

    def _matches(self, letter: dict, args: str) -> bool:
        msg = email.message_from_bytes(letter["raw"], policy=email.policy.default)
        tokens = re.findall(r'"((?:[^"\\]|\\.)*)"|(\S+)', args)
        words = [quoted or bare for quoted, bare in tokens]
        index = 0
        while index < len(words):
            key = words[index].upper()
            if key == "CHARSET":
                index += 2
                continue
            if key in {"FROM", "SUBJECT", "TEXT"}:
                value = words[index + 1].lower()
                hay = {"FROM": str(msg["From"]), "SUBJECT": str(msg["Subject"])}.get(key, letter["raw"].decode(errors="replace") + str(msg["Subject"]))
                if key == "TEXT":
                    hay += msg.get_body(preferencelist=("plain",)).get_content()
                if value not in hay.lower():
                    return False
                index += 2
                continue
            if key == "UNSEEN" and "\\Seen" in letter["flags"]:
                return False
            index += 2 if key == "SINCE" else 1
        return True

    def _fetch(self, tag: str, args: str) -> bytes:
        uid, _, items = args.partition(" ")
        out = b""
        for seq, letter in enumerate(self._box(), 1):
            if str(letter["uid"]) != uid:
                continue
            upper = items.upper()
            if "BODY[" in upper and "BODY.PEEK[" not in upper and not self.readonly:
                letter["flags"].add("\\Seen")  # what a real server does on a plain BODY[] fetch
            if "HEADER.FIELDS" in upper:
                msg = email.message_from_bytes(letter["raw"], policy=email.policy.default)
                data = "".join(f"{h}: {msg[h]}\r\n" for h in ("From", "Subject", "Date")).encode() + b"\r\n"
                item = "BODY[HEADER.FIELDS (FROM SUBJECT DATE)]"
            else:
                data, item = letter["raw"], "BODY[]"
            flags = " ".join(sorted(letter["flags"]))
            out += f"* {seq} FETCH (UID {uid} FLAGS ({flags}) {item} {{{len(data)}}}\r\n".encode() + data + b")\r\n"
        return out + f"{tag} OK done\r\n".encode()

    def _store(self, tag: str, args: str, by_uid: bool) -> bytes:
        ref, _, change = args.partition(" ")
        for seq, letter in enumerate(self._box(), 1):
            if str(letter["uid"] if by_uid else seq) == ref and "\\SEEN" in change.upper() and not self.readonly:
                letter["flags"].add("\\Seen")
        return f"{tag} OK done\r\n".encode()


class _Wire:
    """File-like end of the connection the client reads from."""

    def __init__(self) -> None:
        self.buffer = b""

    def readline(self, limit: int = -1) -> bytes:
        end = self.buffer.find(b"\n")
        end = len(self.buffer) if end < 0 else end + 1
        line, self.buffer = self.buffer[:end], self.buffer[end:]
        return line

    def read(self, size: int) -> bytes:
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data

    def close(self) -> None:
        pass


class MockImap(imaplib.IMAP4):
    """The real imaplib client, talking to ImapServer instead of a socket."""

    def __init__(self, server: ImapServer) -> None:
        self.server = server
        self._pending = b""
        super().__init__("imap.mock", 993)

    def open(self, host="", port=993, timeout=None) -> None:  # noqa: D401 - imaplib hook
        self.host, self.port = host, port
        self.file = _Wire()
        self.file.buffer = b"* OK IMAP4rev1 mock ready\r\n"

    def send(self, data: bytes) -> None:
        self._pending += data
        while b"\r\n" in self._pending:
            line, self._pending = self._pending.split(b"\r\n", 1)
            self.file.buffer += self.server.handle(line + b"\r\n")

    def shutdown(self) -> None:
        pass


def _connector(server: ImapServer):
    def connect(host: str, username: str, password: str):
        assert host == "imap.yandex.ru"
        conn = MockImap(server)
        conn.login(username, password)
        return conn

    return connect


# --- fixtures ----------------------------------------------------------------------


@pytest.fixture()
def mailbox(session, monkeypatch):
    """A user with a mailbox in «Доступы» and a fresh IMAP mock; tool registries restored after the test."""
    monkeypatch.setenv("VAULT_MASTER_KEY", KEY)
    monkeypatch.setattr(tools, "_CLIENT", dict(tools._CLIENT))
    monkeypatch.setattr(executors, "_EXECUTORS", dict(executors._EXECUTORS))
    user = User(email="biz@example.com", consent_pdn=True, consent_training=False, is_admin=False,
                created_at=datetime.now(timezone.utc))
    session.add(user)
    session.flush()
    store_credential(session, user, "imap.yandex.ru", LOGIN, PASSWORD)
    return session, user, ImapServer()


class FakeSMTP:
    sent: list = []

    def __init__(self, host, port, timeout=None) -> None:
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        pass

    def login(self, user, password) -> None:
        assert (user, password) == (LOGIN, PASSWORD)

    def send_message(self, msg) -> None:
        FakeSMTP.sent.append(msg)


@pytest.fixture()
def smtp(monkeypatch):
    FakeSMTP.sent = []
    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP.sent


def _read(mailbox, **args):
    session, user, server = mailbox
    return mail.execute(session, user, None, "mail_read", args, connector=_connector(server))


# --- reading -----------------------------------------------------------------------


def test_mock_marks_letters_seen_like_a_real_server() -> None:
    """The mock is strict enough to catch a regression: SELECT + BODY[] or STORE would mark the letter."""
    server = ImapServer()
    conn = _connector(server)("imap.yandex.ru", LOGIN, PASSWORD)
    conn.select('"INBOX"')
    conn.uid("FETCH", "7", "(BODY[])")
    assert "\\Seen" in server.flags()[("INBOX", 7)]
    server = ImapServer()
    conn = _connector(server)("imap.yandex.ru", LOGIN, PASSWORD)
    conn.select('"INBOX"')
    conn.uid("STORE", "7", "+FLAGS", "(\\Seen)")
    assert "\\Seen" in server.flags()[("INBOX", 7)] and "UID STORE" in server.commands()


def test_mail_read_every_action_keeps_letters_unread(mailbox) -> None:
    server = mailbox[2]
    before = server.flags()

    folders = _read(mailbox, action="folders")
    listed = _read(mailbox, action="list")
    by_sender = _read(mailbox, action="search", **{"from": "supply@example.com"})
    by_subject = _read(mailbox, action="search", subject="Счёт")
    unseen = _read(mailbox, action="search", unseen=True)
    letter = _read(mailbox, action="read", uid="7")
    attached = _read(mailbox, action="attachment", uid="7", index=1)
    sent = _read(mailbox, action="list", folder="Отправленные")  # the name from folders, not UTF-7

    results = [folders, listed, by_sender, by_subject, unseen, letter, attached, sent]
    assert all(r.error is None for r in results), [(r.error, r.content) for r in results]
    assert "INBOX" in folders.content and "Отправленные" in folders.content
    assert "uid 8" in listed.content and "uid 7" in listed.content
    assert "Счёт №15" in by_sender.content and "uid 8" not in by_sender.content
    assert "uid 7" in by_subject.content  # Cyrillic search goes as UTF-8 with CHARSET
    assert "uid 7" in unseen.content and "uid 8" not in unseen.content
    assert "12 500 ₽" in letter.content and "заказ.csv" in letter.content
    assert "GL-M-100;20" in attached.content
    assert "Прайс на упаковку" in sent.content

    # Only looking: EXAMINE (read-only), BODY.PEEK, no flag changes and no other writes.
    commands = server.commands()
    assert "EXAMINE" in commands and "SELECT" not in commands
    fetches = [line for line in server.log if " FETCH " in line.upper()]
    assert fetches and all("BODY.PEEK[" in line.upper() for line in fetches)
    writes = {"STORE", "UID STORE", "COPY", "UID COPY", "MOVE", "UID MOVE", "EXPUNGE", "APPEND", "DELETE"}
    assert not writes & set(commands)
    assert not any("FLAGS" in line.upper() and "FETCH" not in line.upper() for line in server.log)
    assert server.flags() == before and "\\Seen" not in server.flags()[("INBOX", 7)]
    assert PASSWORD not in "".join(r.content for r in results)


def test_mail_read_bad_folder_and_uid_are_errors_not_crashes(mailbox) -> None:
    assert _read(mailbox, action="list", folder="Нет такой").error == "bad_folder"
    assert _read(mailbox, action="read", uid="7; STORE").error == "bad_uid"
    assert "\\Seen" not in mailbox[2].flags()[("INBOX", 7)]


# --- agent loop over the real mail tool --------------------------------------------


def _completion(text: str = "", call: tuple[str, dict] | None = None) -> Completion:
    calls = [ToolCall(f"c{id(call)}", call[0], json.dumps(call[1], ensure_ascii=False))] if call else []
    return Completion("heli-fast", text, 10, 5, Decimal("0.01"), tool_calls=calls)


def _scripted(*steps: Completion):
    queue = list(steps)
    seen: list[list[dict]] = []

    def complete(messages, round_tools):
        seen.append(list(messages))
        return queue.pop(0) if queue else _completion("Готово.")

    complete.seen = seen
    return complete


def _caller(mailbox, confirm: str = "approve"):
    """call_tool for run_agent: mail_* through the real executor, ask_confirmation answered by the «user»."""
    session, user, server = mailbox
    log: list[tuple[str, dict]] = []

    def call(call_id: str, name: str, args: dict) -> ToolResult:
        log.append((name, args))
        if name == "ask_confirmation":
            return ToolResult("approved" if confirm == "approve" else "rejected: пользователь отказал")
        if name.startswith("mail_"):
            result = mail.execute(session, user, None, name, args, connector=_connector(server))
            return ToolResult(result.content, result.error)
        return ToolResult("ok")

    call.log = log
    return call


def _tools(*, send: bool, confirm: bool = True) -> list[dict]:
    mail.register()
    offered = tools.tools_for(tools.client_tool_names())
    names = {t["function"]["name"] for t in offered}
    assert ("mail_send" in names) is send
    return [t for t in offered if confirm or t["function"]["name"] != "ask_confirmation"]


SEND_ARGS = {"to": "supply@example.com", "subject": "Re: Счёт №15", "body": "Счёт получили, оплатим до 10.11.2026."}


def test_draft_reply_does_not_send_and_send_is_off_by_default(mailbox, smtp, monkeypatch) -> None:
    monkeypatch.delenv("HELI_MAIL_SEND", raising=False)
    offered = _tools(send=False)
    call = _caller(mailbox)
    draft = "Черновик (не отправлял):\nКому: supply@example.com\nТема: Re: Счёт №15\n\nСчёт получили, оплатим до 10.11.2026."
    complete = _scripted(
        _completion(call=("mail_read", {"action": "search", "subject": "Счёт"})),
        _completion(call=("mail_read", {"action": "read", "uid": "7"})),
        _completion(draft),
    )
    out = run_agent([{"role": "user", "content": "Подготовь ответ поставщику на счёт, не отправляй"}],
                    tools=offered, call_tool=call, complete=complete, budget=Budget())
    assert out.final_answer == draft and out.stopped is None
    assert [n for n, _ in call.log] == ["mail_read", "mail_read"]  # reading needs no confirmation
    assert smtp == [] and "\\Seen" not in mailbox[2].flags()[("INBOX", 7)]

    # The model calls mail_send anyway: the loop asks first, and the server still refuses with send_disabled.
    call = _caller(mailbox)
    complete = _scripted(_completion(call=("mail_send", SEND_ARGS)), _completion("Отправка выключена, вот черновик."))
    run_agent([{"role": "user", "content": "Отправь"}], tools=offered, call_tool=call, complete=complete, budget=Budget())
    assert [n for n, _ in call.log] == ["ask_confirmation", "mail_send"]
    assert "send_disabled" in str(complete.seen[-1][-1]) or "выключена" in complete.seen[-1][-1]["content"]
    assert smtp == []


def test_mail_send_never_runs_without_a_way_to_ask(mailbox, smtp, monkeypatch) -> None:
    monkeypatch.setenv("HELI_MAIL_SEND", "1")
    offered = _tools(send=True, confirm=False)
    call = _caller(mailbox)
    out = run_agent([{"role": "user", "content": "Отправь"}], tools=offered, call_tool=call,
                    complete=_scripted(_completion(call=("mail_send", SEND_ARGS))), budget=Budget())
    assert call.log == [] and smtp == [] and out.stopped == "confirm"


def test_mail_send_without_approval_asks_and_a_refusal_sends_nothing(mailbox, smtp, monkeypatch) -> None:
    monkeypatch.setenv("HELI_MAIL_SEND", "1")
    offered = _tools(send=True)
    call = _caller(mailbox, confirm="reject")
    out = run_agent([{"role": "user", "content": "Ответь поставщику"}], tools=offered, call_tool=call,
                    complete=_scripted(_completion(call=("mail_send", SEND_ARGS))), budget=Budget())
    assert [n for n, _ in call.log] == ["ask_confirmation"]
    assert call.log[0][1]["action"] == "mail_send" and "supply@example.com" in call.log[0][1]["details"]
    assert smtp == [] and out.stopped == "confirm"


def test_mail_send_runs_after_approval_once(mailbox, smtp, monkeypatch) -> None:
    monkeypatch.setenv("HELI_MAIL_SEND", "1")
    offered = _tools(send=True)
    call = _caller(mailbox)
    complete = _scripted(
        _completion(call=("ask_confirmation", {"action": "mail_send", "details": "Кому: supply@example.com"})),
        _completion(call=("mail_send", SEND_ARGS)),
        _completion("Отправил."),
    )
    out = run_agent([{"role": "user", "content": "Ответь поставщику"}], tools=offered, call_tool=call,
                    complete=complete, budget=Budget())
    assert [n for n, _ in call.log] == ["ask_confirmation", "mail_send"] and out.stopped is None
    assert len(smtp) == 1 and smtp[0]["To"] == "supply@example.com" and smtp[0]["Subject"] == "Re: Счёт №15"


def test_one_approval_is_one_letter_and_other_approvals_do_not_count(mailbox, smtp, monkeypatch) -> None:
    monkeypatch.setenv("HELI_MAIL_SEND", "1")
    offered = _tools(send=True)
    answers = iter(["approved", "approved", "rejected: нет"])
    log: list[str] = []
    session, user, server = mailbox

    def call(call_id, name, args):
        log.append(name if name != "ask_confirmation" else f"ask:{args.get('action')}")
        if name == "ask_confirmation":
            return ToolResult(next(answers))
        result = mail.execute(session, user, None, name, args, connector=_connector(server))
        return ToolResult(result.content, result.error)

    complete = _scripted(
        # An approval for another action does not cover a letter: the loop asks about mail_send itself.
        _completion(call=("ask_confirmation", {"action": "telegram_send", "details": "в чат"})),
        _completion(call=("mail_send", SEND_ARGS)),
        # The approval was used up by the first letter: the second one is asked again and refused.
        _completion(call=("mail_send", {**SEND_ARGS, "subject": "Ещё одно"})),
    )
    out = run_agent([{"role": "user", "content": "x"}], tools=offered, call_tool=call, complete=complete, budget=Budget())
    assert log == ["ask:telegram_send", "ask:mail_send", "mail_send", "ask:mail_send"]
    assert len(smtp) == 1 and out.stopped == "confirm"


# --- guard coverage and prompt -----------------------------------------------------

READ_TOOLS = ["web_search", "open_url", "fetch_page", "read_page", "mail_read", "mail_search", "knowledge_search",
              "http_request", "direct_api", "metrika_read", "bitrix24_read", "moysklad_read", "browser_read",
              "browser_open"]


@pytest.mark.parametrize("name", READ_TOOLS)
def test_reading_tools_need_no_confirmation(name) -> None:
    assert not Guard().needs_approval(name, {"url": "https://example.com", "action": "read"})


def test_every_send_tool_needs_confirmation(monkeypatch) -> None:
    monkeypatch.setenv("HELI_MAIL_SEND", "1")
    monkeypatch.setattr(tools, "_CLIENT", dict(tools._CLIENT))
    monkeypatch.setattr(executors, "_EXECUTORS", dict(executors._EXECUTORS))
    mail.register()
    offered = {t["function"]["name"]: t for t in tools.tools_for(tools.client_tool_names())}
    assert "mail_send" in offered and Guard(schemas=offered).needs_approval("mail_send", SEND_ARGS)
    for name in offered:
        if name.endswith("_send"):
            assert risky_tool(name, offered[name]), name
        if name in READ_TOOLS:
            assert not risky_tool(name, offered[name]), name
    # The eval world counts the same actions as dangerous, so evals and prod agree on what needs a «yes».
    from evals.world import DANGEROUS

    assert all(risky_tool(name) for name in DANGEROUS)


def test_prompt_has_the_short_mail_rules_only_with_mail_tools() -> None:
    from app.agent.task_run import system_prompt

    with_mail = system_prompt([mail.READ_TOOL, tools._CLIENT["ask_confirmation"]])
    without = system_prompt([tools._CLIENT["ask_confirmation"]])
    assert mail.MAIL_HINT in with_mail and mail.MAIL_HINT not in without
    assert "черновик" in mail.MAIL_HINT and "непрочитанными" in mail.MAIL_HINT and "ask_confirmation" in mail.MAIL_HINT
    assert len(mail.MAIL_HINT) < 400
