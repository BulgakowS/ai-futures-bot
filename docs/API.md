# Проєктний API-контракт

Це **цільовий контракт**, не опис наявних endpoint-ів. Фінальні URL, поля та версіонування затвердити під час Foundation і генерувати OpenAPI зі справжніх схем. Усі команди авторизуються, валідуються та отримують `request_id`; мутації, що впливають на ордери або баланс, приймають `Idempotency-Key`.

## HTTP ресурси

| Метод і шлях (проєкт) | Призначення | Особливість |
|---|---|---|
| `GET /api/v1/health` | стан API/DB/Redis/market workers | без секретів |
| `GET /api/v1/markets` | інструменти й правила | version, quality, source time |
| `GET /api/v1/markets/{id}/candles` | свічки | UTC range, timeframe, final flag |
| `GET /api/v1/portfolios` | список за режимом | paper за замовчуванням |
| `POST /api/v1/portfolios` | створити віртуальний портфель | mode=`paper`, стартовий баланс |
| `POST /api/v1/portfolios/{id}/reset` | нова епоха paper-портфеля | підтвердження, журнал збережено |
| `GET/POST /api/v1/bots` | список/створення | версія стратегії й risk profile |
| `POST /api/v1/bots/{id}/start` | запуск | перевірка даних та конфігурації |
| `POST /api/v1/bots/{id}/pause` | призупинення | нові входи зупинено |
| `GET /api/v1/positions` | відкриті позиції | mode/portfolio filters |
| `GET /api/v1/orders` | ордери/fills | status, correlation ID |
| `GET /api/v1/decisions` | AI/strategy/risk журнал | reason codes |
| `POST /api/v1/risk/kill-switch` | аварійне блокування | server-side state + audit |
| `GET/POST /api/v1/backtests` | запуски й результати | dataset/config/seed |
| `GET /api/v1/analytics` | метрики та equity curve | методика, витрати, період |

У MVP не експонувати endpoint для live-ордерів. Якщо згодом він з'явиться, він має окремий namespace й незалежний server-side gate. `POST /bots/start` не повинен бути прихованим способом увімкнення Live.

## Події до frontend

SSE або WebSocket канал (вибір на етапі Foundation) передає `market.updated`, `data.degraded`, `bot.status`, `decision.created`, `risk.rejected`, `order.updated`, `position.updated`, `portfolio.updated`, `alert.created`. Кожна подія має `event_id`, `aggregate_id`, `occurred_at`, `sequence`, `schema_version`. Клієнт після reconnect читає snapshot HTTP і продовжує з останнього cursor; повтор події не подвоює UI-стан.

## Помилки та безпека

Стандартна помилка: `code`, `message`, `details` без секретів, `request_id`, `retryable`. Не зводити `DATA_STALE`, `RISK_REJECTED`, `INSUFFICIENT_MARGIN`, `BROKER_UNKNOWN` та `RATE_LIMITED` до одного generic error. Для команд перевіряти власність портфеля, режим, конфігураційну версію й допустимий стан. Навіть локальний API має захист від випадкового доступу через зовнішній інтерфейс; конкретний механізм auth визначити на етапі Foundation.
