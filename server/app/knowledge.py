"""Heli's own knowledge base (RAG): checked rules about services and topics, with a source and a date.

- Before a task the loop looks here (relevant_notes) and puts what it finds into the prompt.
- The agent searches it itself (knowledge_search), saves checked conclusions after a task that worked
  (knowledge_save: a source is required) and marks a note that turned out wrong or old (knowledge_mark_stale).
- A note older than STALE_AFTER_DAYS is shown as «устарело, перепроверь»: the agent checks it against the
  source again and saves the fresh version.
- The starting notes (SEEDS, from the Директ notes of the read-only track) are only the first content.
  They live in the same table and age the same way.

The base is shared by all accounts, so a note is a general rule, never client data: the text is masked
(emails, phones, ИНН, secrets) before it is stored.

A knowledge_save from a live client task (not an admin's run) is a candidate (origin="candidate"): it is
not shown by search / relevant_notes to anyone until an admin approves it
(GET /v1/admin/knowledge/candidates, POST /v1/admin/knowledge/{id}/review). One client's task cannot put
text in front of another client before a person has looked at it.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.control import ClientResult
from app.masking import mask_text
from app.models import KnowledgeNote

log = logging.getLogger(__name__)

STALE_AFTER_DAYS = 180
SEARCH_LIMIT = 5
TEXT_MAX = 1200
PROMPT_CHARS = 3000
HELI_SOURCE = "heli://rules"  # the owner's own rules: they do not age
CANDIDATE = "candidate"
REJECTED = "rejected"
HIDDEN = (CANDIDATE, REJECTED)

_WORD = re.compile(r"[a-zа-яё0-9]+", re.I)
_STOP = {
    "и", "в", "на", "по", "с", "за", "для", "не", "что", "как", "это", "или", "из", "к", "от", "до", "у", "о",
    "а", "но", "ли", "the", "a", "of", "to", "in", "and", "мне", "нам", "мой", "наш", "сделай", "посмотри",
    "проверь", "скажи", "какой", "какие", "сколько", "нужно", "надо", "можно",
}

# Starting content. Rules marked HELI_SOURCE are the owner's policy, the rest point at official help.
SEEDS: tuple[tuple[str, str, str], ...] = (
    (
        "Яндекс Директ: стратегии с оплатой за конверсии",
        "Стратегии с оплатой за конверсии или оптимизацией конверсий включать только если по цели набирается "
        "не меньше 10 конверсий в неделю. Меньше — ручное управление ставками или «Максимум кликов» с "
        "ограничением цены клика (CPC): сначала накопить данные о конверсиях, потом переходить на конверсионную "
        "стратегию. Перед советом проверь число конверсий за последние 7 и 28 дней (отчёт Директа с Goals или Метрика).",
        HELI_SOURCE,
    ),
    (
        "Яндекс Директ: боевые кабинеты только чтение",
        "В боевом кабинете клиента Heli только читает: кампании, группы, ключевые фразы, ставки, отчёты. Любое "
        "изменение (ставки, стратегия, остановка, новые объявления) — сначала в песочнице API, а в бою — "
        "рекомендацией пользователю: что, где и почему поменять.",
        HELI_SOURCE,
    ),
    (
        "Яндекс Директ API v5: адреса и заголовки",
        "JSON API v5: https://api.direct.yandex.com/json/v5/<сервис> (campaigns, adgroups, ads, keywords, bids, "
        "keywordbids, dictionaries, reports…), запрос POST {\"method\": \"get\", \"params\": {…}} c SelectionCriteria "
        "и FieldNames. Заголовки: Authorization: Bearer <OAuth-токен>, Accept-Language: ru; для агентства — "
        "Client-Login с логином клиента. Ответ с полем error — ошибка (error_code, error_string, error_detail). "
        "Заголовок Units: баллы «израсходовано/осталось/суточный лимит».",
        "https://yandex.ru/dev/direct/doc/ru/",
    ),
    (
        "Яндекс Директ API v5: отчёты (Reports)",
        "Отчёты: POST https://api.direct.yandex.com/json/v5/reports, тело {\"params\": {SelectionCriteria, FieldNames, "
        "ReportName, ReportType, DateRangeType, Format: TSV, IncludeVAT}}. Ответ 200 — готовый TSV; 201 — отчёт "
        "поставлен в очередь; 202 — строится: повторить тот же запрос через retryIn секунд (заголовок). ReportName "
        "должен быть уникальным для набора параметров. Заголовки: processingMode (auto/online/offline), "
        "returnMoneyInMicros: false (деньги в валюте, не в микроединицах), skipReportHeader, skipReportSummary. "
        "Конверсии в отчёте — поля Conversions, CostPerConversion с параметром Goals (id целей Метрики). "
        "Типы: CAMPAIGN_PERFORMANCE_REPORT, ADGROUP_PERFORMANCE_REPORT, AD_PERFORMANCE_REPORT, "
        "CRITERIA_PERFORMANCE_REPORT, SEARCH_QUERY_PERFORMANCE_REPORT (поисковые запросы — для минус-слов), "
        "CUSTOM_REPORT.",
        "https://yandex.ru/dev/direct/doc/ru/reports/reports",
    ),
    (
        "Яндекс Директ API: песочница",
        "Песочница: https://api-sandbox.direct.yandex.com/json/v5/ — тот же API на тестовых данных, изменения не "
        "трогают настоящие кампании и деньги. Включается владельцем аккаунта в интерфейсе Директа (раздел API, "
        "«Песочница»). Записывающие методы (add, update, delete, suspend, resume, set) Heli вызывает только там.",
        "https://yandex.ru/dev/direct/doc/ru/concepts/sandbox",
    ),
    (
        "Яндекс Метрика API: отчёты и цели",
        "Отчёты: GET https://api-metrika.yandex.net/stat/v1/data?ids=<счётчик>&metrics=ym:s:visits,ym:s:users&"
        "dimensions=ym:s:lastTrafficSource&date1=30daysAgo&date2=today, заголовок Authorization: OAuth <токен>. "
        "Конверсии по цели: metrics ym:s:goal<ID>reaches и ym:s:goal<ID>conversionRate. Список целей: "
        "/management/v1/counter/<счётчик>/goals. Счётчики: /management/v1/counters.",
        "https://yandex.ru/dev/metrika/ru/",
    ),
    (
        "Аналитика: событие GA4, ключевое событие GA4 и цель Метрики",
        "Событие GA4 (event) — любое действие, которое ресурс собирает: page_view, click, form_submit, "
        "heli_train_ping. Ключевое событие (key event, раньше «конверсия») — событие, которое владелец ресурса "
        "отметил как важное для бизнеса в Администратор → Ключевые события; отмечать можно до 30 событий (в 360 — "
        "до 50), в отчётах это метрика keyEvents. Цель Яндекс Метрики — условие на визит (посещение страницы, "
        "JavaScript-событие reachGoal, клик по телефону, отправка формы, «Параметры»), его достижения считаются "
        "в ym:s:goal<ID>reaches; Директ берёт конверсии по целям Метрики. Это разные системы: событие GA4 не "
        "становится целью Метрики, считать их надо отдельно. Отвечая, что такое key event, ссылайся на справку "
        "Google, не на память.",
        "https://support.google.com/analytics/answer/9267568",
    ),
    (
        "Google Analytics 4: события и отметка ключевых",
        "О событиях GA4: https://support.google.com/analytics/answer/9322688. Как отметить событие ключевым и "
        "лимиты: https://support.google.com/analytics/answer/13128484. Список ключевых событий ресурса — Admin API "
        "properties/<id>/keyEvents (ga4_read key_events), число — метрика keyEvents в runReport.",
        "https://support.google.com/analytics/answer/13128484",
    ),
    (
        "Google Analytics Data API: отчёты",
        "POST https://analyticsdata.googleapis.com/v1beta/properties/<id>:runReport, тело {dateRanges, dimensions, "
        "metrics, dimensionFilter, limit}: это чтение. Метрики eventCount, totalUsers, activeUsers, sessions, "
        "keyEvents; измерения eventName, date, sessionSource. Реальное время — :runRealtimeReport, список полей — "
        "GET properties/<id>/metadata. Доступ: OAuth с областью analytics.readonly или сервисный аккаунт с ролью "
        "«Читатель».",
        "https://developers.google.com/analytics/devguides/reporting/data/v1",
    ),
    (
        "GA4 Measurement Protocol: только учебный ресурс",
        "Measurement Protocol: POST https://www.google-analytics.com/mp/collect?measurement_id=G-…&api_secret=…, "
        "тело {client_id, events: [{name, params}]}; ответ 2xx всегда, ошибки видны только на "
        "/debug/mp/collect. Хит записывается в статистику ресурса навсегда. Heli шлёт хиты только на учебный "
        "Measurement ID (ga4_send_event). Не предлагать «отправить тестовый хит на боевой сайт, чтобы "
        "проверить»: это портит статистику и ключевые события клиента. Проверка боевого сайта — чтение отчётов "
        "(runReport, realtime) или DebugView/Tag Assistant самим владельцем.",
        HELI_SOURCE,
    ),
    (
        "Яндекс Метрика: цели",
        "Цель Метрики задаётся в настройках счётчика: посещение страниц, JavaScript-событие (ym(<счётчик>, "
        "'reachGoal', '<идентификатор>')), автоматические цели, «Параметры», составная. Какую выбрать: "
        "https://yandex.ru/support/metrica/ru/general/choose-goal.html. Heli цели в боевом счётчике не создаёт и "
        "не меняет и хиты в mc.yandex.ru не шлёт: только читает (metrika_read goals, stat), а пользователю "
        "описывает, какую цель завести.",
        "https://yandex.ru/support/metrica/ru/general/goal-js-event",
    ),
    (
        "Ozon Seller API: доступ",
        "Адрес https://api-seller.ozon.ru, почти все методы POST с JSON-телом, даже чтение (…/list, …/info). "
        "Заголовки Client-Id и Api-Key (ключ в личном кабинете продавца: Настройки → API-ключи). Изменяющие "
        "методы (…/create, …/update, …/import, …/delete) в бою Heli не вызывает.",
        "https://docs.ozon.ru/api/seller/",
    ),
    (
        "ЦБ РФ: официальный курс валют на дату (XML_daily)",
        "Курс доллара, евро, юаня и других валют ЦБ берётся одним GET без ключа: "
        "https://www.cbr.ru/scripts/XML_daily.asp?date_req=ДД/ММ/ГГГГ (например date_req=15/10/2026; без "
        "date_req — последний установленный курс). Ответ XML <ValCurs Date=…>: у каждой <Valute> CharCode (USD, "
        "EUR, CNY), Nominal, Value (дробь через запятую) и VunitRate — курс за 1 единицу; Value делить на Nominal "
        "(JPY, KZT, AMD — за 10 или 100). Атрибут Date — дата, на которую курс установлен (выходные — курс "
        "прошлого рабочего дня), её и называй. Динамика за период: "
        "https://www.cbr.ru/scripts/XML_dynamic.asp?date_req1=…&date_req2=…&VAL_NM_RQ=R01235 (R01235 — USD, "
        "R01239 — EUR, R01375 — CNY; коды — XML_valFull.asp). SOAP-сервис DailyInfo.asmx для курса не используй: "
        "это POST с конвертом и лишние шаги, XML_daily отвечает тем же.",
        "https://www.cbr.ru/development/SXML/",
    ),
    (
        "Капча на сайтах",
        "Капчу Heli не решает и не обходит: ставит задачу на паузу и просит пользователя пройти её на экране "
        "«Компьютер агента», потом продолжает.",
        HELI_SOURCE,
    ),
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stems(text: str) -> set[str]:
    """Words cut to 5 letters: «конверсий», «конверсии», «конверсионную» meet."""
    return {w.lower()[:5] for w in _WORD.findall(text or "") if w.lower() not in _STOP and len(w) > 1}


def ensure_seeded(session: Session) -> None:
    known = {(row.topic, row.origin) for row in session.scalars(select(KnowledgeNote).where(KnowledgeNote.origin == "seed"))}
    added = False
    for topic, text, source in SEEDS:
        if (topic, "seed") in known:
            continue
        now = _now()
        session.add(KnowledgeNote(topic=topic, text=text, source_url=source, origin="seed", checked_at=now, created_at=now))
        added = True
    if added:
        session.flush()


def is_stale(note: KnowledgeNote, now: datetime | None = None) -> bool:
    if note.stale:
        return True
    if note.source_url == HELI_SOURCE:
        return False
    checked = note.checked_at if note.checked_at.tzinfo else note.checked_at.replace(tzinfo=timezone.utc)
    return (now or _now()) - checked > timedelta(days=STALE_AFTER_DAYS)


def search(session: Session, query: str, limit: int = SEARCH_LIMIT) -> list[KnowledgeNote]:
    ensure_seeded(session)
    words = _stems(query)
    if not words:
        return []
    scored = []
    for note in session.scalars(select(KnowledgeNote).where(KnowledgeNote.origin.not_in(HIDDEN))):
        topic_hits = len(words & _stems(note.topic))
        text_hits = len(words & _stems(note.text))
        score = topic_hits * 2 + text_hits
        if topic_hits or text_hits >= 2 or (text_hits and len(words) <= 2):
            scored.append((score, note.checked_at, note))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [note for _, _, note in scored[:limit]]


def render(note: KnowledgeNote) -> str:
    mark = " ⚠ устарело, перепроверь по источнику" if is_stale(note) else ""
    if note.stale and note.stale_reason:
        mark += f" ({note.stale_reason})"
    source = "правило Heli" if note.source_url == HELI_SOURCE else note.source_url
    return f"[{note.id}] {note.topic}: {note.text} (источник: {source}; проверено {note.checked_at:%d.%m.%Y}){mark}"


def relevant_notes(session: Session, query: str) -> list[str]:
    try:
        notes = search(session, query)
    except Exception:  # noqa: BLE001  (the table may be missing on an old database)
        log.info("knowledge search failed")
        session.rollback()
        return []
    return [render(n) for n in notes]


def with_knowledge(text: str, notes: list[str]) -> str:
    if not notes:
        return text
    block = "\n".join(f"- {n}" for n in notes)
    if len(block) > PROMPT_CHARS:
        block = block[:PROMPT_CHARS] + "…"
    return f"{text}\n\nБаза знаний Heli (проверенные правила с источником; помеченное «устарело» — перепроверь):\n{block}"


def save(
    session: Session, user, topic: str, text: str, source_url: str, *, candidate: bool = False
) -> KnowledgeNote:
    """candidate=True: a note from a live client task, hidden until an admin approves it."""
    topic = mask_text(topic.strip())[:200]
    text = mask_text(text.strip())[:TEXT_MAX]
    source_url = source_url.strip()
    if not topic or not text:
        raise ValueError("empty")
    if not re.match(r"^https?://[^\s]+$", source_url):
        raise ValueError("source")
    now = _now()
    same = session.scalars(select(KnowledgeNote).where(KnowledgeNote.topic == topic)).all()
    for note in same:
        if note.origin == REJECTED or _stems(note.text) != _stems(text):
            continue
        if candidate and note.origin != CANDIDATE:
            # The same rule confirmed again from a client task: only the check date moves.
            note.checked_at, note.stale, note.stale_reason = now, False, None
        else:
            note.checked_at, note.stale, note.stale_reason, note.source_url = now, False, None, source_url[:1000]
        session.flush()
        return note
    note = KnowledgeNote(
        topic=topic, text=text, source_url=source_url[:1000], origin=CANDIDATE if candidate else "agent",
        checked_at=now, created_by=getattr(user, "id", None), created_at=now,
    )
    session.add(note)
    session.flush()
    return note


def is_candidate_run(user, task_id) -> bool:
    """A save from a live task of a non-admin account is a candidate; a run without a task or an admin's is not."""
    return task_id is not None and not bool(getattr(user, "is_admin", False))


