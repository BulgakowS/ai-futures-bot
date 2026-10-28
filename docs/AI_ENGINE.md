# AI Engine

AI — необов'язковий аналітик і джерело кандидатного сигналу. Відсутність OpenAI API-ключа не блокує ринкові дані, Paper Trading або базові стратегії. AI не отримує торгових credentials і не може безпосередньо викликати broker adapter.

## Вхід

Версіонований `MarketContext`: інструмент і таймфрейми, фінальні OHLCV, bid/ask, spread, доступна глибина, funding/open interest за наявності, обчислені індикатори, стан позиції та дозволений risk budget. Кожне поле має timestamp і прапорець якості. Контекст має бути обмежений за розміром і очищений від секретів та особистих даних. Скриншот графіка не є єдиним джерелом даних.

## Вихід і валідація

Відповідь моделі проходить JSON-schema/Pydantic validation. Пропоновані поля: `action` (`LONG|SHORT|WAIT|CLOSE`), `confidence` у `[0,1]`, `horizon`, `reason_codes`, коротке пояснення, `invalidation`, кандидатні entry/SL/TP і `context_id`. Відсутнє/невідоме значення, невалідна ціна, застарілий контекст або таймаут дають `WAIT` з кодом причини. `confidence` — оцінка моделі, не калібрована ймовірність прибутку.

## Політика рішень

Модель, версія системного промпту, температура/параметри, частота аналізу, таймфрейми, індикатори, денний/місячний ліміт запитів і витрат — конфігурація бота. Якщо AI і rule-based стратегія суперечать одне одному, за замовчуванням `WAIT`; інший режим арбітражу потребує явної версії конфігурації й тестів. Після AI-сигналу Risk Engine обов'язково перевіряє намір.

## Журнал і покращення

Зберігати контекст або його контрольовану незмінну копію, модель, версію промпту, hash параметрів, raw response з редагуванням секретів, validated decision, risk verdict, фактичні fills і результат. Оцінювати якість AI на відкладених наборах та paper forward tests. Зміни промптів/правил застосовувати як версіоновані експерименти; модель не «навчається» автоматично на кожній угоді.

## Відмови

Недоступність API, вичерпаний бюджет, rate limit, невалідна відповідь або stale context дають `WAIT`, alert та журнал. Повтор запиту має ідемпотентний `analysis_id`; повторне отримання відповіді не створює другий ордер. Вартість рахувати з фактичних usage даних провайдера, коли вони доступні; оцінку позначати як оцінку.

## S07: реалізований мінімальний backend-контур (2026-10-15)

