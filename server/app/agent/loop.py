"""The agent loop: ask the model, run the tools it calls, feed results back, repeat.

It knows nothing about the database or HTTP. The caller passes:
- complete(messages, tools) -> Completion   (metered model call)
- call_tool(call_id, name, args) -> ToolResult
- on_event(dict)                            (step stream + trajectory)
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

from app.agent.control import Control, NoControl, Stopped
from app.agent import write_policy
from app.agent.guard import Guard, describe
from app.llm.client import Completion
from app.llm.tools import parse_args
from app.masking import SECRET_KEYS, mask_passwords

TOOL_RESULT_LIMIT = 9000
AUTOPAUSE_RATIO = Decimal("1.5")
CONFIRM_TOOL = "ask_confirmation"
# Training accounts (HELI_TRAINING_EMAILS) work only on training stands: nobody is there to answer a card,
# and a question stops the test run. The loop approves for them, except a live write (policy card).
TRAINING_APPROVED = "approved: учебный аккаунт, подтверждение не требуется."


@dataclass
class Budget:
    max_steps: int = 15
    # Tool calls allowed in the run (None = only max_steps model rounds count).
    max_tool_calls: int | None = None
    max_rub: Decimal | None = None
    estimate_rub: Decimal | None = None
    autopause: bool = True
    # Rounds added once when the step limit comes while the run still makes progress (no loop, money left).
    extend_steps: int = 0
    # Unfamiliar service or topic: study it (knowledge base, official help) before acting or answering.
    study_first: bool = False
    # The user's own words (without memory and knowledge notes): a bare «привет» may be answered in words.
    request: str | None = None


@dataclass
class ToolResult:
    content: str
    error: str | None = None
    data: dict = field(default_factory=dict)


@dataclass
class Outcome:
    final_answer: str
    steps: int
    cost_rub: Decimal
    stopped: str | None
    alias: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


def approved(result: ToolResult) -> bool:
    text = result.content.strip().lower()
    return result.error is None and (text.startswith("approved") or text.startswith('{"approved": true'))


BROKEN_NOTE = (
    "Твой прошлый ответ не распознан: в нём разметка вызова инструмента вместо вызова. "
    "Вызови инструмент обычным способом или ответь пользователю текстом."
)
BUDGET_TEXT = "Остановился: задача дошла до предела расходов на один запуск."
GAVE_UP = "Не успел закончить задачу за отведённые шаги."
CONTINUE_HINT = "Чтобы продолжить с этого места, напишите «продолжи»."
_REPORT = (
    "Больше инструменты не вызывай. Ответь пользователю по-русски, коротко, в три части: "
    "«Сделано:» что уже сделано и найдено (конкретные цифры, номера документов, источники); "
    "«Не сделано:» что осталось сделать по задаче; «Где остановился:» раздел или страница. "
    f"Последней строкой напиши: «{CONTINUE_HINT}» Не выдумывай того, чего не видел."
)
FINISH_NOTE = "Лимит действий исчерпан. " + _REPORT
BUDGET_NOTE = "Задача дошла до предела расходов на один запуск. " + _REPORT
STOP_NOTE = "Задачу остановили. " + _REPORT
CAPTCHA_NOTE = (
    "Сайт попросил капчу при входе, и её никто не прошёл: агент капчи не решает. " + _REPORT
    + " В «Не сделано» скажи, что нужно: войти на экране «Компьютер агента» или импортировать сессию, "
    "а затем написать «продолжи»."
)
# The budget's last part is kept for the words-only answer: the run never ends without one.
BUDGET_FINISH_SHARE = Decimal("0.9")


LOOP_NOTE = (
    "Ты несколько раз повторил одни и те же действия с одним и тем же результатом. Больше инструменты "
    "не вызывай: честно скажи пользователю, что не получилось, что именно ты пробовал и что видел, "
    "и что можно сделать дальше."
)
REPEAT_WARNING = (
    "\n\n[Heli] Этот вызов уже второй раз дал тот же результат. Не повторяй его: смени тактику "
    "(другой адрес или элемент, browser_screenshot и клик по координатам, другой раздел) "
    "или закончи и честно скажи, что не получилось."
)
REPEAT_BLOCKED = (
    "Не выполнено: этот вызов уже дважды дал один и тот же результат, повтор ничего не изменит. "
    "Смени тактику или закончи и честно скажи пользователю, что не получилось."
)
# A third identical call is refused. Before giving up the loop tries to unstick the page itself:
# Esc (a modal dialog), then reloading the page. This many refusals end the run with a summary.
BLOCKS_TO_STOP = 3
RECOVER_ESC = (
    "\n\n[Heli] Я нажал Esc, чтобы закрыть возможный диалог или меню. Прочитай страницу заново "
    "(browser_read) и попробуй другой путь: другой раздел или пункт меню, поиск, кнопку «Закрыть» / "
    "«Отмена» у окна, прямой адрес."
)
RECOVER_RELOAD = (
    "\n\n[Heli] Я перезагрузил страницу {url}. Начни с browser_read и иди к цели другим путём, "
    "не тем, что не сработал."
)
# Old tool results are cut to this many characters: only the last few are shown in full.
KEEP_FULL_RESULTS = 3
OLD_RESULT_CHARS = 1200
OLD_RESULT_CUT = "\n…[старый результат сокращён]"
# Screenshots per run: each one sends a round to the paid vision model.
MAX_SCREENSHOTS = 8
SCREENSHOT_LIMIT_TEXT = (
    "Не выполнено: лимит снимков экрана на задачу исчерпан. Работай по тексту страницы (browser_read) "
    "и номерам элементов."
)
# Halfway through the steps the model is asked to check where it is: change the path or finish honestly.
HALFWAY_NOTE = (
    "[Heli] Прошло {done} из {total} шагов. Если цель (нужный список, документ, отчёт или карточка) ещё "
    "не открыта, смени путь: прямая ссылка, другой раздел, «Все функции», поиск в списке. Если и так не "
    "выходит — закончи сейчас честным итогом «Сделано / Не сделано», не трать оставшиеся шаги."
)
# After a save in a system with an API reader, the answer waits for a check through that reader.
VERIFY_TOOL = "moysklad_read"
VERIFY_NOTE = (
    "Прежде чем ответить, проверь через moysklad_read, что документ или карточка действительно "
    "сохранились с нужными полями (товар, количество, контрагент, цена). Затем ответь, что проверил."
)
_SAVE_LABEL = re.compile(r"сохран|провест|записат|создат|save", re.I)
# A form typed into in these systems but never saved: the answer waits for «Сохранить» once.
_FORM_HOSTS = re.compile(r"moysklad\.ru", re.I)
UNSAVED_NOTE = (
    "Ты заполнял поля формы, но не нажал «Сохранить» после последнего ввода: без этого запись не появится. "
    "Нажми «Сохранить», проверь, что ошибок нет и запись сохранилась, и только потом отвечай. "
    "Если сохранять не нужно по задаче, так и напиши."
)
# «Изучи предмет»: in an unfamiliar service the first action or the answer waits for a look at the
# official help or a knowledge note. An empty knowledge_search is not a look: the answer then waits for
# web_search / open_url. The loop asks twice at most; an answer given without any look is marked unverified.
STUDY_TOOLS = {"knowledge_search", "web_search", "open_url", "fetch_page"}
ACT_TOOLS = {
    "http_request", "direct_api", "ga4_send_event", "browser_click", "browser_type", "browser_press", "mail_send",
    "save_credential", "save_file", "use_computer",
}
STUDY_MAX_NUDGES = 2
STUDY_GATE = (
    "Не выполнено: сначала освой инструмент. Сервис или тема незнакомые и в базе знаний их нет: "
    "knowledge_search, затем официальная справка или документация API (web_search, open_url). Выпиши "
    "авторизацию, лимиты и что у сервиса песочница, со ссылкой на источник — и только потом действуй."
)
STUDY_NOTE = (
    "Прежде чем отвечать, изучи предмет: тема незнакомая и в базе знаний её нет. Вызови knowledge_search, "
    "найди официальную справку или документацию (web_search, open_url) и сверь с ней ответ; в ответе сошлись "
    "на источник. Если вопрос не про сервис и не про факты, которые могли измениться, ответь как есть."
)
UNVERIFIED_NOTE = (
    "⚠️ Не сверено с официальной документацией: я не нашёл и не открыл источник, проверьте факты ниже.\n\n"
)
_EMPTY_KNOWLEDGE = ("ничего нет", '"notes": []')


# «Чем могу помочь?» on a real task (dx-api-01, ja-web-01, 07.11.2026): the model greeted and called nothing.
# An answer with no tool call that is only a greeting or an offer of help gets one nudge to do the task.
IDLE_NOTE = (
    "Ты ответил приветствием, но в сообщении пользователя уже есть задача — не спрашивай, чем помочь. "
    "Выполни её сейчас инструментами. Если для неё нужен доступ (токен, вход), всё равно вызови нужный "
    "инструмент: если он ответит, что доступа нет, напиши «BLOCKED:», чего не хватает и что сохранить в "
    "«Доступах». Если задача не требует инструментов, просто ответь на неё по существу."
)
_GREETING = re.compile(
    r"(?<![\w])(?:привет\w*|здравствуй\w*|здрасьте|добр(?:ый|ое|ого)\s+(?:день|утр\w*|вечер\w*|дня|времени\w*)|"
    r"доброго\s+времени\s+суток|хай|салют|hello|hi|hey|good\s+(?:morning|afternoon|evening))(?![\w])",
    re.I,
)
_HELP_OFFER = re.compile(
    r"(?:чем|с\s+чем|как)\s+(?:я\s+)?(?:\w+\s+)?(?:могу|можем|мне|могли\s+бы)?\s*(?:вам\s+|тебе\s+)?помочь\w*|"
    r"чем\s+(?:вам\s+|тебе\s+)?(?:могу\s+)?(?:быть\s+)?полезен\w*|"
    r"(?:какая|какой|что)\s+(?:у\s+вас\s+|у\s+тебя\s+)?(?:задача|вопрос)\w*|"
    r"что\s+(?:нужно|надо|требуется)\s+(?:сделать|выполнить)|"
    r"(?:я\s+)?(?:Heli|Хели)[,\s]+(?:ваш\s+|твой\s+)?(?:универсальн\w+\s+)?помощник\w*|"
    r"готов\w*\s+(?:помочь|к\s+работе|приступить)|"
    r"how\s+can\s+i\s+help(?:\s+you)?(?:\s+today)?",
    re.I,
)
# Small talk the user may get a greeting for: «привет», «как дела?», «кто ты?», «спасибо».
_SMALL_TALK = re.compile(
    r"^(?:как\s+(?:дела|ты|жизнь|поживаешь)|кто\s+ты|что\s+(?:ты\s+)?умеешь|ты\s+тут|ты\s+здесь|"
    r"спасибо|благодарю|ок|окей|ok|понял\w*|ясно|пока|до\s+свидания|тест|test|проверка)$",
    re.I,
)


def _letters(text: str) -> str:
    return re.sub(r"[^\w]+", " ", text).strip()


def is_small_talk(request: str) -> bool:
    """A greeting or small talk with no task in it: words are the right answer."""
    rest = _letters(_GREETING.sub(" ", request or ""))
    rest = re.sub(r"\s+", " ", re.sub(r"(?<![\w])(?:heli|хели)(?![\w])", " ", rest, flags=re.I)).strip()
    if len(rest) < 3 or _SMALL_TALK.match(rest):
        return True
    return len(rest) < 40 and bool(re.search(r"спасиб|благодар|thank", rest, re.I))


def is_idle_answer(text: str) -> bool:
    """Only a greeting and «чем могу помочь?»: no data, no result, no question about the task itself."""
    if not text or len(text) > 400 or not _HELP_OFFER.search(text):
        return False
    if re.search(r"\d|https?://|BLOCKED|не\s+удалось|нет\s+доступа|нет\s+токена", text, re.I):
        return False  # numbers, a link or an honest «can't» are an answer, not a greeting
    if re.search(r"(?<![\w])(?:ещё|еще|anything\s+else)(?![\w])", text, re.I):
        return False  # «чем ещё помочь?» closes an answer that was given
    rest = _letters(_HELP_OFFER.sub(" ", _GREETING.sub(" ", text)))
    return len(rest) < 80


def studied_by(name: str, result: ToolResult) -> bool:
    """Whether this successful call is a look at the subject. A knowledge base with nothing on it is not."""
    if name not in STUDY_TOOLS:
        return False
    if name == "knowledge_search":
        head = result.content[:300].lower()
        return not any(marker in head for marker in _EMPTY_KNOWLEDGE)
    return True


# Looking does not change the page; an action in between makes looking again worthwhile.
_LOOK_TOOLS = ("browser_read", "browser_screenshot")
# Tools that load the same thing again whatever the page shows: same args = same call.
_REOPEN_TOOLS = {"browser_open", "open_url", "web_search"}
SCREEN_NOTE = (
    "Снимок экрана после browser_screenshot ({w}×{h}). Для browser_click по координатам бери x и y "
    "в пикселях этого снимка, от левого верхнего угла."
)
SCREEN_DROPPED = "[снимок экрана был здесь, модель его уже видела]"
SCREEN_SIZE = (1280, 800)

# DeepSeek writes its tool-call markup into the text when it cannot call tools in this round.
_DSML_BLOCK = re.compile(
    r"<[｜|]\s*DSML\s*[｜|]\s*(tool_calls|function_calls)\s*>.*?(?:</[｜|]\s*DSML\s*[｜|]\s*\1\s*>|$)", re.S
)
_TOOL_TOKENS = re.compile(r"<[｜|]tool▁calls▁begin[｜|]>.*?(?:<[｜|]tool▁calls▁end[｜|]>|$)", re.S)
_SPECIAL = re.compile(r"</?[｜|]\s*DSML\s*[｜|][^>]*>|<[｜|][^<>｜|]{1,40}[｜|]>")


def strip_markup(text: str) -> str:
    """The answer without the model's raw tool-call markup."""
    if not text:
        return text
    cleaned = _DSML_BLOCK.sub("", text)
    cleaned = _TOOL_TOKENS.sub("", cleaned)
    cleaned = _SPECIAL.sub("", cleaned)
    return cleaned.strip()