def candidates(session: Session, limit: int = 100) -> list[KnowledgeNote]:
    return list(
        session.scalars(
            select(KnowledgeNote)
            .where(KnowledgeNote.origin == CANDIDATE)
            .order_by(KnowledgeNote.created_at.desc())
            .limit(limit)
        )
    )


def review(session: Session, note_id: uuid.UUID, decision: str, reviewer) -> KnowledgeNote | None:
    note = session.get(KnowledgeNote, note_id)
    if note is None or note.origin != CANDIDATE:
        return None
    now = _now()
    note.origin = "agent" if decision == "approve" else REJECTED
    note.approved_at = now if decision == "approve" else None
    note.reviewed_by = getattr(reviewer, "id", None)
    session.flush()
    return note


def public_note(note: KnowledgeNote) -> dict:
    return {
        "id": str(note.id),
        "topic": note.topic,
        "text": note.text,
        "source_url": note.source_url,
        "origin": note.origin,
        "checked_at": note.checked_at.isoformat() if note.checked_at else None,
        "approved_at": note.approved_at.isoformat() if note.approved_at else None,
        "created_at": note.created_at.isoformat() if note.created_at else None,
    }


def mark_stale(session: Session, note_id: str, reason: str) -> KnowledgeNote | None:
    try:
        note = session.get(KnowledgeNote, uuid.UUID(str(note_id).strip("[] ")))
    except ValueError:
        return None
    if note is None:
        return None
    note.stale = True
    note.stale_reason = mask_text(reason.strip())[:300] or None
    session.flush()
    return note