Перевірено офіційні документи OpenAI через gstack `/browse` 2026-10-15: [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses) та [Create a response](https://developers.openai.com/api/reference/resources/responses/methods/create). Підтверджено `POST /v1/responses`, `text.format` із JSON schema/`strict`, `store=false`, `max_output_tokens`, `status`, `output_text` і `usage.input_tokens/output_tokens`. Конкретна доступність моделі для акаунта й актуальна ціна не перевірялися, оскільки живий OpenAI запит заборонений. Модель і тарифні коефіцієнти явно задає користувач у `AiPolicy`; неправильна модель поверне `WAIT` через provider error.

`POST /api/v1/ai/analyses` приймає UUID `analysis_id`, `portfolio_id` і `policy`. `policy` містить model, `prompt_version`, system prompt, timeframe, frequency, кількість/плече для кандидатного paper intent, денні/місячні ліміти запитів і USD, ставки USD за мільйон input/output tokens, `max_output_tokens`, `submit_to_paper` (за замовчуванням `false`). Це разовий portfolio-scoped запуск, а не bot scheduler. Зміни політики версіонуються лише `prompt_version` і hash запиту в журналі; незмінна bot-level AI config, порівняння версій та UI пояснення залишаються відкритими для повного етапу 6.

`market-context-v1` збирається тільки з білого списку: BTC_USDT, 1m/5m/15m, до 20 REST-final OHLCV, актуальні bid/ask, одна paper-позиція за наявності та `risk_per_trade`. Кожна свічка має `event_id/source_time/quality`, ticker має `event_id/source_time/quality`, позиція має `opened_at/quality`; контекст має `context_id` SHA-256 і межу 12 KB. `market_quality` і вік останньої фінальної свічки перевіряються перед запитом. Depth/funding/OI, індикатори та окремий `spread` поки не передаються; spread обчислюється з bid/ask. Дані з браузера, exchange credentials, raw MEXC payload і персональні поля не входять у prompt.

Вихід провайдера проходить strict JSON schema та `AiProposal` Pydantic validation, зокрема action, confidence, reason codes, horizon, context ID та ціни. Порожній/невалідний/чужий context ID, timeout, rate limit, missing key, stale, frequency/budget cap і provider failure стають `WAIT`; `AiAnalysis` фіксує причину. `AI_ENABLED=false` за замовчуванням; `OPENAI_API_KEY` береться лише із server-side середовища. Вебконтейнер більше не успадковує `.env`: він отримує тільки backend token і URL. Raw response та prompt текст не зберігаються; журнал має SHA-256 prompt, sanitized context, validated proposal, model/version, timestamps, config version і, за наявності, paper intent/risk/order IDs. Журнал `AiUsage` зберігає фактичні token counts і **оцінку** USD за заданими ставками; зовнішній рахунок OpenAI є джерелом істини для фактичних витрат.

Перед API call у PostgreSQL резервується оцінка для 24 000 input tokens і заданого `max_output_tokens`; всі спроби, включно з невизначеним результатом після збою, споживають резерв для денного/місячного cap. Після відповіді більша фактична оцінка підвищує резерв. Це локальний консервативний cap, а не гарантія рахунку провайдера: токенізація/тарифи можуть відрізнятися, тому звірка з білінгом потрібна перед реальним використанням ключа. Унікальний `analysis_id` і незмінний paper idempotency key `ai:<analysis_id>` не допускають другого ордера; незавершений виклик після 30 секунд стає `REQUEST_UNCERTAIN`/`WAIT` без повторного API запиту. Лише `PENDING_PAPER` може повторно доставити вже збережений intent через `PaperService.submit()`. `submit_to_paper=false` лишає пропозицію аналізом; `true` завжди проходить Paper Risk Engine, включно з kill switch і повторною перевіркою stale даних.

Немає автоматичного змішування AI з rule-based ботом, отже арбітраж їхніх суперечливих сигналів із цільової політики ще не реалізовано. Немає AI worker/scheduler, UI, alert delivery, калібрування confidence, live OpenAI contract test, provider billing reconciliation чи forward evaluation. Paper Trading та базові боти продовжують працювати без ключа.

## S11/N4a: локальний offline forecast contract

Окремий news+market `ForecastContext` має тільки минулі final BTC_USDT 4h свічки, as-of доступні версії заголовків і coverage. `predict()` читає feature-only поля, тому training `Sample` із future label не потрібний для inference. Пошкоджений/застарілий контекст, gap/edit/backfill або відсутній approved artifact дають `WAIT`. Поточний synthetic approval обмежений `APP_ENV=test`; `FORECAST_FIXTURE_ENABLED=false` за замовчуванням. Лише test fixture може отримати Decimal/tick-rounded кандидатні entry/SL/TP зі свіжих validated rules. Сервіс не має PaperService/broker/API/UI підключення; реальна модель і її право на дані не підтверджені.

## S11/G5: окреме GKG v2 synthetic training

`gdelt_gkg_training.py` приймає лише sealed `gdelt-gkg-market-dataset-v2` з точними synthetic market/GKG provenance versions і очікуваним `source_checksum`. Він не використовує старі title-based чи DOC artifacts. `FeatureInput` містить тільки as-of числові ознаки та причинні ID; label і майбутня Candle не входять у prediction. Для 4h sample `decision_at` уже визначений G4/T1, а target є nominal next close в `anchor+4h`. Зважений tone за нульового BTC theme count лишається `NULL`, окремий `tone_missing=1`; числового tone не домислюємо.

Два локальні multinomial SGD artifacts (`market-only`, `gkg+market`) мають спільні train/validation/test UTC межі та embargo ≥4h. Фіксовані algorithm/hyperparameters/seed, версії Python і залежностей, code SHA-256, source/config/dataset checksums, validation threshold і weights входять у sealed artifact. Fit читає лише train, поріг — лише validation; test і два послідовні блоки не налаштовують модель. Звіт має Brier, log loss, 3-bin calibration, `WAIT` і явні synthetic fee/funding/slippage assumptions. Вихід завжди `collect-only` щодо promotion: немає real source cohort, GKG-compatible freeze, Paper wiring чи production approval.

## S11/N5b: GKG numeric preregistration, без runtime evaluator

[N5b protocol](plans/2026-10-19-s11-n5b-gkg-forward-protocol.md) і [config](../configs/forward/s11-n5b-gkg-v1.json) окремі від DOC N5a. Вони фіксують 56 UTC діб, 336 anchors, повні 16 as-of GKG slots, фактичний `decision_at≤anchor+15m`, target наступного nominal 4h close і paired `market-only`/`gkg+market` оцінку з витратами, `WAIT` та day-block CI. G5 synthetic artifacts не є real-source freeze. Відлік не починається без перевірених real artifacts і manifest із immutable market/rules/cost provenance; поточний verdict `collect-only`, автоматичного Paper або Live activation немає.

## S11/G5b: persisted real-source eligibility, без fit

`gdelt_gkg_real_training.py` читає тільки test-only disposable SQLite G3/M1 export v3, перевіряє sealed G4 v2 dataset, IDs ревізій/слотів/M1 observations, source versions, запізнення/корекції та повну cohort із chronological split й embargo. G4 versioned builder тепер вибирає label для кожного anchor as-of `target_close_at+15m`; пізніша M1 correction не переписує label. Старий G5 synthetic шлях збережено. Довільний `Config.cost_bps` або текстовий M1 `rules_version` не доводять effective/observed rules і `2×fee+funding+slippage`, тому реальний шлях повертає `collect-only/missing_cost_provenance` без fit, artifacts, metric чи Paper activation. Disposable fixture — перевірка контракту, не реальний GKG/market доказ.

## S11/G7b/N5c: sparse v3 без моделі

Окремий `gdelt-gkg-sparse-market-dataset-v3` з однією G7a числовою observation на anchor має власні as-of feature hashes, immutable M1 IDs/source/rules/config seals і next nominal 4h close label as-of target+15m. `Config.cost_bps` у цьому test-only dataset є явно сценарним; `eligibility=collect-only` навіть якщо caller позначив cost frozen. [N5c config](../configs/forward/s11-n5c-gkg-sparse-v1.json) фіксує 56 діб/336 one-slot anchors, ≤5 GB загальний cap і парні пороги до реального forward. Старі G5b/N5b лишаються 16-slot `collect-only`.

## S11/G7c: synthetic-only sparse model contract

`gdelt_gkg_sparse_training.py` навчає окрему детерміновану пару `market-only` і `sparse-gkg+market` лише на явно synthetic G7b dataset. Ознаки містять тільки числові GKG aggregate та as-of M1 market return, без label/target candle. Chronological train/validation/test мають embargo ≥4h; поріг `WAIT` обирається тільки на validation. Артефакти фіксують code/dependency/dataset/source/config checksums, версії, seed, межі й ваги. Парний test звіт є технічною перевіркою; [G7c evidence](evidence/s11-g7c-sparse-synthetic-training-2026-10-20.json) не доводить прогнозну цінність. `gdelt_gkg_sparse_real_training.py` читає persisted temp export і повертає `collect-only` без реальних artifacts навіть за повної fixture, доки немає перевіреного effective/observed rules, account fee, funding/slippage та реального freeze. Production inference, N5c clock і автоматичний Paper не підключені. Для майбутнього live-as-of inference потрібен окремий контракт ознак без прив'язки нового рішення до checksum навчального dataset.

## S11/G7d: synthetic-only offline as-of inference

`project_as_of_features()` приймає рівно один synthetic G7a attempt/result і два immutable M1 Hour4 observation для нового UTC 4h anchor. Він вимагає `ok`, відомі synthetic source/config/rules versions, version 1 без наданої correction, as-of видимість до `anchor+15m`, два попередні final вікна і валідні числові aggregate; missing/error/late/unknown відхиляє. Обчислення feature hashes тотожне G7b, але тип `AsOfFeatureInput` не містить label, target candle або checksum нового dataset. `freeze_training_manifest()` зберігає окремий seal двох G7c artifacts, training dataset/config/source checksums і source schema. `predict_as_of()` перевіряє artifact checksum, frozen manifest, schema/versions, feature hash та `anchor>test_end`; checksum нових source observations закономірно відрізняється від training source checksum і не порівнюється з ним. Frozen manifest має бути збережений і переданий незалежно від нового row; локальний SHA-256 seal виявляє випадкове пошкодження, але не є підписом або зовнішньою атестацією джерела. Контракт лише offline synthetic; він не читає DB/мережу й не підключений до production inference, N5c clock, Paper чи Live. Перед реальним inference потрібні trusted source snapshot, correction/coverage gate, verified rules/cost freeze і окремі real artifacts.
