"""Files between Heli's cloud computer and the user's PC.

Two directions, one store per account on the server:

- to_pc: a file the agent downloaded in the cloud browser or wrote with save_file. The app
  takes it from GET /v1/files/{id}/content and puts it in Загрузки/Heli on the user's PC.
- from_pc: a file the user attached in the chat (POST /v1/files). The agent can read it
  (read_file) or upload it to a site in the cloud browser (browser_upload).

Nothing here runs a file. Names are cleaned (no paths, no Windows-reserved names), each file
is capped at FILES_MAX_MB, an account keeps at most FILES_ACCOUNT_MB and files expire after a day.
Content sits on disk next to a small JSON with the metadata; there is no database table.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import unicodedata
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from app.config import get_settings

TTL_S = 24 * 60 * 60
TEXT_LIMIT = 20_000
SAVE_TEXT_MAX = 1_000_000

TO_PC = "to_pc"
FROM_PC = "from_pc"

_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
# Opening these runs code. The app never opens them itself, only shows them in the folder.
RISKY_EXTENSIONS = {
    "exe", "msi", "msix", "msp", "appx", "appxbundle", "bat", "cmd", "com", "scr", "pif", "cpl", "ps1", "psm1",
    "ps1xml", "vbs", "vbe", "js", "jse", "wsf", "wsh", "hta", "lnk", "url", "scf", "reg", "inf", "jar", "msc",
    "dll", "sys", "gadget", "application", "chm", "sh", "command", "app", "appimage", "deb", "rpm", "pkg", "dmg",
    "iso", "img", "vhd", "vhdx",
}
TEXT_EXTENSIONS = {"txt", "csv", "tsv", "md", "json", "xml", "html", "htm", "log", "ini", "yaml", "yml"}


class FileError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class FileInfo:
    id: str
    name: str
    size: int
    direction: str
    created: float
    delivered: bool = False
    task_id: str | None = None

    def public(self) -> dict:
        data = asdict(self)
        data["risky"] = is_risky(self.name)
        return data


def safe_name(raw: str, fallback: str = "файл") -> str:
    """A file name that is safe on Windows, macOS and Linux: no folders, no reserved names."""
    name = unicodedata.normalize("NFC", str(raw or ""))
    name = name.replace("\\", "/").split("/")[-1]
    name = _BAD_CHARS.sub("_", name).strip().strip(".").strip()
    name = re.sub(r"\s+", " ", name)
    if not name:
        name = fallback
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    if stem.split(".")[0].lower() in _RESERVED:
        stem = "_" + stem
    ext = ext[:16]
    stem = stem[: 120 - len(ext) - 1] if ext else stem[:120]
    return f"{stem}.{ext}" if ext else stem


def extension(name: str) -> str:
    _, dot, ext = name.rpartition(".")
    return ext.lower() if dot else ""


def is_risky(name: str) -> bool:
    return extension(name) in RISKY_EXTENSIONS


def human_size(size: int) -> str:
    if size < 1024:
        return f"{size} Б"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} КБ"
    return f"{size / 1024 / 1024:.1f} МБ"


class FileStore:
    def __init__(self, root: Path | str | None = None, max_bytes: int | None = None, account_bytes: int | None = None):
        settings = get_settings()
        self.root = Path(root or settings.files_dir)
        self.max_bytes = max_bytes if max_bytes is not None else settings.files_max_mb * 1024 * 1024
        self.account_bytes = account_bytes if account_bytes is not None else settings.files_account_mb * 1024 * 1024
        self._lock = threading.Lock()

    # --- paths ---------------------------------------------------------------------

    def _dir(self, user_id) -> Path:
        return self.root / str(uuid.UUID(str(user_id)))

    def _paths(self, user_id, file_id: str) -> tuple[Path, Path]:
        try:
            fid = str(uuid.UUID(str(file_id)))
        except ValueError:
            raise FileError("not_found") from None
        base = self._dir(user_id)
        return base / f"{fid}.bin", base / f"{fid}.json"

    # --- writes --------------------------------------------------------------------

    def put_bytes(self, user_id, name: str, data: bytes, direction: str, task_id=None) -> FileInfo:
        if len(data) > self.max_bytes:
            raise FileError("file_too_large")
        return self._put(user_id, name, direction, task_id, lambda path: path.write_bytes(data))

    def put_path(self, user_id, name: str, source: Path | str, direction: str, task_id=None) -> FileInfo:
        """Move a finished download into the store (it is deleted from `source`)."""
        source = Path(source)
        if source.stat().st_size > self.max_bytes:
            source.unlink(missing_ok=True)
            raise FileError("file_too_large")
        return self._put(user_id, name, direction, task_id, lambda path: shutil.move(str(source), path))

    def put_stream(self, user_id, name: str, chunks, direction: str, task_id=None) -> FileInfo:
        """Write chunks as they come; stop as soon as the limit is passed."""

        def write(path: Path) -> None:
            total = 0
            with open(path, "wb") as out:
                for chunk in chunks:
                    total += len(chunk)
                    if total > self.max_bytes:
                        raise FileError("file_too_large")
                    out.write(chunk)

        return self._put(user_id, name, direction, task_id, write)

    def _put(self, user_id, name: str, direction: str, task_id, write) -> FileInfo:
        if direction not in (TO_PC, FROM_PC):
            raise FileError("bad_direction")
        base = self._dir(user_id)
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = FileInfo(
            id=str(uuid.uuid4()),
            name=safe_name(name),
            size=0,
            direction=direction,
            created=time.time(),
            task_id=str(task_id) if task_id else None,
        )
        blob, meta = self._paths(user_id, info.id)
        try:
            write(blob)
        except BaseException:
            blob.unlink(missing_ok=True)
            raise
        os.chmod(blob, 0o600)
        info.size = blob.stat().st_size
        if info.size > self.max_bytes:
            blob.unlink(missing_ok=True)
            raise FileError("file_too_large")
        with self._lock:
            self._sweep(user_id)
            if self._used(user_id) > self.account_bytes:
                blob.unlink(missing_ok=True)
                raise FileError("storage_full")
            self._write_meta(meta, info)
        return info

    def _write_meta(self, meta: Path, info: FileInfo) -> None:
        tmp = meta.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(info), ensure_ascii=False))
        os.chmod(tmp, 0o600)
        tmp.replace(meta)

    def mark_delivered(self, user_id, file_id: str) -> FileInfo:
        info, _ = self.get(user_id, file_id)
        info.delivered = True
        with self._lock:
            self._write_meta(self._paths(user_id, file_id)[1], info)
        return info

    def delete(self, user_id, file_id: str) -> None:
        blob, meta = self._paths(user_id, file_id)
        if not meta.exists():
            raise FileError("not_found")
        blob.unlink(missing_ok=True)
        meta.unlink(missing_ok=True)

    # --- reads ---------------------------------------------------------------------

    def get(self, user_id, file_id: str) -> tuple[FileInfo, Path]:
        blob, meta = self._paths(user_id, file_id)
        info = self._read_meta(meta)
        if info is None or not blob.exists() or time.time() - info.created > TTL_S:
            raise FileError("not_found")
        return info, blob

    def list(self, user_id, direction: str | None = None) -> list[FileInfo]:
        base = self._dir(user_id)
        if not base.exists():
            return []
        out = []
        for meta in base.glob("*.json"):
            info = self._read_meta(meta)
            if info is None or time.time() - info.created > TTL_S:
                continue
            if direction and info.direction != direction:
                continue
            out.append(info)
        return sorted(out, key=lambda item: item.created)

    def read_text(self, user_id, file_id: str, limit: int = TEXT_LIMIT) -> str:
        info, blob = self.get(user_id, file_id)
        raw = blob.read_bytes()[: limit * 4]
        if b"\x00" in raw[:4096] and extension(info.name) not in TEXT_EXTENSIONS:
            raise FileError("not_text")
        for encoding in ("utf-8-sig", "cp1251"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise FileError("not_text")
        return text[:limit]

    @staticmethod
    def _read_meta(meta: Path) -> FileInfo | None:
        try:
            return FileInfo(**json.loads(meta.read_text()))
        except Exception:  # noqa: BLE001
            return None

    # --- housekeeping --------------------------------------------------------------

    def _used(self, user_id) -> int:
        base = self._dir(user_id)
        return sum(path.stat().st_size for path in base.glob("*.bin")) if base.exists() else 0

    def _sweep(self, user_id) -> None:
        """Drop expired files; then the oldest delivered ones while the account is over its quota."""
        base = self._dir(user_id)
        now = time.time()
        items = []
        for meta in base.glob("*.json"):
            info = self._read_meta(meta)
            blob = meta.with_suffix(".bin")
            if info is None or now - info.created > TTL_S:
                blob.unlink(missing_ok=True)
                meta.unlink(missing_ok=True)
                continue
            items.append((info, blob, meta))
        used = self._used(user_id)
        for info, blob, meta in sorted(items, key=lambda item: item[0].created):
            if used <= self.account_bytes:
                break
            if info.delivered or info.direction == FROM_PC:
                used -= blob.stat().st_size if blob.exists() else 0
                blob.unlink(missing_ok=True)
                meta.unlink(missing_ok=True)


_store: FileStore | None = None
_store_lock = threading.Lock()


def store() -> FileStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = FileStore()
        return _store


def set_store(value: FileStore | None) -> None:
    """Tests point the store at a temporary folder."""
    global _store
    with _store_lock:
        _store = value


# --- what the model sees -------------------------------------------------------------

def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


SAVE_TOOL = _fn(
    "save_file",
    "Положить файл на компьютер пользователя (папка Загрузки/Heli). Для текстовых результатов: таблица CSV, "
    "текст, Markdown, JSON. Файлы, скачанные в браузере, попадают туда сами, их сохранять не нужно.",
    {
        "name": {"type": "string", "description": "Имя файла с расширением, например «цены.csv»"},
        "content": {"type": "string", "description": "Содержимое файла"},
    },
    ["name", "content"],
)
READ_TOOL = _fn(
    "read_file",
    "Прочитать текстовый файл, который пользователь приложил к задаче (txt, csv, md, json, xml, html).",
    {"file_id": {"type": "string", "description": "id файла из списка приложенных"}},
    ["file_id"],
)
UPLOAD_TOOL = _fn(
    "browser_upload",
    "Загрузить приложенный пользователем файл на сайт в браузере: укажи поле или кнопку выбора файла "
    "(ref из browser_read или видимый текст) и id файла.",
    {
        "file_id": {"type": "string", "description": "id файла из списка приложенных"},
        "ref": {"type": "integer", "description": "Номер поля или кнопки «Выбрать файл»"},
        "text": {"type": "string", "description": "Видимый текст кнопки, если номера нет"},
    },
    ["file_id"],
)

FILES_HINT = (
    " Файлы, которые ты скачал в браузере, сами попадают на компьютер пользователя в папку Загрузки/Heli. "
    "Готовый текстовый результат (таблицу, список) можно положить туда же через save_file. "
    "Сам файлы не запускай и не проси пользователя запускать программы."
)


def attachments_note(user_id, file_ids) -> str:
    """The list the model gets under the user's request."""
    lines = []
    for file_id in list(file_ids or [])[:10]:
        try:
            info, _ = store().get(user_id, str(file_id))
        except FileError:
            continue
        if info.direction != FROM_PC:
            continue
        lines.append(f"- {info.name} ({human_size(info.size)}), file_id={info.id}")
    if not lines:
        return ""
    return (
        "\n\nПользователь приложил файлы. Они лежат на компьютере Heli: текст можно прочитать через read_file, "
        "загрузить на сайт — через browser_upload.\n" + "\n".join(lines)
    )