# --- agent tools -------------------------------------------------------------------


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


SEARCH_TOOL = _fn(
    "knowledge_search",
    "Поиск в базе знаний Heli: проверенные правила и ограничения сервисов с источником и датой. Ищи здесь "
    "первым делом перед задачей в сервисе или теме, затем — официальную справку через web_search и open_url.",
    {"query": {"type": "string", "description": "Сервис и тема, например «Директ стратегия конверсии»"}},
    ["query"],
)
SAVE_TOOL = _fn(
    "knowledge_save",
    "Сохранить в базу знаний проверенный вывод после удачной задачи: общее правило, ограничение, адрес API, "
    "порядок действий. Только то, что подтверждено источником (официальная справка, документация API) — "
    "его адрес обязателен. Без данных клиента: имён, сумм, логинов, ключей.",
    {
        "topic": {"type": "string", "description": "Сервис: тема, например «Ozon Seller API: остатки»"},
        "fact": {"type": "string", "description": "Вывод одним-тремя предложениями"},
        "source_url": {"type": "string", "description": "Адрес источника https://…"},
    },
    ["topic", "fact", "source_url"],
)
STALE_TOOL = _fn(
    "knowledge_mark_stale",
    "Пометить запись базы знаний устаревшей или неверной, если источник говорит иначе. Затем сохрани "
    "правильную версию через knowledge_save.",
    {
        "id": {"type": "string", "description": "id записи в квадратных скобках из knowledge_search"},
        "reason": {"type": "string", "description": "Что изменилось и где это сказано"},
    },
    ["id", "reason"],
)
TOOL_NAMES = ("knowledge_search", "knowledge_save", "knowledge_mark_stale")

