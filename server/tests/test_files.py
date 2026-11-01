"""Files between the cloud computer and the user's PC: store, API, tools, cloud browser."""

from __future__ import annotations

import base64
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

import pytest

from app import cloud_browser as cb
from app import files as hf
from app.agent import executors

KEY = base64.b64encode(b"k" * 32).decode()


@pytest.fixture
def store(tmp_path):
    value = hf.FileStore(tmp_path / "files", max_bytes=1000, account_bytes=2500)
    hf.set_store(value)
    yield value
    hf.set_store(None)


def _user():
    return type("U", (), {"id": uuid.uuid4()})()


# --- names and the store ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("отчёт.pdf", "отчёт.pdf"),
        ("../../etc/passwd", "passwd"),
        ("C:\\Windows\\System32\\evil.dll", "evil.dll"),
        ('a<b>c:"d|e?f*.txt', "a_b_c__d_e_f_.txt"),
        ("CON.txt", "_CON.txt"),
        ("lpt1", "_lpt1"),
        ("  .hidden. ", "hidden"),
        ("", "файл"),
        ("x" * 300 + ".xlsx", "x" * 115 + ".xlsx"),
    ],
)
def test_safe_name(raw, expected):
    assert hf.safe_name(raw) == expected


def test_risky_extensions():
    assert hf.is_risky("setup.EXE") and hf.is_risky("run.ps1") and hf.is_risky("a.lnk")
    assert not hf.is_risky("отчёт.pdf") and not hf.is_risky("цены.xlsx")


def test_store_round_trip_limits_and_owner(store):
    a, b = uuid.uuid4(), uuid.uuid4()
    info = store.put_bytes(a, "../счёт.pdf", b"%PDF-1", hf.FROM_PC)
    assert info.name == "счёт.pdf" and info.size == 6
    got, blob = store.get(a, info.id)
    assert blob.read_bytes() == b"%PDF-1"
    assert oct(os.stat(blob).st_mode & 0o777) == "0o600"
    with pytest.raises(hf.FileError):
        store.get(b, info.id)  # another account
    with pytest.raises(hf.FileError):
        store.get(a, "../../etc/passwd")
    with pytest.raises(hf.FileError) as too_big:
        store.put_bytes(a, "big.bin", b"x" * 1001, hf.FROM_PC)
    assert too_big.value.code == "file_too_large"
    with pytest.raises(hf.FileError):
        store.put_stream(a, "big.bin", iter([b"x" * 600, b"x" * 600]), hf.FROM_PC)
    assert [item.id for item in store.list(a)] == [info.id]  # nothing half-written left


def test_quota_drops_delivered_files_first_and_refuses_when_full(store):
    user = uuid.uuid4()
    first = store.put_bytes(user, "1.txt", b"a" * 900, hf.TO_PC)
    store.put_bytes(user, "2.txt", b"b" * 900, hf.TO_PC)
    with pytest.raises(hf.FileError) as full:
        store.put_bytes(user, "3.txt", b"c" * 900, hf.TO_PC)
    assert full.value.code == "storage_full"
    store.mark_delivered(user, first.id)
    store.put_bytes(user, "3.txt", b"c" * 900, hf.TO_PC)
    assert [item.name for item in store.list(user)] == ["2.txt", "3.txt"]


def test_files_expire(store, monkeypatch):
    user = uuid.uuid4()
    info = store.put_bytes(user, "old.txt", b"x", hf.TO_PC)
    real = time.time
    monkeypatch.setattr(hf.time, "time", lambda: real() + hf.TTL_S + 1)
    assert store.list(user) == []
    with pytest.raises(hf.FileError):
        store.get(user, info.id)


def test_read_text_utf8_cp1251_and_binary(store):
    user = uuid.uuid4()
    utf = store.put_bytes(user, "a.csv", "\ufeffИНН;Сумма\n7700;10".encode(), hf.FROM_PC)
    win = store.put_bytes(user, "b.txt", "Привет".encode("cp1251"), hf.FROM_PC)
    pdf = store.put_bytes(user, "c.pdf", b"%PDF\x00\x01\x02", hf.FROM_PC)
    assert store.read_text(user, utf.id).startswith("ИНН;Сумма")
    assert store.read_text(user, win.id) == "Привет"
    with pytest.raises(hf.FileError):
        store.read_text(user, pdf.id)


