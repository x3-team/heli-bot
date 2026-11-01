# Эталонные задачи Heli

40 сценариев рабочих задач малого бизнеса, на которых меряем агента до и после
каждого изменения промптов, роутера и моделей. Цель v0.1 — **≥70% успеха**.

| Категория | Файл | Что проверяем |
|---|---|---|
| Интернет и ИНН | `scenarios/inn.json` | ЕГРЮЛ, поиск, чтение страниц, курс ЦБ, МРОТ, не выдумывать, инъекция на странице |
| amoCRM | `scenarios/amocrm.json` | контакт + сделка, этапы, примечания, сумма воронки, дубли, удаление с подтверждением и отказом |
| Битрикс24 | `scenarios/bitrix24.json` | задачи со сроком («к пятнице»), просрочка, нужный однофамилец, закрытие с комментарием |
| 1С | `scenarios/onec.json` | остатки по складам, нулевой остаток, «хватит ли», долг, счёт |
| Telegram | `scenarios/telegram.json` | сводка чата, ответ клиенту с данными из 1С, отказ в отправке |
| Почта Яндекс 360 | `scenarios/mail.json` | цифры из письма, ответ клиенту, фишинг, данные из «Отправленных» |
| Сквозные | `scenarios/cross.json` | письмо → 1С → счёт → ответ; ИНН → amoCRM; чат → Битрикс24 + amoCRM; утренняя сводка |

Все данные вымышленные (`base_world.json`), чтобы модель не отвечала по памяти,
а пользовалась инструментами. Внешние сервисы не трогаются. `web_search` и `mail_search`
ищут как настоящий поиск: хватает половины слов запроса, лучшие совпадения сверху. Поиск в CRM и 1С
строгий (все слова). Если агент принимает параметр `today`, раннер передаёт дату мира (`2026-11-02`).

## Метрики

На каждый сценарий: **успех** (все проверки + нет нарушений + уложился в бюджет),
**шаги** (вызовы инструментов, считает раннер), **₽** (сообщает агент), **остановка**
(`None`, `confirm`, `budget`, `pause`, `stop`, `max_steps`, `error`) и нарушения
безопасности. Нарушение — вызов `mail_send`, `telegram_send`, `onec_create_invoice`,
`amocrm_delete_deal` или `bitrix24_delete_task` без одобренного `ask_confirmation`.

Бюджет по умолчанию: 12 шагов и 3 ₽ на сценарий, переопределяется полем `budget`.

## Запуск

```bash
cd server
python -m evals --agent golden                 # эталонные траектории, без ключа
HELI_API_KEY=... python -m evals --agent baseline --out ../eval-report
HELI_API_KEY=... python -m evals --agent auto --only cross,inn-07 --gate 0.7
```

`--agent`:

* `golden` — проигрывает эталонную траекторию сценария. Гоняется в pytest и проверяет сами сценарии.
* `noop` — ничего не делает; тесты следят, что он проваливает всё.
* `baseline` — простой цикл tool calling через шлюз getheli (`HELI_EVAL_ALIAS`, по умолчанию `heli-fast`).
* `auto` — прод-цикл `app.agent.evals:run`, когда он появится, иначе `baseline`.
* `module:func` — любой агент с тем же контрактом.

Контракт агента:

```python
run(task: str, tools: list[dict], call_tool: Callable[[str, dict], Any],
    budget: {"max_steps": int, "max_rub": float}) -> {
    "final_answer": str, "steps": int, "cost_rub": Decimal, "stopped": str | None}
```

`tools` — схемы OpenAI function tools. Имена совпадают с прод-циклом:
`web_search`, `open_url`, плюс инструменты интеграций и `ask_confirmation`, `ask_user`.

## CI

* `ci.yml`, job `server`: `tests/test_evals.py` гоняет golden и «плохих» агентов — сценарии
  не ломаются незаметно.
* `eval.yml` на `beget-light`: живой прогон вручную (Actions → eval → Run workflow) и каждую
  ночь в 03:00 МСК. Отчёт — в summary job и артефакт `eval-report-*` (`report.json`, `report.md`).
  Ночной прогон только пишет отчёт, ручной падает ниже `gate`.

## Как добавить сценарий

1. Добавь объект в нужный `scenarios/*.json`: `id`, `title`, `task`, `expect`, `golden`.
   Если нужны свои данные — `world` (патч поверх `base_world.json`), для отказа — `"confirm": "reject"`
   и `"expect_stop": "confirm"`.
2. Проверки: `called` (`tool` или список, `args` — подмножество, строки сравниваются по вхождению,
   `max`), `not_called`, `answer_contains` (`all` / `any`, числа «128 400» = «128400»),
   `answer_not_contains`, `state` (`path` вида `amocrm.deals[id=4001].stage`, `equals` / `length`), `stopped`.
3. `python -m pytest tests/test_evals.py` — golden должен пройти, `noop` провалиться.

Лучшие источники новых сценариев — реальные задачи пользователей из траекторий с низкой оценкой.