def has_markup(text: str) -> bool:
    return bool(text) and strip_markup(text) != text.strip()


def call_key(name: str, args: dict) -> str:
    """Identity of a call for the cycle check: the tool and all its arguments."""
    clean = {k: (v.strip().lower() if isinstance(v, str) else v) for k, v in args.items() if not k.startswith("_")}
    return name + json.dumps(clean, ensure_ascii=False, sort_keys=True, default=str)


def result_hash(result: "ToolResult") -> str:
    text = re.sub(r"\s+", " ", result.content or "").strip()
    digest = hashlib.sha1(f"{result.error}|{text}".encode())
    shot = result.data.get("screenshot") if isinstance(result.data, dict) else None
    if isinstance(shot, (bytes, bytearray)):
        digest.update(bytes(shot))
    return digest.hexdigest()


def summary_messages(messages: list[dict], note: str, limit: int = 14000) -> list[dict]:
    """The run so far as plain text, without tool calls: any model can answer it with words only.

    Sending the real history without tools makes some models print tool-call markup instead."""
    system = [m for m in messages[:1] if m.get("role") == "system"]
    lines: list[str] = []
    for message in messages[len(system):]:
        role = message.get("role")
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict) and p.get("type") == "text")
        content = strip_markup(str(content or ""))
        if role == "user":
            lines.append(f"Пользователь: {content[:3000]}")
        elif role == "assistant":
            if content:
                lines.append(f"Ты: {content[:1500]}")
            for call in message.get("tool_calls") or []:
                fn = call.get("function", {})
                lines.append(f"Ты вызвал {fn.get('name')}({str(fn.get('arguments', ''))[:300]})")
        elif role == "tool":
            lines.append(f"Результат: {content[:1200]}")
    transcript = "\n".join(lines)
    if len(transcript) > limit:
        transcript = "…\n" + transcript[-limit:]
    return [
        *system,
        {"role": "user", "content": f"Ход работы над задачей:\n{transcript}\n\n{note}"},
    ]


