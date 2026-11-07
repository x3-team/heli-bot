# Сигналы самообучения Heli bot

Модель в этом шаге не дообучается. Сервер копит правильные сигналы с живых задач, чтобы потом собрать из них датасет: что вышло, что нет и почему.

## Что пишется

- **Журнал задачи** (`tasks`): `finished_at`, `stopped` (confirm, budget, max_steps, loop, captcha, quota, stop, error, lost), `duration_ms`, `parent_task_id` (продолжение, повтор, уточнение).
- Задача без исхода старше часа, у которой нет живого прогона (рестарт сервиса при деплое обрывает прогоны), закрывается как `aborted` / `stopped=lost` при старте следующей задачи того же пользователя: так она попадает в журнал и датасет.
- **Явная оценка** (`task_feedback`): `rating`, `acceptance`, `comment`, `user_edit` как раньше, плюс `reason_code` (wrong_fact, incomplete, wrong_system, refused_needed, dangerous, slow, other), `user_fixed` («Исправил сам»), `mail_disposition` (sent_as_is, edited_then_sent, declined). Поле `judge` зарезервировано под будущего LLM-судью, сейчас его никто не пишет.
- **Неявные сигналы** (`task_signals`, модуль `app/signals.py`): continue, retry, reformulate, next_unrelated, abandon (крестик на карточке или 30 минут без продолжения: проверяется при старте следующей задачи), confirm_approved/rejected/edited, verify_ok/verify_fail (чтение того же сервиса после записи), loop/max_steps/captcha/budget. Клиенту их не показываем и о них не спрашиваем.

## Выгрузка

`GET /v1/admin/trajectories.jsonl?kind=raw|sft|dpo`

- В документе есть `feedback.reason_code/user_fixed/mail_disposition`, `signals[]`, `stopped`, `duration_ms`, `cost_rub_total`, `revision` (`APP_REVISION` или `GIT_SHA`, иначе sha из `server/REVISION`, который пишет deploy-pinvds, иначе null), `internal`.
- **SFT** берёт документ, только если у него есть настоящее «хорошо»: 👍, `acceptance=accepted` или сигнал `verify_ok`. Голый `success` от auto_finish в SFT не попадает. `success_only` (по умолчанию для sft) и `thumbs_up_only` работают как раньше.
- **DPO**: пары answer (из `user_edit`, в том числе «Исправил сам») и action (из `tool_args_edit`).
- Задачи общего аккаунта (`HELI_OPEN_ACCESS_EMAIL`, по умолчанию `owner@heli.local`) и учебных аккаунтов (`HELI_TRAINING_EMAILS`) не попадают в sft и dpo. В raw остаются с `internal=true` (старые документы без флага помечаются по аккаунту при выгрузке). `sft_ready` в /admin/quality считается по тем же правилам, что SFT-выгрузка.
- В документе нет user_id, почты аккаунта, паролей и снимков экрана.

## Согласия и удаление

- `consent_pdn` обязателен для старта задачи. `consent_training` по умолчанию выключен; без него обучающая копия не пишется, выключение удаляет копию.
- `DELETE /v1/me/trajectories` удаляет обучающую копию. `DELETE /v1/me/tasks` (или `DELETE /v1/me/trajectories?include_tasks=true`) удаляет историю задач: задачи, шаги, оценки, сигналы и документы. Доступы и память остаются. Задачи, которые ещё выполняются, не трогаются (`kept_running`).

## База знаний

`knowledge_save` из живой задачи обычного аккаунта сохраняется кандидатом (`origin=candidate`) и в `knowledge_search` / `relevant_notes` не виден, пока админ не подтвердит: `GET /v1/admin/knowledge/candidates`, `POST /v1/admin/knowledge/{id}/review {"decision": "approve"|"reject"}`. Записи админа и стартовые записи видны сразу. Автокандидатов в этом шаге нет.

## Проверка на проде

Actions → deploy-pinvds → mode `learning-check`: только чтение, печатает счётчики (журнал, оценки, сигналы, выгрузки, /admin/quality), без текстов задач, почт и id.

## Дашборд

`GET /v1/admin/quality?days=7`: задачи, доля оценённых, 👍/👎, причины, «исправил сам», письма, сигналы, остановки, средние шаги/время/стоимость, воронка датасета, кандидаты в базу знаний, `hash_pepper_set`. `GET /v1/admin/feedback` дополнен полями `reason_code`, `user_fixed`, `mail_disposition`, `stopped`, `signals`.

## Абзац для оферты (черновик, на сайт не выкладывать)

> Чтобы оказать услугу, мы обрабатываем ваши персональные данные: почту аккаунта, тексты заданий, данные из сервисов, которые вы сами подключили, и технический журнал действий бота. Журнал хранится на сервере в Российской Федерации. Для ответа бот обращается к языковым моделям через шлюз GetHeli; содержимое задания при этом передаётся модели.
>
> Отдельно, по желанию, вы можете разрешить использовать обезличенные задания и ваши оценки, чтобы улучшать Heli bot, в том числе обучать наши модели. Пароли, ключи и прямые идентификаторы в эту копию не входят. Разрешение можно отозвать в настройках в любой момент: обучающая копия ваших заданий будет удалена. Отказ не влияет на работу сервиса.

Текст не обещает, что задание не покидает РФ на время вызова модели: обещать этого нельзя.