# --- the model's tools -----------------------------------------------------------------


def test_save_file_goes_to_the_pc_and_is_announced(store, monkeypatch):
    user, events = _user(), []
    monkeypatch.setattr(hf, "_publisher", lambda task_id, user_id: events.append)
    result = hf.execute(None, user, uuid.uuid4(), "save_file", {"name": "цены.csv", "content": "Тариф;Цена\nБазовый;599"})
    assert result.error is None and "Загрузки/Heli" in result.content
    (info,) = store.list(user.id, hf.TO_PC)
    assert store.get(user.id, info.id)[1].read_bytes().startswith(b"\xef\xbb\xbf")  # Excel sees Cyrillic
    assert events == [{"type": "file", "id": info.id, "name": "цены.csv", "size": info.size, "risky": False}]
    assert hf.execute(None, user, None, "save_file", {"name": "x.txt"}).error == "bad_args"


def test_read_file_only_reads_attached_files(store):
    user = _user()
    attached = store.put_bytes(user.id, "список.txt", "ООО Ромашка".encode(), hf.FROM_PC)
    downloaded = store.put_bytes(user.id, "скачано.txt", b"x", hf.TO_PC)
    assert "ООО Ромашка" in hf.execute(None, user, None, "read_file", {"file_id": attached.id}).content
    assert hf.execute(None, user, None, "read_file", {"file_id": downloaded.id}).error == "not_found"
    assert hf.execute(None, _user(), None, "read_file", {"file_id": attached.id}).error == "not_found"


def test_attachments_note_lists_only_own_attached_files(store):
    user = uuid.uuid4()
    info = store.put_bytes(user, "договор.pdf", b"%PDF", hf.FROM_PC)
    note = hf.attachments_note(user, [info.id, str(uuid.uuid4()), "junk"])
    assert f"договор.pdf (4 Б), file_id={info.id}" in note and "browser_upload" in note
    assert hf.attachments_note(uuid.uuid4(), [info.id]) == ""


def test_register_adds_server_tools(store):
    from app.agent.tools import client_tool_names, tools_for

    hf.register()
    assert executors.executor_for("save_file") is hf.execute
    assert executors.executor_for("read_file") is hf.execute
    assert {"save_file", "read_file", "browser_upload"} <= client_tool_names()
    offered = {tool["function"]["name"] for tool in tools_for(["save_file"])}
    assert "save_file" in offered and "browser_upload" not in offered


def test_system_prompt_mentions_the_heli_folder():
    from app.agent.task_run import system_prompt

    text = system_prompt([hf.SAVE_TOOL])
    assert "Загрузки/Heli" in text and "не запускай" in text


# --- cloud browser: downloads and uploads ----------------------------------------------


class FakeDownload:
    def __init__(self, name: str, data: bytes, fail: str | None = None) -> None:
        self.suggested_filename = name
        self.data = data
        self.fail = fail
        self.deleted = False

    def failure(self):
        return self.fail

    def save_as(self, path):
        with open(path, "wb") as out:
            out.write(self.data)

    def delete(self):
        self.deleted = True


def _fake_browser(tmp_path, monkeypatch):
    from tests.test_cloud_browser import FakeLauncher

    monkeypatch.setenv("VAULT_MASTER_KEY", KEY)
    launcher = FakeLauncher()
    pool = cb.CloudBrowser(launcher=launcher, max_sessions=2, store=cb.ProfileStore(tmp_path / "profiles"))
    return pool, launcher