def compact_history(messages: list[dict], keep: int = KEEP_FULL_RESULTS, limit: int = OLD_RESULT_CHARS) -> None:
    """Cut old tool results in place: the model needs the last pages in full, older ones only in short."""
    tool_positions = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    for index in tool_positions[:-keep] if keep else tool_positions:
        content = messages[index].get("content")
        if isinstance(content, str) and len(content) > limit + len(OLD_RESULT_CUT) and not content.endswith(OLD_RESULT_CUT):
            messages[index] = {**messages[index], "content": content[:limit] + OLD_RESULT_CUT}


VISION_NOTE = (
    "Продолжай задачу. Ниже снимок экрана: найди на нём нужное и сделай следующий шаг "
    "(browser_click по координатам x, y этого снимка, browser_type, browser_press) или ответь, если всё готово."
)


def vision_messages(messages: list[dict], limit: int = 8000) -> list[dict]:
    """A short context for the round that looks at a screenshot: the run as text plus the picture.

    The vision model costs more per token than the text model, so it gets the transcript, not the history."""
    images = [m for m in messages if m.get("role") == "user" and isinstance(m.get("content"), list)]
    if not images:
        return messages
    rest = [m for m in messages if m is not images[-1]]
    return [*summary_messages(rest, VISION_NOTE, limit=limit), images[-1]]


