# Backtesting і Replay

## Мета

Перевіряти стратегії на історичних даних без майбутньої інформації й відтворювати рішення того самого strategy/risk contract, що використовується в Paper Trading. Backtest є дослідницьким інструментом: його виконання та ліквідність є моделлю, а не історією реальних ордерів.

## Dataset

Dataset має ID, джерело, інструмент, часовий діапазон UTC, таймфрейм, тип ціни, версію нормалізації, checksum, виявлені пропуски і доступність funding/book. Імпорт перевіряє монотонність часу, дублікати, порожні свічки, зміни контракту й часові зони. Виправлення даних створює нову версію dataset.

Для S11/N2b research dataset окремий test-only export бере копії persisted synthetic `Candle`/`AnnouncementVersion` і незмінні coverage observations за UTC cutoff, передає їх N2a event-time builder та фіксує checksum. Поточний mutable candle row із `received_at` після anchor дає `WAIT` замість ретроспективної ознаки; цей export не є backtest run, не створює Paper ордерів і не дозволений для production news corpus без data-use gate.

## Event-time симуляція

Replay clock видає тільки події з часом `<= now`. Індикатори будуються лише з уже закритих свічок, якщо стратегія явно не підтримує поточну. Сигнал на свічці `t` виконується за наступною доступною котировкою/подією відповідно до моделі fill, а не за ціною, яку стратегія щойно «побачила» постфактум. За відсутності order book використовувати консервативну модель і позначати обмеження. Funding, fees, margin та risk rules беруться з версіонованих припущень.

## Replay UI

Швидкості ×1, ×10, ×50, ×100 є цільовими режимами керування, фактична швидкість відображається. Pause/step/seek допустимі лише якщо відновлюється повний стан стратегії та портфеля; інакше seek створює новий запуск із початку. Графік не показує майбутні свічки відносно replay clock.

## Метрики й аналіз

Звіт містить net/gross PnL, fees, funding, slippage, max drawdown, win rate, profit factor, кількість угод, експозицію, часовий період і список припущень. Для порівняння використовувати однакові dataset, початковий баланс, risk profile та cost model. Відокремлювати train/validation/out-of-sample періоди; оптимізація параметрів не може оцінюватися лише на тому самому періоді.

## Приймання

Повторний запуск із тим самим dataset/config/seed дає ідентичні fills і метрики; зміна майбутнього після `t` не змінює рішення до `t`; сценарії gap, spread, partial fill, funding і ліквідації мають золоті очікувані результати; звіт показує provenance й усі припущення.

## Реалізований backend мінімум S08 (2026-10-15)

`POST /api/v1/backtests/datasets` імпортує 2–2000 збережених `BTC_USDT` REST-final свічок для `1m|5m|15m`, перевіряє час, OHLCV, дублікати/порядок, позначає gap і фіксує `source_event_id`, source/receive UTC, payload hash, правила інструмента/version та доступні підтверджені funding settlements. Canonical JSON має SHA-256 checksum; повторний імпорт тотожного snapshot повертає наявний ID. `GET /api/v1/backtests/datasets/{id}` повертає snapshot. Виправлення джерела створює інший checksum/ID. Імпорт не виконує мережевих запитів до MEXC.

`POST /api/v1/backtests/datasets/{id}/runs` приймає `StrategyConfig`, `RiskProfile`, Decimal `deposit`, `seed`. Чистий replay використовує `evaluate()` із тих самих базових стратегій, видає final свічки лише після їх закриття і fill за close наступної final події. Розрив скасовує pending; неповна свічка не може потрапити в dataset. Кожен кандидат OPEN/CLOSE має причинні decision/intent/risk verdict IDs, UTC і `config_version`; модельний order/fill виникає лише за ALLOW. Звіт зберігає окремі від Paper `account`, `positions`, `decisions`, `intents`, `verdicts`, `orders`, `fills`, двосторонній збалансований `ledger`, `equity_curve`, `trades`, metrics та assumptions. `GET /api/v1/backtests/runs/{id}` повертає звіт. `POST /api/v1/backtests/compare` відхиляє різні dataset/інтервал/початковий баланс/risk profile/cost model/seed/config version.

Це модель **повного fill за наступним final close** із tick-rounded slippage і public taker fee; order book, spread, partial fills, intrabar path, maintenance margin/liquidation не симулюються. Stop, порушений на final close, дає CLOSE на наступній final події. Funding застосовується лише для відомих settlement ставок за наявного збіжного final close, тому `funding_coverage=unavailable|known_settlements_only` не означає повноту історії. Risk gate повторює caps профілю/контракту, але поки не є production `PaperService.check()`; поточний kill switch і live snapshots історичному запуску недоступні. Replay UI/speed та позавибіркова оцінка залишаються цільовими, а не реалізованими можливостями.
