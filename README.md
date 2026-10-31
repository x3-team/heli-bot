# Heli bot

Десктопный ассистент для компаний в РФ. Клиент — Tauri 2 (Windows и macOS) и та же веб-оболочка. Сервер — FastAPI. Модели вызываются только с сервера, ключ `HELI_API_KEY` живёт в окружении и в секрете GitHub Actions, не в клиенте и не в репозитории.

v0.1: чат, облачный браузер, простые действия на компьютере с подтверждением, доступы, расписания, процент лимита, журнал, отзыв. Не входит: оплата, роли, маркетплейс коннекторов, своя модель, голос, мобильное приложение.

## Репозиторий

- `apps/desktop` — Tauri 2 + React
- `apps/web` — веб-версия того же интерфейса
- `packages/ui` — общая оболочка. Визуал — заглушки с `data-design-slot`; макет и маскот подставляет отдельный дизайн-проект
- `server` — оркестратор
- `browser` — образ Chromium + noVNC, один контейнер на пользователя
- `infra` — docker-compose: api, Postgres (pgvector), Redis, Caddy

Публичные алиасы моделей: `heli-fast`, `heli-fast-alt`, `heli-vision`, `heli-vision-lite`, `heli-smart`, `heli-smart-alt`. Клиент и публичное API других имён не показывают.

## Локально

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e "./server[dev]"
pytest server/tests

pnpm install
pnpm typecheck
pnpm --filter @heli/web dev
```

Сервер: `python -m app.main` из каталога `server` (слушает `0.0.0.0:$PORT`). Ключ модели: переменная окружения `HELI_API_KEY`. Пример остальных переменных — `.env.example`.

Инфраструктура: `docker compose -f infra/docker-compose.yml up --build` (нужен Docker и `HELI_API_KEY` в окружении).

Десктоп: `pnpm --filter @heli/desktop tauri dev`. Сборка без подписи — позже, в этапе релиза. На macOS без подписи Gatekeeper, на Windows SmartScreen предупредит.

## Лимиты и журнал

Тестерам показывается процент недели и месяца, на 100% задача останавливается. С первого дня пишутся траектории задач для будущего дообучения: только при отдельном согласии и после обезличивания. Пайплайна обучения в v0.1 нет.

## CI

Один workflow, только `pull_request`. Тесты сервера и проверка типов фронта. Секрет `HELI_API_KEY` подключается к смоук-тесту маршрутизатора, когда этот тест появляется; значение не печатается.