def progress_text(messages: list[dict], note: str) -> str:
    """The answer when no model could write one (quota, network): what was done, from the calls themselves."""
    done: list[str] = []
    for message in messages:
        for call in message.get("tool_calls") or [] if message.get("role") == "assistant" else []:
            fn = call.get("function", {})
            args = parse_args(fn.get("arguments") or "{}")
            what = args.get("url") or args.get("text") or args.get("query") or args.get("resource") or ""
            if fn.get("name") == "browser_type":
                what = ""  # typed text may be private
            done.append(f"{fn.get('name')}{(' ' + str(what)[:80]) if what else ''}")
    lines = [note]
    if done:
        lines.append("Сделано шагов: " + str(len(done)) + ". Последние: " + "; ".join(done[-5:]) + ".")
    lines.append(CONTINUE_HINT)
    return "\n".join(lines)


def _address(text: str) -> str | None:
    for line in (text or "").splitlines()[:3]:
        if line.startswith(("Адрес:", "URL:")):
            value = line.split(":", 1)[1].strip()
            if value.startswith("http"):
                return value
    return None


# http_tool.UA_REJECTED_CODE: the service refused the User-Agent, a new header fixes it, not a browser.
UA_REJECTED = "user_agent_rejected"


def repeat_key(name: str, args: dict) -> str | None:
    """Identity of a call for the repeat check: the tool and its text and object arguments (limits and
    counts aside). Objects count: http_request with new headers (a User-Agent the service asked for) or a
    new query is a different call, not a repeat. Browser tools and confirmations are left out: the page
    or the user's answer can change between calls."""
    if name.startswith("browser_") or name == CONFIRM_TOOL:
        return None
    texts: dict[str, str] = {}
    for k, v in args.items():
        if isinstance(v, str):
            texts[k] = v.strip().lower()
        elif isinstance(v, (dict, list)) and v:
            texts[k] = json.dumps(v, ensure_ascii=False, sort_keys=True, default=str).lower()
    return name + json.dumps(texts, ensure_ascii=False, sort_keys=True)