def published(info: FileInfo) -> dict:
    return {"type": "file", "id": info.id, "name": info.name, "size": info.size, "risky": is_risky(info.name)}


def saved_note(info: FileInfo) -> str:
    return f"Файл «{info.name}» ({human_size(info.size)}) отправлен на компьютер пользователя в папку Загрузки/Heli."


def _publisher(task_id, user_id):
    try:
        from app.agent import runs

        agent_run = runs.registry.get(task_id, user_id)
    except Exception:  # noqa: BLE001
        agent_run = None
    return agent_run.publish if agent_run else (lambda event: None)


def execute(session, user, task_id, name: str, args: dict):
    """app.agent.executors contract for save_file and read_file."""
    from app.agent.control import ClientResult

    if name == "save_file":
        content = args.get("content")
        if not isinstance(content, str):
            return ClientResult("Ошибка: нет содержимого", "bad_args")
        data = content.encode("utf-8")
        if len(data) > SAVE_TEXT_MAX:
            return ClientResult("Ошибка: файл больше 1 МБ", "file_too_large")
        file_name = safe_name(str(args.get("name") or ""), fallback="результат.txt")
        if extension(file_name) in {"csv", "tsv"}:
            data = b"\xef\xbb\xbf" + data  # Excel opens UTF-8 CSV with Cyrillic only with a BOM
        try:
            info = store().put_bytes(user.id, file_name, data, TO_PC, task_id)
        except FileError as exc:
            return ClientResult(f"Ошибка: {exc.code}", exc.code)
        _publisher(task_id, user.id)(published(info))
        return ClientResult(saved_note(info))
    if name == "read_file":
        try:
            info, _ = store().get(user.id, str(args.get("file_id") or ""))
            if info.direction != FROM_PC:
                raise FileError("not_found")
            text = store().read_text(user.id, info.id)
        except FileError as exc:
            if exc.code == "not_text":
                return ClientResult(
                    "Это не текстовый файл. Его можно загрузить на сайт через browser_upload.", exc.code
                )
            return ClientResult(f"Ошибка: {exc.code}", exc.code)
        return ClientResult(f"Файл «{info.name}»:\n\n{text}")
    return ClientResult("Ошибка: unknown_tool", "unknown_tool")


def register() -> None:
    """save_file and read_file run on the server. browser_upload comes with the cloud browser."""
    from app.agent.executors import register_executor
    from app.agent.tools import register_client_tool

    register_client_tool(SAVE_TOOL)
    register_client_tool(READ_TOOL)
    register_client_tool(UPLOAD_TOOL)
    register_executor(("save_file", "read_file"), execute)
