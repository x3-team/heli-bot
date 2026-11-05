"""Agent tools for logins: keep a password in the vault instead of printing it.

- save_credential(site, username, password?) stores the login (a strong password is made up when none is
  given) and returns the placeholder <CREDENTIAL:uuid>. The model types the placeholder with browser_type;
  the server swaps in the real password only on that site (app.skills.login.resolve_secret_args).
- find_credential(site) returns the placeholder of a login the user already has for the site.

The model, the step stream and the trajectory see only the placeholder. The user finds the password in
«Доступы» (GET /v1/credentials lists them; the secret itself never leaves the vault through the API).
"""

from __future__ import annotations

import logging
import secrets
import string

from app.agent.control import ClientResult

log = logging.getLogger(__name__)


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
    "save_credential",
    "Сохранить логин и пароль сайта в защищённое хранилище «Доступы». Нужен при регистрации или когда "
    "пользователь дал пароль в задаче. Без password придумает надёжный пароль сам. Возвращает плейсхолдер "
    "<CREDENTIAL:…>: вводи его через browser_type в поле пароля, сервер подставит настоящий пароль только "
    "на этом сайте. Сам пароль никогда не печатай в ответе и не вводи его открытым текстом.",
    {
        "site": {"type": "string", "description": "Адрес сайта, например online.moysklad.ru"},
        "username": {"type": "string", "description": "Логин или email, как в задаче"},
        "password": {"type": "string", "description": "Пароль из задачи, если он дан. Иначе не указывай"},
    },
    ["site", "username"],
)
FIND_TOOL = _fn(
    "find_credential",
    "Найти сохранённый логин пользователя для сайта. Возвращает логин и плейсхолдер пароля <CREDENTIAL:…> "
    "для browser_type, или что доступа нет.",
    {"site": {"type": "string", "description": "Адрес сайта или текущей страницы"}},
    ["site"],
)
TOOL_NAMES = ("save_credential", "find_credential")

CREDENTIALS_HINT = (
    " Пароли храни только в «Доступах»: при регистрации или если пароль дан в задаче вызови save_credential "
    "и вводи в поле пароля полученный плейсхолдер <CREDENTIAL:…>. Для входа сначала вызови find_credential. "
    "В ответе пользователю пароль не пиши: скажи, что он сохранён в «Доступах»."
)

_ALPHABET = string.ascii_letters + string.digits


def make_password(length: int = 16) -> str:
    """Letters, digits and one sign: passes the usual «8+ chars, a digit, a capital» rules."""
    while True:
        body = "".join(secrets.choice(_ALPHABET) for _ in range(length - 1))
        if any(c.isdigit() for c in body) and any(c.isupper() for c in body) and any(c.islower() for c in body):
            return body + secrets.choice("!#%*")


def execute(session, user, task_id, name: str, args: dict) -> ClientResult:
    """app.agent.executors contract."""
    from app.skills.hosts import host_of
    from app.skills.login import credential_for, placeholder
    from app.vault import VaultError, store_credential

    site = host_of(str(args.get("site") or ""))
    if not site:
        return ClientResult("Ошибка: укажи адрес сайта", "bad_args")
    if name == "find_credential":
        found = credential_for(session, user, url=f"https://{site}")
        if found is None:
            return ClientResult(f"Сохранённого доступа для {site} нет.")
        return ClientResult(
            f"Доступ для {found['site']}: логин {found['username']}, пароль {placeholder(found['id'])} "
            "(вводи плейсхолдер через browser_type в поле пароля)."
        )
    if name == "save_credential":
        username = str(args.get("username") or "").strip()
        if not username:
            return ClientResult("Ошибка: укажи логин или email", "bad_args")
        given = args.get("password")
        password = given.strip() if isinstance(given, str) and given.strip() and "<CREDENTIAL" not in given else ""
        made_up = not password
        try:
            row = store_credential(session, user, site, username, password or make_password())
            session.commit()
        except VaultError as exc:
            session.rollback()
            log.warning("save_credential failed code=%s", exc.code)
            return ClientResult(
                "Ошибка: хранилище паролей недоступно. Не печатай пароль в ответе: скажи пользователю, "
                "что пароль нужно сохранить в «Доступах» вручную.",
                exc.code,
            )
        note = "Пароль придуман и сохранён" if made_up else "Пароль сохранён"
        return ClientResult(
            f"{note} в «Доступах» для {site} (логин {username}). В поле пароля вводи {placeholder(row.id)} — "
            "сервер подставит настоящий пароль только на этом сайте. В ответе пароль не пиши."
        )
    return ClientResult("Ошибка: unknown_tool", "unknown_tool")


def register() -> None:
    from app.agent.executors import register_executor
    from app.agent.tools import register_client_tool

    register_client_tool(SAVE_TOOL)
    register_client_tool(FIND_TOOL)
    register_executor(TOOL_NAMES, execute)