def run_agent(
    messages: list[dict],
    *,
    tools: list[dict],
    call_tool: Callable[[str, str, dict], ToolResult],
    complete: Callable[[list[dict], list[dict] | None], Completion],
    budget: Budget,
    control: Control | None = None,
    on_event: Callable[[dict], None] | None = None,
    start_cost: Decimal = Decimal("0"),
    training: bool = False,
    prepare_args: Callable[[str, dict], dict] | None = None,
) -> Outcome:
    """`training`: a training account. Its own ask_confirmation and the loop's question before a risky
    action are approved without a person; a live write (write_policy card) still asks.
    `prepare_args(name, args)`: fills what the model left out (the only Битрикс24 portal) before the guard
    judges the call, so the guard and the tool see the same arguments."""
    control = control or NoControl()
    emit = on_event or (lambda event: None)
    spent = start_cost
    threshold = budget.estimate_rub * AUTOPAUSE_RATIO if budget.estimate_rub else None
    out = Outcome(final_answer="", steps=0, cost_rub=spent, stopped=None)
    guard = Guard(schemas={t["function"]["name"]: t for t in tools})
    can_ask = CONFIRM_TOOL in guard.schemas
    calls_used = 0
    auto_ids = 0
    failed: dict[str, str] = {}  # same call that already failed -> its error
    seen: dict[str, list[str]] = {}  # call -> hashes of the results it gave
    blocked = 0
    broken_rounds = 0
    finish_note: str | None = None  # set: the next round is the last, words only
    finish_reason: str | None = None
    max_steps = budget.max_steps
    extended = False
    screenshots = 0
    recoveries = 0
    last_url: str | None = None
    saved_unchecked = False  # a «Сохранить» click not yet checked through the API reader
    verify_nudged = False
    halfway_done = budget.max_steps < 10
    typed_unsaved = False  # typed into a form on a _FORM_HOSTS page after the last save
    unsaved_nudged = False
    studied = not budget.study_first
    study_nudges = 0
    idle_nudged = False
    request = budget.request
    if request is None:
        request = next(
            (str(m.get("content")) for m in reversed(messages) if m.get("role") == "user" and isinstance(m.get("content"), str)),
            "",
        )

    def safe_args(name: str, args: dict) -> dict:
        """Arguments as they go to the step stream and the trajectory: no passwords, no internals."""
        clean = {
            k: ("<CREDENTIAL>" if str(k).lower() in SECRET_KEYS and v else v)
            for k, v in args.items()
            if not k.startswith("_")
        }
        if name == "browser_type" and isinstance(clean.get("text"), str) and "<CREDENTIAL:" not in clean["text"]:
            label = guard.elements.get(clean["ref"], "") if isinstance(clean.get("ref"), int) else ""
            if label.startswith("поле пароля") or args.get("_secret"):
                clean["text"] = "<CREDENTIAL>"
        return {k: (mask_passwords(v) if isinstance(v, str) else v) for k, v in clean.items()}

    def run_call(call_id: str, name: str, args: dict, self_approve: bool = False) -> ToolResult:
        shown = safe_args(name, args)
        emit({"type": "step", "kind": "tool_call", "call_id": call_id, "tool": name, "args": shown})
        if self_approve:
            result = ToolResult(TRAINING_APPROVED)
        else:
            result = call_tool(call_id, name, args)
        content = result.content[:TOOL_RESULT_LIMIT]
        emit(
            {
                "type": "step",
                "kind": "tool_result",
                "call_id": call_id,
                "tool": name,
                "args": shown,
                "text": content,
                "error": result.error,
                "data": result.data,
            }
        )
        return ToolResult(content, result.error, result.data)

    def drop_old_screens() -> None:
        """A screenshot is shown to the model once: later rounds get a note instead (cost, and the text model)."""
        for index, message in enumerate(messages):
            content = message.get("content")
            if message.get("role") == "user" and isinstance(content, list) and any(
                isinstance(p, dict) and p.get("type") == "image_url" for p in content
            ):
                messages[index] = {"role": "user", "content": SCREEN_DROPPED}

    try:
        round_index = 0
        while True:
            control.checkpoint()
            if budget.max_rub is not None and finish_note is None and spent >= budget.max_rub * BUDGET_FINISH_SHARE:
                # Keep the rest of the money for the answer: what was done and what is left.
                finish_note = BUDGET_NOTE
                finish_reason = "budget"
            if budget.autopause and threshold is not None and spent > threshold:
                control.auto_pause(
                    "cost",
                    {"spent_rub": str(spent), "estimate_rub": str(budget.estimate_rub)},
                )
                # Resumed: pause again if the run costs another half of what it has spent.
                threshold = spent * AUTOPAUSE_RATIO
            out_of_calls = budget.max_tool_calls is not None and calls_used >= budget.max_tool_calls
            out_of_steps = round_index >= max_steps - 1
            if (
                out_of_steps
                and finish_note is None
                and not extended
                and budget.extend_steps > 0
                and blocked == 0
                and (budget.max_rub is None or spent < budget.max_rub / 2)
            ):
                # Still moving and money is left: a long form in 1С or МойСклад gets more rounds once.
                extended = True
                max_steps += budget.extend_steps
                out_of_steps = False
                emit({"type": "step", "kind": "extended", "max_steps": max_steps})
            if finish_note is None and (out_of_steps or out_of_calls):
                finish_note = FINISH_NOTE
                finish_reason = "max_steps" if out_of_steps and not out_of_calls else None
            last = finish_note is not None
            if last:
                # Words only: the run as plain text, no tools. With the real history and no tools
                # some models print their tool-call markup instead of an answer.
                completion = complete(summary_messages(messages, finish_note), None)
            else:
                compact_history(messages)
                completion = complete(messages, tools)
            drop_old_screens()
            round_index += 1
            out.steps += 1
            spent += completion.cost_rub
            out.alias = completion.alias
            out.input_tokens += completion.input_tokens
            out.output_tokens += completion.output_tokens
            text = strip_markup(completion.text)
            broken = has_markup(completion.text) and not completion.tool_calls
            emit(
                {
                    "type": "step",
                    "kind": "answer" if (not completion.tool_calls or last) else "model",
                    "model_alias": completion.alias,
                    "text": mask_passwords(text),
                    "input_tokens": completion.input_tokens,
                    "output_tokens": completion.output_tokens,
                    "cost_rub": completion.cost_rub,
                    "spent_rub": spent,
                }
            )
            if last:
                out.final_answer = text or GAVE_UP
                out.stopped = out.stopped or finish_reason
                break
            if broken and not text:
                # Markup instead of a real call or an answer. Once: ask again; twice: words only.
                broken_rounds += 1
                if broken_rounds >= 2:
                    finish_note = FINISH_NOTE
                    finish_reason = None
                else:
                    messages.append({"role": "user", "content": BROKEN_NOTE})
                continue
            if not completion.tool_calls:
                if not idle_nudged and calls_used == 0 and is_idle_answer(text) and not is_small_talk(request):
                    # A task, and the model only said hello: once, ask it to do the task (or say BLOCKED).
                    idle_nudged = True
                    messages.append({"role": "assistant", "content": text or "…"})
                    messages.append({"role": "user", "content": IDLE_NOTE})
                    emit({"type": "step", "kind": "idle_nudge"})
                    continue
                if not studied and study_nudges < STUDY_MAX_NUDGES:
                    study_nudges += 1
                    messages.append({"role": "assistant", "content": text or "…"})
                    messages.append({"role": "user", "content": STUDY_NOTE})
                    emit({"type": "step", "kind": "study_nudge"})
                    continue
                if typed_unsaved and not unsaved_nudged:
                    unsaved_nudged = True
                    messages.append({"role": "assistant", "content": text or "…"})
                    messages.append({"role": "user", "content": UNSAVED_NOTE})
                    emit({"type": "step", "kind": "unsaved_nudge"})
                    continue
                if saved_unchecked and not verify_nudged and VERIFY_TOOL in guard.schemas:
                    # Saved in МойСклад but never checked: one more look through the API before the answer.
                    verify_nudged = True
                    messages.append({"role": "assistant", "content": text or "…"})
                    messages.append({"role": "user", "content": VERIFY_NOTE})
                    emit({"type": "step", "kind": "verify_nudge", "tool": VERIFY_TOOL})
                    continue
                if not studied and text:
                    # Asked twice and still no look at a source: the user sees that the facts are unchecked.
                    emit({"type": "step", "kind": "study_unverified"})
                    text = UNVERIFIED_NOTE + text
                out.final_answer = text
                break
            completion.text = text
            messages.append(completion.assistant_message())
            stop_after = None
            screens: list[bytes] = []
            for call in completion.tool_calls:
                skip = stop_after or (
                    budget.max_tool_calls is not None and calls_used >= budget.max_tool_calls
                )
                if skip:
                    # The model must see an answer to every call it made.
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": "Не выполнено."})
                    continue
                control.checkpoint()
                args = parse_args(call.arguments)
                if prepare_args is not None and call.name != CONFIRM_TOOL:
                    try:
                        args = prepare_args(call.name, args)
                    except Exception:  # noqa: BLE001
                        pass  # the model's own arguments; the guard judges them as they are
                key = repeat_key(call.name, args)
                if key and key in failed:
                    # The model repeats a call that already failed the same way: don't spend a step on it.
                    hint = (
                        "Не повторяй его: передай в headers User-Agent, который требует документация API. "
                        "Браузер для этого не нужен."
                        if failed[key] == UA_REJECTED
                        else "Не повторяй его, попробуй другие аргументы или другой инструмент."
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": f"Не выполнено: этот вызов уже вернул ошибку ({failed[key]}). {hint}",
                        }
                    )
                    continue
                ckey = call_key(call.name, args)
                past = seen.get(ckey, [])
                if call.name != CONFIRM_TOOL and len(past) >= 2 and (
                    call.name in _REOPEN_TOOLS or past[-1] == past[-2]
                ):
                    # Third time the same call, and twice it changed nothing: refuse, don't loop.
                    blocked += 1
                    emit({"type": "step", "kind": "repeat_blocked", "tool": call.name, "args": safe_args(call.name, args)})
                    note = ""
                    if blocked < BLOCKS_TO_STOP:
                        # Stuck UI: try to unstick it before the run gives up (dialog, then reload).
                        recoveries += 1
                        rid = f"{call.id}_recover{recoveries}"
                        if blocked == 1 and "browser_press" in guard.schemas:
                            run_call(rid, "browser_press", {"key": "Escape"})
                            note = RECOVER_ESC
                        elif last_url and "browser_open" in guard.schemas:
                            run_call(rid, "browser_open", {"url": last_url})
                            note = RECOVER_RELOAD.format(url=last_url)
                        if note:
                            seen.clear()
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": REPEAT_BLOCKED + note})
                    continue
                if not studied and study_nudges < STUDY_MAX_NUDGES and call.name in ACT_TOOLS:
                    study_nudges += 1
                    emit({"type": "step", "kind": "study_gate", "tool": call.name})
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": STUDY_GATE})
                    continue
                if call.name == "browser_screenshot":
                    screenshots += 1
                    if screenshots > MAX_SCREENSHOTS:
                        messages.append({"role": "tool", "tool_call_id": call.id, "content": SCREENSHOT_LIMIT_TEXT})
                        continue
                approved_here = False
                policy_card = None
                if guard.needs_approval(call.name, args):
                    # A live write shows the policy card (host, change, budget) even if the model asked
                    # before: its own question may leave those out.
                    policy_card = guard.card(call.name, args)
                    if policy_card is None and guard.consume(call.name):
                        approved_here = True
                    elif policy_card is None and training:
                        emit({"type": "step", "kind": "training_approved", "tool": call.name})
                        approved_here = True
                    elif not can_ask:
                        # The model skipped ask_confirmation: the loop asks, and never runs the tool unasked.
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.id,
                                "content": "Ошибка: это действие нельзя выполнить без подтверждения пользователя.",
                            }
                        )
                        stop_after = "confirm"
                        continue
                    else:
                        auto_ids += 1
                        label = None
                        if call.name == "browser_click" and isinstance(args.get("ref"), int):
                            label = guard.elements.get(args["ref"])
                        ask_args = policy_card or {"action": call.name, "details": describe(call.name, args, label)}
                        answer = run_call(f"{call.id}_confirm{auto_ids}", CONFIRM_TOOL, ask_args)
                        calls_used += 1
                        if not approved(answer):
                            messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": call.id,
                                    "content": "Не выполнено: пользователь не разрешил это действие.",
                                }
                            )
                            stop_after = "confirm"
                            continue
                        approved_here = True
                run_args = {k: v for k, v in args.items() if not k.startswith("_")}
                if approved_here and (call.name == "browser_click" or policy_card is not None):
                    # The browser refuses a risky click by coordinates, and a write tool a live write,
                    # unless the loop says this very call was approved.
                    run_args[write_policy.CONFIRMED_ARG] = True
                # The model's own question on a training account: approved here. A live write never
                # counts on it (its card is asked above, always from a person).
                result = run_call(call.id, call.name, run_args, self_approve=training and call.name == CONFIRM_TOOL)
                calls_used += 1
                content = result.content
                if call.name != CONFIRM_TOOL:
                    digest = result_hash(result)
                    if past and past[-1] == digest:
                        content += REPEAT_WARNING
                    seen[ckey] = [*past, digest]
                    if call.name not in _LOOK_TOOLS:
                        # Something was done: looking again may show a new page.
                        for other in [k for k in seen if k.startswith(_LOOK_TOOLS)]:
                            seen.pop(other)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
                if not result.error:
                    if studied_by(call.name, result):
                        studied = True
                    if call.name == "browser_open" and isinstance(args.get("url"), str):
                        last_url = args["url"]
                    last_url = _address(result.content) or last_url
                    if call.name == "browser_click":
                        label = guard.elements.get(args["ref"], "") if isinstance(args.get("ref"), int) else ""
                        if _SAVE_LABEL.search(f"{label} {args.get('text') or ''}"):
                            saved_unchecked = True
                            typed_unsaved = False
                    if (
                        call.name == "browser_type"
                        and not args.get("submit")
                        and last_url
                        and _FORM_HOSTS.search(last_url)
                    ):
                        typed_unsaved = True
                    if call.name == VERIFY_TOOL:
                        saved_unchecked = False
                if key and result.error:
                    failed[key] = result.error
                guard.remember_page(result.content)
                shot = result.data.get("screenshot") if isinstance(result.data, dict) else None
                if call.name == "browser_screenshot" and isinstance(shot, (bytes, bytearray)) and shot:
                    screens.append(bytes(shot))
                if call.name == CONFIRM_TOOL:
                    ok = approved(result)
                    guard.record_approval(args, ok)
                    if not ok:
                        stop_after = "confirm"
            if screens:
                # Tool messages carry text only: the picture goes in a user message right after them.
                w, h = SCREEN_SIZE
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": SCREEN_NOTE.format(w=w, h=h)},
                            {
                                "type": "image_url",
                                "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(screens[-1]).decode()},
                            },
                        ],
                    }
                )
            if stop_after:
                out.stopped = stop_after
                out.final_answer = "Остановился: действие не подтверждено."
                break
            if not halfway_done and finish_note is None and round_index >= max_steps // 2:
                halfway_done = True
                messages.append({"role": "user", "content": HALFWAY_NOTE.format(done=round_index, total=max_steps)})
                emit({"type": "step", "kind": "halfway", "round": round_index})
            if blocked >= BLOCKS_TO_STOP and finish_note is None:
                finish_note = LOOP_NOTE
                finish_reason = "loop"
                out.stopped = "loop"
    except Stopped as exc:
        out.stopped = exc.reason
        out.cost_rub = spent
        out.final_answer = _stopped_answer(messages, complete, emit, out)
        spent = out.cost_rub
    out.final_answer = mask_passwords(out.final_answer)
    out.cost_rub = spent
    return out