STUDY_HINT = (
    " Осваивай незнакомый инструмент сам, прежде чем действовать или отвечать фактами о нём. Незнакомый — "
    "сервис, API, кабинет или тема, которых нет в «Базе знаний Heli» ниже и в инструкциях. Цикл «освой "
    "инструмент»: 1) knowledge_search; 2) официальная справка или документация API (web_search, open_url; "
    "официальный сайт сервиса важнее форумов, отзывов и статей); 3) выпиши авторизацию (какой ключ или токен "
    "и в каком заголовке), лимиты и что у сервиса песочница (отдельный хост или тестовый режим); 4) проверь на "
    "деле через http_request: публичный GET-метод или запись только в песочнице, боевые кабинеты — только "
    "чтение; ключ — только {{secret:ИМЯ}} и только на хост этого ключа; 5) после проверки сохрани вывод через "
    "knowledge_save с адресом источника (дата проверки ставится сама). Ответ API с ошибкой авторизации "
    "(401/403, нет ключа) — это не данные: так и скажи и попроси ключ, ответ и цифры не выдумывай. Если запись "
    "базы противоречит источнику или помечена «устарело», перепроверь, пометь knowledge_mark_stale и сохрани новую."
)


def execute(session, user, task_id, name: str, args: dict) -> ClientResult:
    """app.agent.executors contract."""
    if name == "knowledge_search":
        notes = search(session, str(args.get("query") or ""))
        session.commit()
        if not notes:
            return ClientResult(
                "В базе знаний ничего нет по этой теме. Найди официальную справку или документацию (web_search, "
                "open_url) и после удачной задачи сохрани вывод через knowledge_save."
            )
        return ClientResult("\n".join(render(n) for n in notes))
    if name == "knowledge_save":
        candidate = is_candidate_run(user, task_id)
        try:
            note = save(
                session, user, str(args.get("topic") or ""), str(args.get("fact") or ""),
                str(args.get("source_url") or ""), candidate=candidate,
            )
            session.commit()
        except ValueError as exc:
            session.rollback()
            if str(exc) == "source":
                return ClientResult("Ошибка: нужен адрес источника https://… — без источника вывод не сохраняется.", "no_source")
            return ClientResult("Ошибка: укажи тему и вывод.", "bad_args")
        if note.origin == CANDIDATE:
            return ClientResult(
                f"Вывод принят на проверку: [{note.id}] {note.topic} (проверено {note.checked_at:%d.%m.%Y}). "
                "В базе знаний он появится после проверки; в этой задаче опирайся на сам источник."
            )
        return ClientResult(f"Сохранено в базу знаний: [{note.id}] {note.topic} (проверено {note.checked_at:%d.%m.%Y}).")
    if name == "knowledge_mark_stale":
        note = mark_stale(session, str(args.get("id") or ""), str(args.get("reason") or ""))
        if note is None:
            return ClientResult("Ошибка: записи с таким id нет.", "not_found")
        session.commit()
        return ClientResult(f"Помечено устаревшим: {note.topic}. Сохрани правильную версию через knowledge_save.")
    return ClientResult("Ошибка: unknown_tool", "unknown_tool")


def register() -> None:
    from app.agent.executors import register_executor
    from app.agent.tools import register_client_tool

    for schema in (SEARCH_TOOL, SAVE_TOOL, STALE_TOOL):
        register_client_tool(schema)
    register_executor(TOOL_NAMES, execute)
