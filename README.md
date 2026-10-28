# AI Futures Trading Platform

Git remote `origin`: [BulgakowS/ai-futures-bot](https://github.com/BulgakowS/ai-futures-bot).

Локальна платформа для дослідження ф'ючерсних стратегій на ринкових даних MEXC. Основний режим — **Paper Trading**: ринкові дані надходять з біржі, а ордери та баланс є віртуальними. Реалізовано FastAPI/PostgreSQL/Redis backend, market worker, Paper/Risk, три параметризовані стратегії та локальний Next.js UI. Опціональний AI, backtesting і Live Trading ще не реалізовані; повні критерії roadmap 3–5 лишаються відкритими.

## Ціль

Користувач обирає інструмент, створює віртуальний портфель і бота, бачить живі дані, рішення стратегії та AI, перевірку ризику, віртуальні виконання і результат. Кілька ботів можуть паралельно порівнюватися на тому самому потоці даних. Live Trading є майбутнім окремим етапом і за замовчуванням технічно заблокований.

## Прийняті рішення

- Один репозиторій, модульний Python backend і окремий Next.js frontend; локальний backend запускається через один Docker Compose із кількома контейнерами.
- Python/FastAPI, PostgreSQL, Redis, Next.js/React/TypeScript. Поточний графік — SVG; Tailwind CSS, shadcn/ui, Lightweight Charts і OpenAI API не інтегровані.
- Публічні REST/WS дані MEXC підтверджені для Paper Trading без торгового API-ключа; джерела, дата перевірки й обмеження наведені у [MEXC](docs/MEXC_API.md).
- Кожен торговий намір проходить незалежний Risk Engine. AI не має прямого доступу до брокера.
- Paper, backtest і майбутній live користуються спільними правилами сигналів і ризику, але окремими адаптерами виконання та ізольованими обліковими книгами.
- Інтерфейс має українську й англійську мови, темну тему та мобільну адаптацію. Рівні Simple / Advanced / Expert заплановані окремо.

## Локальний запуск

Потрібні Python 3.12 і Docker Compose.

```sh
make setup       # .venv та локальні секрети в .env (0600); існуючий .env зберігає
make up          # UI, API, trading, PostgreSQL, Redis
make smoke       # readiness і persisted AppState
make lint test
```

UI: `http://127.0.0.1:3030`; API/OpenAPI: `http://127.0.0.1:8000/docs`; liveness: `/api/v1/health/live`, readiness: `/api/v1/health/ready`, стан: `/api/v1/state`. Dev hot reload: `make dev`. Зупинка: `make down` (volumes збережені). DB і Redis не публікують портів. API_PORT можна змінити в `.env`, якщо 8000 зайнятий.

Усі backend мутації вимагають `Authorization: Bearer <BACKEND_AUTH_TOKEN>`; браузер використовує локальний same-origin BFF і не отримує секрет із `.env`. `APP_MODE=paper`, `LIVE_TRADING_ENABLED=false`; Live runtime відсутній. Початковий kill switch увімкнений, його потрібно явно зняти для нових paper входів. Worker обробляє public market data, ботів і paper позиції.

## Навігація

| Документ | Зміст |
|---|---|
| [PRD](docs/PRD.md) | Цілі, сценарії, обсяг, вимоги |
| [Архітектура](docs/ARCHITECTURE.md) | Модулі, потоки даних, контракти, відмови |
| [Roadmap](docs/ROADMAP.md) | Етапи, залежності, результати й критерії приймання |
| [UX](docs/UX_SPEC.md) | Екрани та взаємодії |
| [MEXC](docs/MEXC_API.md) | План адаптера, перевірки API та обмеження |
| [Paper Trading](docs/PAPER_TRADING.md) | Симуляція, облік, формули й крайові випадки |
| [Стратегії](docs/STRATEGIES.md) | Контракт стратегій і черговість реалізації |
| [Risk Engine](docs/RISK_ENGINE.md) | Правила, пріоритети, kill switch |
| [AI Engine](docs/AI_ENGINE.md) | Вхідні дані, рішення, журнал, бюджет |
| [Backtesting](docs/BACKTESTING.md) | Історичні тести та replay |
| [Дані](docs/DATABASE.md) | Сутності, ізоляція, аудит і зберігання |
| [API](docs/API.md) | Проєктний контракт HTTP та подій |
| [Тестування](docs/TESTING.md) | Матриця перевірок і докази приймання |
| [Розгортання](docs/DEPLOYMENT.md) | Локальний Compose та майбутній VPS |
| [Рішення й відкриті питання](docs/DECISIONS.md) | Зафіксовані рішення та припущення |

`AGENTS.md` задає правила роботи Codex. `configs/strategies/` і `configs/risk-profiles/` зарезервовані для майбутніх конфігурацій; зараз у них лише пояснення.

## Структура папок

```text
apps/web/                   # локальний Next.js paper UI та BFF
services/api/ai_futures/    # FastAPI, market, paper, risk, bots, стратегії
services/trading/           # async market/bot/paper worker
services/ai/                # майбутній AI worker
packages/exchanges/mexc/    # адаптер біржі
packages/strategies/        # логіка стратегій
packages/indicators/        # індикатори
packages/risk/              # незалежна перевірка ризику
packages/backtesting/       # історична симуляція
configs/strategies/         # пресети стратегій
configs/risk-profiles/      # профілі ризику
configs/presets/            # інші пресети
configs/prompts/            # версії AI-промптів
infra/docker/               # backend і web Dockerfiles
tests/unit/                 # тести розрахунків та інваріантів
tests/integration/          # перевірки взаємодії компонентів
tests/contract/             # перевірки API та схем
tests/e2e/                  # наскрізні сценарії
docs/                       # специфікації й плани
```

Зарезервовані порожні папки містять `.gitkeep`, щоб Git зберігав структуру. `.gitignore` виключає локальні секрети, залежності, кеші, ринкові дані й результати запусків.

## Статус

Фактичні результати й обмеження — у [IMPLEMENTATION](docs/IMPLEMENTATION.md). Локальний browser E2E підтвердив живі MEXC дані → бот → risk verdict → віртуальний fill/fee/ledger. Цей доказ не підтверджує прибутковість, точність біржового виконання або повне приймання всіх критеріїв roadmap.