def test_downloads_in_the_cloud_browser_go_to_the_store(store, tmp_path, monkeypatch):
    pool, launcher = _fake_browser(tmp_path, monkeypatch)
    user = uuid.uuid4()
    pool.call(user, "browser_read", {})
    page = launcher.pages[0]
    page.downloads = [
        FakeDownload("Счёт №12.pdf", b"%PDF-1.4"),
        FakeDownload("huge.zip", b"x" * 2000),
        FakeDownload("broken.xlsx", b"", fail="canceled"),
    ]
    result = pool.call(user, "browser_click", {"text": "Скачать"})
    assert "Файл «Счёт №12.pdf» (8 Б) отправлен на компьютер пользователя" in result.text
    assert "«huge.zip» не передан пользователю: больше" in result.text
    assert "Скачать «broken.xlsx» не удалось." in result.text
    assert result.text.endswith("текст")  # the page is still read back
    assert [info.name for info in result.files] == ["Счёт №12.pdf"]
    assert [item.name for item in store.list(user, hf.TO_PC)] == ["Счёт №12.pdf"]
    assert page.downloads == []


def test_executor_publishes_file_events_and_uploads_attached_files(store, tmp_path, monkeypatch):
    pool, launcher = _fake_browser(tmp_path, monkeypatch)
    cb.set_pool(pool)
    try:
        user, events = _user(), []
        run = type("R", (), {"publish": staticmethod(events.append), "control": type("C", (), {"stopped": False})()})()
        monkeypatch.setattr(cb, "_run_for", lambda task_id, user_id: run)
        cb.execute(None, user, uuid.uuid4(), "browser_read", {})
        page = launcher.pages[0]
        page.downloads = [FakeDownload("акт.pdf", b"%PDF")]
        cb.execute(None, user, uuid.uuid4(), "browser_click", {"ref": 2})
        assert events[-2]["type"] == "file" and events[-2]["name"] == "акт.pdf"
        assert events[-1]["type"] == "screen"

        uploaded: list = []
        page.upload = lambda payload, ref, text: (uploaded.append((payload, ref)), "Файл выбран")[1]
        attached = store.put_bytes(user.id, "скан паспорта.jpg", b"\xff\xd8jpg", hf.FROM_PC)
        result = cb.execute(None, user, uuid.uuid4(), "browser_upload", {"file_id": attached.id, "ref": 5})
        assert result.error is None
        payload, ref = uploaded[0]
        assert payload == {"name": "скан паспорта.jpg", "mimeType": "image/jpeg", "buffer": b"\xff\xd8jpg"}
        assert ref == 5
        # A downloaded file is not an attachment; another account's file does not exist.
        downloaded = store.list(user.id, hf.TO_PC)[0]
        assert cb.execute(None, user, None, "browser_upload", {"file_id": downloaded.id}).error == "file_not_found"
        assert cb.execute(None, _user(), None, "browser_upload", {"file_id": attached.id}).error == "file_not_found"
    finally:
        cb.set_pool(None)


# --- API ------------------------------------------------------------------------------


def _headers(client, monkeypatch) -> dict:
    monkeypatch.setenv("HELI_OPEN_ACCESS", "1")
    token = client.post("/v1/auth/open").json()["access_token"]
    return {"authorization": f"Bearer {token}"}


def test_api_upload_list_download_deliver(client, store, monkeypatch):
    headers = _headers(client, monkeypatch)
    up = client.post(
        "/v1/files", content="ИНН;Сумма".encode(), headers={**headers, "x-file-name": quote("реестр.csv")}
    )
    assert up.status_code == 201, up.text
    assert up.json()["name"] == "реестр.csv" and up.json()["direction"] == "from_pc"
    assert client.post("/v1/files", content=b"x" * 1001, headers={**headers, "x-file-name": "a"}).status_code == 413
    assert client.post("/v1/files", content=b"", headers={**headers, "x-file-name": "a"}).status_code == 400
    assert client.post("/v1/files", content=b"x").status_code == 401

    user_id = uuid.UUID(client.get("/v1/me", headers=headers).json()["id"])
    out = store.put_bytes(user_id, "отчёт.pdf", b"%PDF", hf.TO_PC)
    risky = store.put_bytes(user_id, "setup.exe", b"MZ", hf.TO_PC)
    pending = client.get("/v1/files?pending=true", headers=headers).json()["files"]
    assert [(item["name"], item["risky"]) for item in pending] == [("отчёт.pdf", False), ("setup.exe", True)]

    content = client.get(f"/v1/files/{out.id}/content", headers=headers)
    assert content.content == b"%PDF"
    assert content.headers["content-type"] == "application/octet-stream"
    assert "attachment" in content.headers["content-disposition"]
    assert client.post(f"/v1/files/{out.id}/delivered", headers=headers).json()["delivered"] is True
    assert [item["id"] for item in client.get("/v1/files?pending=true", headers=headers).json()["files"]] == [risky.id]
    assert client.delete(f"/v1/files/{risky.id}", headers=headers).json() == {"deleted": True}
    assert client.get(f"/v1/files/{risky.id}/content", headers=headers).status_code == 404
    assert client.get(f"/v1/files/{uuid.uuid4()}/content", headers=headers).status_code == 404


