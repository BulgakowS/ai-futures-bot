# Модель даних

PostgreSQL — джерело істини для конфігурацій, позицій, ордерів, fills, ledger та журналу рішень. Redis призначений для черг, короткоживучого cache і fan-out; його очищення не повинно втратити баланс або створити нові угоди. Нижче — логічна модель, не схема реалізованої БД.

## Основні сутності

| Сутність | Ключові поля / зв'язки | Правило |
|---|---|---|
| `market_instrument` | canonical ID, exchange symbol, contract metadata version, status | Історичні версії не переписувати |
| `market_event` / `candle` | instrument, source/receive time, sequence, quality, raw ref | Унікальність за ключем джерела; provenance |
| `portfolio` | ID, mode, currency, start balance, epoch, status | Ізоляція режимів і ботів |
| `bot` / `bot_config_version` | portfolio, strategy, risk profile, AI policy, status | Запуск посилається на незмінну версію |
| `decision` | bot, context ID, strategy/AI output, reason, timestamp | Записувати також WAIT і помилки |
| `risk_verdict` | decision/intent, rules version, status, adjustments, codes | Обов'язковий перед ордером |
| `order_intent` / `order` | idempotency key, broker mode, state, amount, price | Окремий intent і стан ордера |
| `fill` | order, price, quantity, fee, source event | Append-only, дедуплікація fill ID |
| `position` | portfolio, instrument, side, quantity, entry, margin, SL/TP | Проєкція з fills і ledger |
| `ledger_transaction` / `ledger_entry` | portfolio, account, amount, currency, event ID | Append-only, дебети = кредити |
| `funding_event` | position, rate, amount, source time, source | Не вигадувати при відсутності даних |
| `backtest_run` / `dataset` | checksums, config versions, seed, metrics | Відтворюваність |
| `audit_event` / `outbox_event` | actor, action, correlation ID, payload ref | Незмінний причинний слід |

## Цілісність

Кожен `order` має попередній `risk_verdict` з режимом і портфелем, що збігаються. Кожен `fill` належить одному ордеру; `filled_quantity <= ordered_quantity`. Кожен ledger transaction збалансований за валютою. Унікальні ключі оберігають market event, команду, ордер і fill від повтору. Cash/equity-проєкції відтворюються з ledger; розбіжність спричиняє halt. Всі суми зберігаються як exact decimal, час — UTC з timezone.

## Ізоляція й зберігання

Paper, backtest і live мають окремі namespace/режимні обмеження. Запит UI ніколи не поєднує баланси різних режимів без явно позначеної агрегації. Market raw data можуть мати обмежений retention, але посилання, checksum та мінімальний snapshot для аудиту рішень зберігаються за визначеною політикою. Політику retention, обсяг історії та backup encryption треба затвердити на етапі 0/1. Секрети не зберігати в цій схемі у відкритому вигляді.

## Міграції та відновлення

Кожна зміна схеми має forward migration і перевірений backup/restore сценарій. Після рестарту система читає незавершені ордери, звіряє їх із брокером відповідного режиму, відновлює проєкції та не запускає нові входи, доки невідомі стани не вирішені. Для paper reconciliation використовувати внутрішній журнал подій; для майбутнього live — підтверджені дані біржі.