def _stopped_answer(messages: list[dict], complete, emit, out: Outcome) -> str:
    """Stopped from outside (stop, pause timeout, quota): still tell the user what was done and what is left."""
    if out.steps == 0:
        return ""
    try:
        note = CAPTCHA_NOTE if out.stopped == "captcha" else STOP_NOTE
        completion = complete(summary_messages(messages, note), None)
    except Exception:  # noqa: BLE001
        return progress_text(messages, "Задача остановлена.")
    out.steps += 1
    out.cost_rub += completion.cost_rub
    out.input_tokens += completion.input_tokens
    out.output_tokens += completion.output_tokens
    text = strip_markup(completion.text)
    emit(
        {
            "type": "step",
            "kind": "answer",
            "model_alias": completion.alias,
            "text": mask_passwords(text),
            "input_tokens": completion.input_tokens,
            "output_tokens": completion.output_tokens,
            "cost_rub": completion.cost_rub,
            "spent_rub": out.cost_rub,
        }
    )
    return text or progress_text(messages, "Задача остановлена.")


def has_image(messages: list[dict]) -> bool:
    """The next round must go to a model that sees pictures."""
    for message in messages:
        content = message.get("content")
        if isinstance(content, list) and any(isinstance(p, dict) and p.get("type") == "image_url" for p in content):
            return True
    return False