def test_task_context_files_reach_the_prompt(client, session, store, monkeypatch):
    from app.models import Task

    headers = _headers(client, monkeypatch)
    up = client.post("/v1/files", content=b"1;2", headers={**headers, "x-file-name": "t.csv"}).json()
    created = client.post("/v1/tasks", json={"request": "Загрузи файл", "context": {"files": [up["id"]]}}, headers=headers)
    assert created.status_code == 201
    task = session.get(Task, uuid.UUID(created.json()["id"]))
    assert task.context == {"files": [up["id"]]}
    assert f"t.csv (3 Б), file_id={up['id']}" in hf.attachments_note(task.user_id, task.context["files"])


# --- real Chromium -------------------------------------------------------------------

UPLOAD_PAGE = """<!doctype html><html><head><title>Файлы</title></head><body>
<a href="/report.csv" download="отчёт.csv">Скачать отчёт</a>
<label for="f" style="padding:8px;border:1px solid">Выбрать файл</label>
<input id="f" type="file" style="display:none"
  onchange="this.files[0].text().then(t => document.title = 'Загружен:' + this.files[0].name + ':' + t)">
</body></html>"""


@pytest.mark.skipif(
    not os.path.exists("/opt/pw-browsers/chromium-1243/chrome-linux64/chrome"), reason="Playwright Chromium not installed"
)
def test_real_chromium_download_and_upload(store, tmp_path, monkeypatch):
    pytest.importorskip("playwright.sync_api")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/report.csv":
                body = "Тариф;Цена\n".encode()
                self.send_response(200)
                self.send_header("content-type", "text/csv")
            else:
                body = UPLOAD_PAGE.encode()
                self.send_response(200)
                self.send_header("content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    monkeypatch.setenv("VAULT_MASTER_KEY", KEY)
    monkeypatch.setenv("CLOUD_BROWSER_EXECUTABLE", "/opt/pw-browsers/chromium-1243/chrome-linux64/chrome")
    monkeypatch.setattr(cb, "check_url", lambda u, **kw: u)
    monkeypatch.setattr(cb, "request_allowed", lambda u, resolver=None: True)
    pool = cb.CloudBrowser(store=cb.ProfileStore(tmp_path / "profiles"))
    user = uuid.uuid4()
    try:
        assert pool.call(user, "browser_open", {"url": url}).error is None
        clicked = pool.call(user, "browser_click", {"text": "Скачать отчёт"})
        assert "Файл «отчёт.csv»" in clicked.text, clicked.text
        (info,) = store.list(user, hf.TO_PC)
        assert store.get(user, info.id)[1].read_text() == "Тариф;Цена\n"

        direct = pool.call(user, "browser_open", {"url": url + "report.csv"})
        assert direct.error is None and "отправлен на компьютер" in direct.text, direct.text
        assert "браузер его скачал" in direct.text and "Адрес:" not in direct.text

        payload = {"name": "реестр.txt", "mimeType": "text/plain", "buffer": "ок".encode()}
        uploaded = pool.call(user, "browser_upload", {"_payload": payload, "text": "Выбрать файл"})
        assert "Заголовок: Загружен:реестр.txt:ок" in uploaded.text, uploaded.text
    finally:
        pool.stop(user)
        server.shutdown()
