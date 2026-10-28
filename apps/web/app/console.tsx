"use client";

import { FormEvent, ReactNode, useCallback, useEffect, useMemo, useState } from "react";
import { overviewDegradedReason, overviewTradeEligible, parseOverviewResponse } from "./overview-status";

type Row = Record<string, any>;
type Page = "dashboard" | "bots" | "positions" | "journal" | "research" | "settings";
type Language = "uk" | "en";
type Load = "loading" | "ready" | "error";

const words = {
  uk: {
    dashboard: "Огляд", bots: "Боти", positions: "Позиції", journal: "Журнал", research: "Дослідження / модель", settings: "Налаштування",
    researchStatus: "Лише збір та аналіз", researchEmpty: "Прогнозів ще немає. Реальна news-модель не запущена.",
    researchNote: "Синтетичні тести не доводять якість моделі на MEXC. Автоматичних paper-ордерів тут немає.",
    researchScores: "Оцінки класів моделі, не ймовірність прибутку", researchTest: "TEST · синтетичний fixture",
    researchProvenance: "Походження рішення", researchNoExecution: "Тільки перегляд; виконання недоступне",
    paper: "PAPER · віртуальні кошти", data: "Якість даних", active: "Дані актуальні", degraded: "Дані обмежені",
    equity: "Equity", balance: "Доступно", unrealized: "Нереалізований PnL", price: "Ціна MEXC",
    source: "Джерело", updated: "Час джерела", market: "Ринок", portfolio: "Портфель", noPortfolio: "Портфелів ще немає",
    createPortfolio: "Створити paper портфель", createBot: "Створити бота", name: "Назва", deposit: "Стартовий баланс (USDT)",
    create: "Створити", cancel: "Скасувати", strategy: "Стратегія", timeframe: "Таймфрейм", risk: "Risk profile",
    review: "Перевірка", start: "Запустити", ready: "Підготувати", pause: "Пауза", stop: "Зупинити",
    status: "Стан", actions: "Дії", noBots: "Ботів ще немає", noPositions: "Відкритих позицій немає",
    close: "Закрити позицію", confirmClose: "Підтвердіть paper закриття через risk gate. Результат залежить від актуальних даних.",
    kill: "Kill switch", enabled: "Увімкнений", disabled: "Вимкнений", turnOn: "Увімкнути блокування", turnOff: "Зняти блокування",
    confirmKill: "Kill switch блокує нові входи. Захисні дії зменшення ризику лишаються під контролем risk engine. Продовжити?",
    confirmUnkill: "Зняти блокування нових входів? Перед запуском перевірте якість ринку.",
    assumption: "Котирування MEXC — публічні. Ордери та баланс — симуляція; виконання за depth є оцінкою.",
    noJournal: "Подій у вибраному портфелі ще немає", decision: "Рішення", botDecision: "Рішення бота", verdict: "Risk verdict", orders: "Ордери", fills: "Виконання", ledger: "Баланс і витрати",
    selectPortfolio: "Оберіть портфель", retry: "Повторити", loading: "Завантаження…", error: "Сервіс недоступний або повернув помилку.",
    notAvailable: "Недоступно", estimated: "оцінка", finalCandles: "Фінальні OHLCV свічки", noCandles: "Фінальних свічок поки немає",
    noProfitClaim: "Параметри не є торговою рекомендацією. Результат стратегії не підтверджений backtest/forward paper перевіркою.",
    reviewCheck: "Я розумію: це paper симуляція, виконання та ліквідаційні рівні оцінені, прибутковість не доведена.",
    allFields: "Укажіть усі параметри; готових перевірених пресетів немає.",
    noAi: "OpenAI не потрібен для базових стратегій. AI у цьому етапі не підключено.",
    refresh: "Оновити", updatedAt: "Оновлено", mode: "Режим", config: "Версія конфігурації", newest: "Останні рішення",
    rule: "Правила інструмента", emptyRisk: "Risk verdict ще немає", utc: "Усі часи UTC", positionHistory: "Історія позицій",
    pilotSettings: "Ліміти майбутнього Live pilot", pilotDisabled: "Live вимкнено. Збереження цих лімітів не дозволяє реальні ордери.",
    pilotMissing: "Ліміти ще не задано; Live недоступний.", pilotSave: "Зберегти чернетку", pilotVersion: "Версія чернетки",
    maxPairs: "Максимум одночасних пар (до 3)", perPairNotional: "Повний notional на пару, з плечем",
    aggregateNotional: "Сумарний повний notional, з плечем", maxLeverage: "Максимальне плече",
    takeProfitPct: "Take profit (%)", stopLossPct: "Stop loss (%)", maxDailyLoss: "Максимальна денна втрата",
    maxRoundTripCostPct: "Максимальні оцінені витрати входу й виходу (%) · 0.5 = 0.5%",
    limitValue: "Значення", limitUnit: "Одиниця", pctEquity: "% equity (5 = 5%)", pilotSaveError: "Не вдалося зберегти чернетку.",
    botResults: "Результат кожного Paper бота", currentEpoch: "Поточна епоха", netResult: "Чистий результат", realizedNet: "Реалізовано з витратами", unrealizedResult: "Нереалізовано", profit: "Прибуток", loss: "Збиток", flat: "Без змін", valuationUnknown: "Оцінка невідома", accountUnavailable: "Дані рахунку недоступні", viewBot: "До бота", viewJournal: "Журнал бота", resultNote: "USDT · поточна епоха · після проведених комісій і funding; відкритий PnL є оцінкою.",
    simpleMode: "Простий · модель", advancedMode: "Ручне налаштування", modelDraftTitle: "Чернетка бота на основі моделі",
    modelDraftNote: "Реальна модель ще не пройшла forward-перевірку. Можна зберегти ліміти, але запуск та автоматичні Paper-ордери поки заблоковані.",
    modelDraftCard: "Модель очікує перевірки · запуск заблоковано", maxPositionNotional: "Максимальна позиція (USDT)", simpleLeverage: "Плече (1–3×)",
    simpleStopLoss: "Макс. відстань Stop loss (%)", simpleTakeProfit: "Макс. відстань Take profit (%)", createDraft: "Зберегти чернетку",
    simpleReview: "Я розумію, що це лише Paper-чернетка: модель не запускається та ордерів не створює.",
    simpleLimitNote: "Сума — межа повної позиції з урахуванням плеча, не гарантія її використання. SL/TP — верхні межі відстані від ціни входу; після окремого схвалення модель зможе вибрати точні додатні значення в цих межах. Майбутній Risk Engine може дозволити менший обсяг або відхилити вхід.",
    connections: "Біржові підключення", connectionsNote: "Ключі зберігаються зашифрованими. Збереження або вибір ключа не вмикає Live торгівлю.",
    addConnection: "Додати підключення", connectionLabel: "Назва підключення", exchange: "Біржа", apiKey: "API key", apiSecret: "API secret",
    rotateConnection: "Замінити ключі", revokeConnection: "Відкликати", revokeConfirm: "Відкликати цей ключ? Поточний секрет буде стерто, а прив’язки ботів стануть недійсними.",
    noConnections: "Підключень ще немає", vaultUnavailable: "Сховище ключів недоступне. Перевірте локальний master key та API.",
    connectionError: "Не вдалося зберегти підключення", connectionActive: "Активне", connectionRevoked: "Відкликане",
    selectConnection: "Підключення для майбутнього Live", connectionSelectionNote: "Це лише чернетка. Paper бот не використовує ключ, Live ордери недоступні.",
    selectionMissing: "Підключення не вибрано", selectionInvalid: "Вибір застарів після заміни або відкликання ключа. Виберіть активний запис ще раз.",
    saveSelection: "Зберегти вибір", futureExchanges: "Інші біржі з’являться після окремої інтеграції.",
  },
  en: {
    dashboard: "Overview", bots: "Bots", positions: "Positions", journal: "Journal", research: "Research / Model", settings: "Settings",
    researchStatus: "Collect and analyze only", researchEmpty: "No forecasts yet. The real news model is not running.",
    researchNote: "Synthetic tests do not establish model quality on MEXC. There are no automatic paper orders here.",
    researchScores: "Model class scores, not probability of profit", researchTest: "TEST · synthetic fixture",
    researchProvenance: "Decision provenance", researchNoExecution: "View only; execution unavailable",
    paper: "PAPER · virtual funds", data: "Data quality", active: "Data current", degraded: "Data degraded",
    equity: "Equity", balance: "Available", unrealized: "Unrealized PnL", price: "MEXC price",
    source: "Source", updated: "Source time", market: "Market", portfolio: "Portfolio", noPortfolio: "No portfolios yet",
    createPortfolio: "Create paper portfolio", createBot: "Create bot", name: "Name", deposit: "Starting balance (USDT)",
    create: "Create", cancel: "Cancel", strategy: "Strategy", timeframe: "Timeframe", risk: "Risk profile",
    review: "Review", start: "Start", ready: "Ready", pause: "Pause", stop: "Stop",
    status: "Status", actions: "Actions", noBots: "No bots yet", noPositions: "No open positions",
    close: "Close position", confirmClose: "Confirm paper close through the risk gate. The result depends on current data.",
    kill: "Kill switch", enabled: "Enabled", disabled: "Disabled", turnOn: "Block new entries", turnOff: "Remove block",
    confirmKill: "The kill switch blocks new entries. Protective risk reduction still passes through the risk engine. Continue?",
    confirmUnkill: "Remove the block on new entries? Check market data quality before starting.",
    assumption: "MEXC quotes are public. Orders and balance are simulated; depth based fills are estimates.",
    noJournal: "No events for this portfolio yet", decision: "Decisions", botDecision: "Bot decisions", verdict: "Risk verdict", orders: "Orders", fills: "Fills", ledger: "Balance and costs",
    selectPortfolio: "Select portfolio", retry: "Retry", loading: "Loading…", error: "Service unavailable or returned an error.",
    notAvailable: "Unavailable", estimated: "estimate", finalCandles: "Final OHLCV candles", noCandles: "No final candles yet",
    noProfitClaim: "Parameters are not trading advice. No backtest or forward paper result validates this strategy.",
    reviewCheck: "I understand: this is a paper simulation; fills and liquidation levels are estimates, profitability is unproven.",
    allFields: "Set every parameter; there are no validated presets.",
    noAi: "OpenAI is optional for basic strategies. AI is not connected in this stage.",
    refresh: "Refresh", updatedAt: "Updated", mode: "Mode", config: "Config version", newest: "Recent decisions",
    rule: "Instrument rules", emptyRisk: "No risk verdict yet", utc: "All times UTC", positionHistory: "Position history",
    pilotSettings: "Future Live pilot limits", pilotDisabled: "Live is disabled. Saving these limits does not allow real orders.",
    pilotMissing: "Limits are unset; Live is unavailable.", pilotSave: "Save draft", pilotVersion: "Draft version",
    maxPairs: "Maximum simultaneous pairs (up to 3)", perPairNotional: "Full notional per pair, including leverage",
    aggregateNotional: "Aggregate full notional, including leverage", maxLeverage: "Maximum leverage",
    takeProfitPct: "Take profit (%)", stopLossPct: "Stop loss (%)", maxDailyLoss: "Maximum daily loss",
    maxRoundTripCostPct: "Maximum estimated entry and exit cost (%) · 0.5 = 0.5%",
    limitValue: "Value", limitUnit: "Unit", pctEquity: "% equity (5 = 5%)", pilotSaveError: "Could not save draft.",
    botResults: "Paper bot results", currentEpoch: "Current epoch", netResult: "Net result", realizedNet: "Realized after costs", unrealizedResult: "Unrealized", profit: "Profit", loss: "Loss", flat: "No change", valuationUnknown: "Valuation unknown", accountUnavailable: "Account data unavailable", viewBot: "View bot", viewJournal: "Bot journal", resultNote: "USDT · current epoch · after posted fees and funding; open PnL is an estimate.",
    simpleMode: "Simple · model", advancedMode: "Manual setup", modelDraftTitle: "Model bot draft",
    modelDraftNote: "The real model has not passed forward evaluation. You can save limits, but starting it and automatic Paper orders remain blocked.",
    modelDraftCard: "Model awaiting validation · start blocked", maxPositionNotional: "Maximum position (USDT)", simpleLeverage: "Leverage (1–3×)",
    simpleStopLoss: "Max stop loss distance (%)", simpleTakeProfit: "Max take profit distance (%)", createDraft: "Save draft",
    simpleReview: "I understand this is only a Paper draft: the model cannot start or place orders.",
    simpleLimitNote: "The amount caps full position value including leverage; it does not guarantee that amount will be used. SL/TP are maximum distances from entry; after separate approval, the model may choose exact positive values within those caps. The future Risk Engine may allow less or reject an entry.",
    connections: "Exchange connections", connectionsNote: "Keys are stored encrypted. Saving or selecting a key does not enable Live trading.",
    addConnection: "Add connection", connectionLabel: "Connection name", exchange: "Exchange", apiKey: "API key", apiSecret: "API secret",
    rotateConnection: "Replace keys", revokeConnection: "Revoke", revokeConfirm: "Revoke this key? Its current secret will be erased and bot selections will become invalid.",
    noConnections: "No connections yet", vaultUnavailable: "Credential vault unavailable. Check the local master key and API.",
    connectionError: "Could not save connection", connectionActive: "Active", connectionRevoked: "Revoked",
    selectConnection: "Connection for future Live", connectionSelectionNote: "This is only a draft. The Paper bot does not use this key; Live orders are unavailable.",
    selectionMissing: "No connection selected", selectionInvalid: "This selection became invalid after a key replacement or revocation. Select an active record again.",
    saveSelection: "Save selection", futureExchanges: "Other exchanges will appear after a separate integration.",
  },
};

const fieldHelp: Record<Language, Record<string, string>> = {
  uk: {
    name: "Назва цього Paper бота. Допомагає відрізняти його журнал і результат від інших ботів.",
    deposit: "Віртуальний стартовий баланс окремого Paper портфеля в USDT. Це не поповнення біржового рахунку.",
    strategy: "Правило, за яким бот шукає сигнал. Жодна стратегія тут не має доведеної прибутковості.",
    timeframe: "Тривалість однієї фінальної свічки MEXC: 1m, 5m або 15m. Менший інтервал дає частіші можливі рішення.",
    quantity: "Кількість контрактів у намірі на ордер. Більша кількість збільшує експозицію та можливий збиток; діють правила інструмента і risk gate.",
    leverage: "Плече стратегії для наміру на ордер. Підсилює як прибуток, так і збиток; не може перевищувати ліміт risk profile.",
    stop_distance: "Відстань захисного stop від ціни входу як частка: 0.01 = 1%. Менше значення може частіше зупиняти позицію.",
    fast_period: "Кількість фінальних свічок для швидкого індикатора. Має бути меншою за slow period.",
    slow_period: "Кількість фінальних свічок для повільного індикатора. Більше значення повільніше реагує на зміну ціни.",
    breakout_period: "Кількість попередніх фінальних свічок для пошуку межі пробою.",
    breakout_buffer: "Додатковий відступ за межею пробою як частка ціни: 0.01 = 1%. Більший буфер вимагає сильнішого руху.",
    min_volume_ratio: "Мінімальне відношення поточного обсягу до базового обсягу стратегії: 1 = однакові обсяги, 2 = удвічі більший.",
    max_leverage: "Найбільше плече, яке risk gate дозволить цьому Paper боту. Вище плече збільшує ризик ліквідації.",
    risk_per_trade: "Максимальний ризик однієї угоди як частка equity: 0.005 = 0.5%. Це обмеження, а не очікуваний збиток.",
    max_total_risk: "Сумарний допустимий ризик відкритих позицій як частка equity: 0.015 = 1.5%.",
    max_daily_loss: "Межа денного збитку як частка equity: 0.03 = 3%. Після досягнення нові входи блокуються.",
    max_drawdown: "Межа падіння equity від попереднього максимуму як частка: 0.10 = 10%. Обмежує новий ризик.",
    max_positions: "Максимальна кількість одночасно відкритих Paper позицій цього профілю.",
    max_notional_equity: "Граничне відношення повної вартості позицій до equity: 3 = notional до 3× equity, включно з ефектом плеча.",
    slippage_bps: "Граничне прослизання у базисних пунктах: 10 bps = 0.1% ціни. Обмежує допустиму оцінку виконання.",
    depth_participation: "Максимальна частка доступної глибини книги для оцінки ордера: 0.10 = 10%. Менше значення стримує великі ордери.",
    reviewCheck: "Підтвердження розуміння Paper симуляції та її невизначеності. Потрібне перед створенням бота; не відкриває Live торгівлю.",
    maxPairs: "Максимум різних одночасно відкритих пар у майбутньому pilot: від 1 до 3. Це лише чернетка Live ліміту.",
    maxLeverage: "Верхня межа плеча для майбутнього pilot. Реальна дозволена межа також залежатиме від актуальних правил MEXC.",
    perPairNotional: "Повна вартість експозиції однієї пари з урахуванням плеча, а не лише внесена маржа.",
    aggregateNotional: "Сумарна повна експозиція всіх пар з урахуванням плеча. Ліміт має враховувати позиції та очікувані ордери.",
    maxDailyLoss: "Найбільший допустимий денний збиток майбутнього pilot. У % equity значення має бути меншим за 100.",
    takeProfitPct: "Цільове відхилення ціни виходу від входу у відсотках: 5 = 5%. Досягнення цілі не гарантує виконання.",
    stopLossPct: "Захисне відхилення ціни від входу у відсотках: 5 = 5%. Прослизання може збільшити фактичний збиток.",
    maxRoundTripCostPct: "Бюджет оцінених витрат входу та захисного виходу у відсотках від повного notional: 0.5 = 0.5%. Включає можливу taker комісію виходу.",
    limitValue: "Числова величина цього ліміту в одиниці, вибраній праворуч. Для % equity введіть 5, щоб задати 5%.",
    limitUnit: "USDT — фіксована сума; % equity — частка поточної equity, де 5 означає 5%, а не 0.05.",
    maxPositionNotional: "Найбільша повна вартість однієї майбутньої Paper позиції в USDT, включно з плечем. Це верхня межа, а не сума, яку бот зобов'язаний використати.",
    simpleLeverage: "Плече майбутньої Paper позиції від 1× до 3×. Збільшує можливі прибутки та збитки; максимальна позиція означає повну вартість із цим плечем, а не маржу.",
    simpleStopLoss: "Верхня межа відстані захисного рівня від ціни входу: 1 означає максимум 1%. Після схвалення модель зможе вибрати точне значення >0 і ≤ цієї межі. Фактичний збиток може перевищити SL через прослизання.",
    simpleTakeProfit: "Верхня межа відстані цільового рівня від ціни входу: 2 означає максимум 2%. Після схвалення модель зможе вибрати точне значення >0 і ≤ цієї межі. Досягнення TP не гарантує виконання ордера.",
    simpleReview: "Підтверджує, що навчена модель поки не схвалена для автоматичних Paper угод і цей бот не запускається.",
  },
  en: {
    name: "Name of this Paper bot. It distinguishes its journal and result from other bots.",
    deposit: "Virtual starting balance of a separate Paper portfolio in USDT. No funds are added to an exchange account.",
    strategy: "Rule the bot uses to look for a signal. None of these strategies has proven profitability.",
    timeframe: "Length of one final MEXC candle: 1m, 5m, or 15m. Shorter intervals may produce more frequent decisions.",
    quantity: "Number of contracts in an order intent. More contracts increase exposure and potential loss; instrument rules and the risk gate apply.",
    leverage: "Strategy leverage for an order intent. It magnifies gains and losses and cannot exceed the risk profile limit.",
    stop_distance: "Protective stop distance from entry as a fraction: 0.01 = 1%. A shorter distance can stop a position more often.",
    fast_period: "Number of final candles for the fast indicator. Must be less than slow period.",
    slow_period: "Number of final candles for the slow indicator. Higher values respond more slowly to price changes.",
    breakout_period: "Number of previous final candles used to find a breakout boundary.",
    breakout_buffer: "Extra distance beyond a breakout boundary as a price fraction: 0.01 = 1%. A larger buffer requires a stronger move.",
    min_volume_ratio: "Minimum ratio of current to baseline strategy volume: 1 = equal volume, 2 = twice as much.",
    max_leverage: "Maximum leverage the risk gate allows this Paper bot. Higher leverage increases liquidation risk.",
    risk_per_trade: "Maximum risk per trade as an equity fraction: 0.005 = 0.5%. This is a limit, not an expected loss.",
    max_total_risk: "Total allowed risk across open positions as an equity fraction: 0.015 = 1.5%.",
    max_daily_loss: "Daily loss limit as an equity fraction: 0.03 = 3%. New entries are blocked after the limit is reached.",
    max_drawdown: "Maximum equity decline from its previous peak as a fraction: 0.10 = 10%. Limits new risk.",
    max_positions: "Maximum number of simultaneously open Paper positions under this profile.",
    max_notional_equity: "Maximum full position value relative to equity: 3 = notional up to 3× equity, including leverage.",
    slippage_bps: "Maximum slippage in basis points: 10 bps = 0.1% of price. Limits acceptable estimated execution cost.",
    depth_participation: "Maximum share of available order book depth used to estimate an order: 0.10 = 10%. Lower values restrain larger orders.",
    reviewCheck: "Acknowledges the Paper simulation and its uncertainty. Required before creating the bot; it does not enable Live trading.",
    maxPairs: "Maximum distinct simultaneously open pairs in the future pilot: 1 to 3. This is only a Live limit draft.",
    maxLeverage: "Upper leverage limit for the future pilot. Actual allowed leverage also depends on current MEXC rules.",
    perPairNotional: "Full exposure value for one pair including leverage, not only the posted margin.",
    aggregateNotional: "Combined full exposure across all pairs including leverage. The limit must cover positions and pending orders.",
    maxDailyLoss: "Maximum allowed daily loss for the future pilot. As % equity, the value must be below 100.",
    takeProfitPct: "Target exit price move from entry in percent: 5 = 5%. Reaching the target does not guarantee a fill.",
    stopLossPct: "Protective price move from entry in percent: 5 = 5%. Slippage may increase the actual loss.",
    maxRoundTripCostPct: "Estimated entry and protective exit cost budget as a percent of full notional: 0.5 = 0.5%. Includes possible taker exit fee.",
    limitValue: "Numeric amount of this limit in the unit selected to the right. For % equity, enter 5 to set 5%.",
    limitUnit: "USDT is a fixed amount; % equity is a share of current equity, where 5 means 5%, not 0.05.",
    maxPositionNotional: "Maximum full value of one future Paper position in USDT, including leverage. This is a cap, not an amount the bot must use.",
    simpleLeverage: "Leverage for a future Paper position, from 1× to 3×. It magnifies gains and losses; maximum position means full value including this leverage, not margin.",
    simpleStopLoss: "Maximum protective level distance from entry: 1 means at most 1%. After approval, the model may choose an exact value >0 and ≤ this cap. Slippage may make the actual loss exceed the stop loss.",
    simpleTakeProfit: "Maximum target level distance from entry: 2 means at most 2%. After approval, the model may choose an exact value >0 and ≤ this cap. Reaching the take profit price does not guarantee a fill.",
    simpleReview: "Acknowledges that the trained model is not yet approved for automatic Paper trades and this bot cannot start.",
  },
};

function FieldInfo({ id, label, help, language }: { id: string; label: string; help: string; language: Language }) {
  return <span className="field-help"><button type="button" className="field-help-button" aria-label={`${language === "uk" ? "Пояснення поля" : "Help for"} ${label}`} aria-describedby={`${id}-help`}>i</button><span className="field-tooltip" id={`${id}-help`} role="tooltip">{help}</span></span>;
}

function FormField({ id, label, help, language, children }: { id: string; label: string; help: string; language: Language; children: ReactNode }) {
  return <div className="form-field"><div className="field-heading"><label htmlFor={id}>{label}</label><FieldInfo id={id} label={label} help={help} language={language} /></div>{children}</div>;
}

async function api(path: string, body?: object, method: "POST" | "PUT" = "POST"): Promise<any> {
  const response = await fetch(`/api/backend/${path}`, {
    method: body === undefined ? "GET" : method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
  });
  return parseOverviewResponse(path, response);
}

function decimal(value: unknown, digits = 2) {
  if (value === null || value === undefined || value === "") return "—";
  const numeric = Number(value);
  return Number.isFinite(numeric) ? new Intl.NumberFormat("en-US", { maximumFractionDigits: digits }).format(numeric) : String(value);
}
function paperAmount(value: unknown) {
  if (typeof value === "string" && /^-?0(?:\.0*)?E[+-]?\d+$/.test(value)) return "0";
  if (typeof value !== "string" || !/^-?\d+(?:\.\d+)?$/.test(value)) return "—";
  const [integer, fraction = ""] = value.replace(/^-/, "").split(".");
  const grouped = integer.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const trimmed = fraction.replace(/0+$/, "");
  return `${paperSign(value) === "negative" ? "−" : paperSign(value) === "positive" ? "+" : ""}${grouped}${trimmed ? `.${trimmed}` : ""}`;
}
function paperSign(value: unknown): "positive" | "negative" | "flat" {
  if (typeof value !== "string") return "flat";
  if (/^-0*(?:\.0*)?$/.test(value)) return "flat";
  return value.startsWith("-") ? "negative" : "positive";
}
function fillSummary(row: Row, language: Language) {
  const p = row.provenance ?? {};
  if (p.model !== "estimated_fill") return null;
  const label = language === "uk" ? "Оцінене виконання" : "Estimated fill";
  const fee = language === "uk" ? "Комісія" : "Fee";
  const limit = language === "uk" ? "Limit: перетин REST книги без черги" : "Limit: crossing REST book without queue";
  const rules = language === "uk" ? "Правила" : "Rules";
  const depth = language === "uk" ? "REST книга" : "REST book";
  return `${label} · ${fee}: ${p.fee_source ?? "—"} @ ${p.fee_rate ?? "—"} · ${rules}: ${String(p.rules_version ?? "—").slice(0, 10)} · ${depth}: ${p.depth_version ?? "—"}${p.limit_model ? ` · ${limit}` : ""}`;
}
function utc(value: unknown) {
  if (!value) return "—";
  const date = new Date(String(value));
  return Number.isNaN(date.getTime()) ? String(value) : date.toISOString().replace("T", " ").slice(0, 19) + " UTC";
}
function sumStrings(values: string[]) {
  const units = values.reduce((sum, value) => {
    const [whole, fraction = ""] = value.split(".");
    const sign = whole.startsWith("-") ? BigInt(-1) : BigInt(1);
    const abs = whole.replace("-", "");
    return sum + sign * (BigInt(abs || "0") * BigInt("1000000000000") + BigInt(fraction.padEnd(12, "0").slice(0, 12)));
  }, BigInt(0));
  const sign = units < BigInt(0) ? "-" : "";
  const abs = units < BigInt(0) ? -units : units;
  return `${sign}${abs / BigInt("1000000000000")}.${String(abs % BigInt("1000000000000")).padStart(12, "0")}`;
}

const riskFields = [
  ["max_leverage", "Max leverage", "1", "3"], ["risk_per_trade", "Risk/trade", "0.000001", "0.005"],
  ["max_total_risk", "Total risk", "0.000001", "0.015"], ["max_daily_loss", "Daily loss", "0.000001", "0.03"],
  ["max_drawdown", "Drawdown", "0.000001", "0.10"], ["max_positions", "Max positions", "1", "3"],
  ["max_notional_equity", "Notional/equity", "0.000001", "3"], ["slippage_bps", "Slippage bps", "2", "100"],
  ["depth_participation", "Depth participation", "0.000001", "0.10"],
];
const strategyFields = [
  ["quantity", "Quantity · contracts", "0.000001", ""], ["leverage", "Leverage", "1", "3"],
  ["stop_distance", "Stop distance · fraction", "0.000001", "0.499999"],
  ["fast_period", "Fast period · candles", "2", "200"], ["slow_period", "Slow period · candles", "3", "400"],
  ["breakout_period", "Breakout period · candles", "2", "200"], ["breakout_buffer", "Breakout buffer · fraction", "0", "0.1"],
  ["min_volume_ratio", "Min volume ratio", "0", "10"],
];

function CandleChart({ candles, noCandles }: { candles: Row[]; noCandles: string }) {
  const final = candles.filter((c) => c.final && c.source === "rest" && [c.open, c.high, c.low, c.close].every((value) => value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value)))).slice(-48);
  if (!final.length) return <div className="empty chart-empty">{noCandles}</div>;
  const highs = final.map((c) => Number(c.high));
  const lows = final.map((c) => Number(c.low));
  const volume = (c: Row) => c.vol === undefined || c.vol === null || c.vol === "" || !Number.isFinite(Number(c.vol)) ? null : Number(c.vol);
  const volumes = final.map(volume).filter((value): value is number => value !== null);
  const hi = Math.max(...highs), lo = Math.min(...lows), maxVolume = Math.max(...volumes, 1);
  const span = hi - lo || 1, width = 960, height = 310, plot = 225, step = width / final.length;
  const y = (value: number) => 20 + (hi - value) / span * (plot - 30);
  return <div className="chart-scroll"><svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Final MEXC OHLCV candles" preserveAspectRatio="none">
    {[0, 1, 2, 3].map((n) => <g key={n}><line x1="0" x2={width} y1={20 + n * 64} y2={20 + n * 64} className="gridline" /><text x="6" y={16 + n * 64} className="axis">{decimal(hi - span * n / 3, 0)}</text></g>)}
    {final.map((c, index) => {
      const x = index * step + step / 2, open = Number(c.open), close = Number(c.close);
      const up = close >= open, color = up ? "#6ee7b7" : "#ff8b84";
      const observedVolume = volume(c);
      return <g key={c.event_id ?? index}><line x1={x} x2={x} y1={y(Number(c.high))} y2={y(Number(c.low))} stroke={color} strokeWidth="1.5" />
        <rect x={x - Math.max(2, step * .28)} y={Math.min(y(open), y(close))} width={Math.max(4, step * .56)} height={Math.max(2, Math.abs(y(open) - y(close)))} fill={color} />
        {observedVolume !== null && <rect x={x - Math.max(2, step * .28)} y={300 - Math.max(2, observedVolume / maxVolume * 60)} width={Math.max(4, step * .56)} height={Math.max(2, observedVolume / maxVolume * 60)} fill={color} opacity=".35" />}
        <title>{`${utc(c.window_start)} · O ${c.open} H ${c.high} L ${c.low} C ${c.close} V ${observedVolume === null ? "—" : c.vol}`}</title></g>;
    })}
    <line x1="0" x2={width} y1="235" y2="235" className="gridline" />
  </svg><div className="chart-ticks"><span>{utc(final[0].window_start)}</span><span>{utc(final[final.length - 1].window_start)}</span></div></div>;
}

function BotWizard({ t, language, defaults, onCreate, onCancel }: { t: typeof words.uk; language: Language; defaults: Row; onCreate: (body: Row) => Promise<void>; onCancel: () => void }) {
  const help = fieldHelp[language];
  const [creationMode, setCreationMode] = useState<"simple" | "advanced">("simple");
  const [step, setStep] = useState(1);
  const [name, setName] = useState("");
  const [deposit, setDeposit] = useState("1000");
  const [maxPositionNotional, setMaxPositionNotional] = useState("");
  const [simpleLeverage, setSimpleLeverage] = useState("");
  const [simpleStopLoss, setSimpleStopLoss] = useState("");
  const [simpleTakeProfit, setSimpleTakeProfit] = useState("");
  const [strategy, setStrategy] = useState("");
  const [timeframe, setTimeframe] = useState("1m");
  const [config, setConfig] = useState<Record<string, string>>({});
  const [risk, setRisk] = useState<Record<string, string>>(Object.fromEntries(riskFields.map(([key]) => [key, String(defaults[key] ?? "")])));
  const [accepted, setAccepted] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const validPct = (value: string) => value !== "" && Number.isFinite(Number(value)) && Number(value) > 0 && Number(value) < 100;
  const baseComplete = name.trim() && Number.isFinite(Number(deposit)) && Number(deposit) > 0;
  const complete = creationMode === "simple"
    ? baseComplete && Number.isFinite(Number(maxPositionNotional)) && Number(maxPositionNotional) > 0 && simpleLeverage !== "" && Number.isFinite(Number(simpleLeverage)) && Number(simpleLeverage) >= 1 && Number(simpleLeverage) <= 3 && validPct(simpleStopLoss) && validPct(simpleTakeProfit)
    : baseComplete && strategy && strategyFields.every(([key]) => config[key] !== undefined && config[key] !== "") && riskFields.every(([key]) => risk[key] !== "") && Number(config.fast_period) < Number(config.slow_period);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!complete || !accepted) return;
    setBusy(true); setError("");
    try {
      if (creationMode === "simple") {
        await onCreate({ name: name.trim(), deposit, config: { kind: "learned_paper", max_position_notional_usdt: maxPositionNotional, leverage: simpleLeverage, stop_loss_pct: simpleStopLoss, take_profit_pct: simpleTakeProfit } });
      } else {
        await onCreate({ name: name.trim(), deposit, risk_profile: Object.fromEntries(riskFields.map(([key]) => [key, key === "max_positions" ? Number(risk[key]) : risk[key]])),
          config: { strategy, timeframe, ...config, fast_period: Number(config.fast_period), slow_period: Number(config.slow_period), breakout_period: Number(config.breakout_period) } });
      }
    } catch (reason) { setError(reason instanceof Error ? reason.message : t.error); }
    finally { setBusy(false); }
  };
  const modeSwitch = <div className="bot-mode-switch" role="group" aria-label={t.mode}>
    <button type="button" className={creationMode === "simple" ? "selected" : ""} aria-pressed={creationMode === "simple"} onClick={() => { setCreationMode("simple"); setAccepted(false); }}>{t.simpleMode}</button>
    <button type="button" className={creationMode === "advanced" ? "selected" : ""} aria-pressed={creationMode === "advanced"} onClick={() => { setCreationMode("advanced"); setAccepted(false); }}>{t.advancedMode}</button>
  </div>;
  if (creationMode === "simple") return <div className="modal-backdrop"><form className="wizard card" onSubmit={submit}>
    <div className="modal-head"><div><span className="eyebrow">PAPER / BTC_USDT</span><h2>{t.modelDraftTitle}</h2></div><button type="button" className="icon-button" onClick={onCancel} aria-label={t.cancel}>×</button></div>
    {modeSwitch}
    <p className="banner warning model-draft-note" role="status">{t.modelDraftNote}</p>
    <div className="form-grid">
      <FormField id="paper-name" label={t.name} help={help.name} language={language}><input id="paper-name" aria-describedby="paper-name-help" required maxLength={100} value={name} onChange={(e) => setName(e.target.value)} /></FormField>
      <FormField id="paper-deposit" label={t.deposit} help={help.deposit} language={language}><input id="paper-deposit" aria-describedby="paper-deposit-help" required type="number" min="0.01" step="any" value={deposit} onChange={(e) => setDeposit(e.target.value)} /></FormField>
      <FormField id="paper-max-position" label={t.maxPositionNotional} help={help.maxPositionNotional} language={language}><input id="paper-max-position" aria-describedby="paper-max-position-help" required type="number" min="0.000001" step="any" value={maxPositionNotional} onChange={(e) => setMaxPositionNotional(e.target.value)} /></FormField>
      <FormField id="paper-simple-leverage" label={t.simpleLeverage} help={help.simpleLeverage} language={language}><input id="paper-simple-leverage" aria-describedby="paper-simple-leverage-help" required type="number" min="1" max="3" step="any" value={simpleLeverage} onChange={(e) => setSimpleLeverage(e.target.value)} /></FormField>
      <FormField id="paper-simple-sl" label={t.simpleStopLoss} help={help.simpleStopLoss} language={language}><input id="paper-simple-sl" aria-describedby="paper-simple-sl-help" required type="number" min="0.000001" max="99.999999" step="any" value={simpleStopLoss} onChange={(e) => setSimpleStopLoss(e.target.value)} /></FormField>
      <FormField id="paper-simple-tp" label={t.simpleTakeProfit} help={help.simpleTakeProfit} language={language}><input id="paper-simple-tp" aria-describedby="paper-simple-tp-help" required type="number" min="0.000001" max="99.999999" step="any" value={simpleTakeProfit} onChange={(e) => setSimpleTakeProfit(e.target.value)} /></FormField>
    </div>
    <p className="note simple-limit-note">{t.simpleLimitNote}</p>
    <div className="check"><input id="paper-simple-review" type="checkbox" aria-describedby="paper-simple-review-help" checked={accepted} onChange={(e) => setAccepted(e.target.checked)} /><div className="field-heading"><label htmlFor="paper-simple-review">{t.simpleReview}</label><FieldInfo id="paper-simple-review" label={t.simpleReview} help={help.simpleReview} language={language} /></div></div>
    {error && <p className="error-text" role="alert">{error}</p>}
    <div className="form-actions"><button type="button" className="secondary" onClick={onCancel}>{t.cancel}</button><button type="submit" disabled={!complete || !accepted || busy}>{busy ? t.loading : t.createDraft}</button></div>
  </form></div>;
  return <div className="modal-backdrop"><form className="wizard card" onSubmit={submit}>
    <div className="modal-head"><div><span className="eyebrow">PAPER / BTC_USDT</span><h2>{t.createBot}</h2></div><button type="button" className="icon-button" onClick={onCancel} aria-label={t.cancel}>×</button></div>
    {modeSwitch}
    <div className="steps"><button type="button" className={step === 1 ? "current" : ""} onClick={() => setStep(1)}>01 · {t.strategy}</button><button type="button" className={step === 2 ? "current" : ""} onClick={() => setStep(2)}>02 · {t.risk}</button><button type="button" className={step === 3 ? "current" : ""} onClick={() => setStep(3)}>03 · {t.review}</button></div>
    {step === 1 && <div className="form-grid"><p className="note full">{t.allFields}</p>
      <FormField id="paper-name" label={t.name} help={help.name} language={language}><input id="paper-name" aria-describedby="paper-name-help" required maxLength={100} value={name} onChange={(e) => setName(e.target.value)} /></FormField>
      <FormField id="paper-deposit" label={t.deposit} help={help.deposit} language={language}><input id="paper-deposit" aria-describedby="paper-deposit-help" required type="number" min="0.01" step="any" value={deposit} onChange={(e) => setDeposit(e.target.value)} /></FormField>
      <FormField id="paper-strategy" label={t.strategy} help={help.strategy} language={language}><select id="paper-strategy" aria-describedby="paper-strategy-help" required value={strategy} onChange={(e) => setStrategy(e.target.value)}><option value="">—</option><option value="trend_following">Trend Following</option><option value="pullback">Pullback</option><option value="breakout">Breakout</option></select></FormField>
      <FormField id="paper-timeframe" label={t.timeframe} help={help.timeframe} language={language}><select id="paper-timeframe" aria-describedby="paper-timeframe-help" value={timeframe} onChange={(e) => setTimeframe(e.target.value)}><option>1m</option><option>5m</option><option>15m</option></select></FormField>
      {strategyFields.map(([key, label, min, max]) => <FormField key={key} id={`paper-${key}`} label={label} help={help[key]} language={language}><input id={`paper-${key}`} aria-describedby={`paper-${key}-help`} required type="number" inputMode="decimal" step={key.includes("period") ? "1" : "any"} min={min} max={max || undefined} value={config[key] ?? ""} onChange={(e) => setConfig({ ...config, [key]: e.target.value })} /></FormField>)}
    </div>}
    {step === 2 && <div className="form-grid"><p className="note full">{t.allFields}</p>{riskFields.map(([key, label, min, max]) => <FormField key={key} id={`paper-risk-${key}`} label={label} help={help[key]} language={language}><input id={`paper-risk-${key}`} aria-describedby={`paper-risk-${key}-help`} required type="number" inputMode="decimal" step={key === "max_positions" ? "1" : "any"} min={min} max={max} value={risk[key]} onChange={(e) => setRisk({ ...risk, [key]: e.target.value })} /></FormField>)}</div>}
    {step === 3 && <div className="review-block"><p><strong>{name || "—"}</strong> · BTC_USDT · {strategy || "—"} · {timeframe}</p><p>{t.deposit}: {deposit} USDT · {t.config}: v1</p><p>{t.assumption}</p><p>{t.noProfitClaim}</p><p>{t.noAi}</p><div className="check"><input id="paper-review" type="checkbox" aria-describedby="paper-review-help" checked={accepted} onChange={(e) => setAccepted(e.target.checked)} /><div className="field-heading"><label htmlFor="paper-review">{t.reviewCheck}</label><FieldInfo id="paper-review" label={t.reviewCheck} help={help.reviewCheck} language={language} /></div></div></div>}
    {error && <p className="error-text" role="alert">{error}</p>}
    <div className="form-actions"><button type="button" className="secondary" onClick={onCancel}>{t.cancel}</button>{step < 3 ? <button type="button" onClick={() => setStep(step + 1)}>→</button> : <button type="submit" disabled={!complete || !accepted || busy}>{busy ? t.loading : t.create}</button>}</div>
  </form></div>;
}

function PilotSettingsDialog({ t, language, botId, initial, onCancel }: { t: typeof words.uk; language: Language; botId: string; initial: Row; onCancel: () => void }) {
  const help = fieldHelp[language];
  const [saved, setSaved] = useState<Row>(initial);
  const draft = initial.draft ?? {};
  const [maxPairs, setMaxPairs] = useState(String(draft.max_pairs ?? ""));
  const [maxLeverage, setMaxLeverage] = useState(String(draft.max_leverage ?? ""));
  const [takeProfit, setTakeProfit] = useState(String(draft.take_profit_pct ?? ""));
  const [stopLoss, setStopLoss] = useState(String(draft.stop_loss_pct ?? ""));
  const [maxRoundTripCost, setMaxRoundTripCost] = useState(String(draft.max_round_trip_cost_pct ?? ""));
  const [limits, setLimits] = useState<Row>({
    per_pair_notional: { unit: draft.per_pair_notional?.unit ?? "USDT", value: String(draft.per_pair_notional?.value ?? "") },
    aggregate_notional: { unit: draft.aggregate_notional?.unit ?? "USDT", value: String(draft.aggregate_notional?.value ?? "") },
    max_daily_loss: { unit: draft.max_daily_loss?.unit ?? "USDT", value: String(draft.max_daily_loss?.value ?? "") },
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const limitField = (key: string, label: string, helpKey: string) => <div className="pilot-limit" key={key}>
    <div className="field-heading pilot-limit-heading"><strong>{label}</strong><FieldInfo id={`pilot-${key}`} label={label} help={help[helpKey]} language={language} /></div>
    <FormField id={`pilot-${key}-value`} label={t.limitValue} help={`${help[helpKey]} ${help.limitValue}`} language={language}><input id={`pilot-${key}-value`} aria-describedby={`pilot-${key}-value-help`} required type="number" inputMode="decimal" min="0" step="any" value={limits[key].value} onChange={(e) => setLimits({ ...limits, [key]: { ...limits[key], value: e.target.value } })} /></FormField>
    <FormField id={`pilot-${key}-unit`} label={t.limitUnit} help={help.limitUnit} language={language}><select id={`pilot-${key}-unit`} aria-describedby={`pilot-${key}-unit-help`} value={limits[key].unit} onChange={(e) => setLimits({ ...limits, [key]: { ...limits[key], unit: e.target.value } })}><option value="USDT">USDT</option><option value="PCT_EQUITY">{t.pctEquity}</option></select></FormField>
  </div>;
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true); setError("");
    try {
      const result = await api(`bots/${botId}/live-pilot-settings`, {
        max_pairs: Number(maxPairs), per_pair_notional: limits.per_pair_notional,
        aggregate_notional: limits.aggregate_notional, max_leverage: maxLeverage,
        take_profit_pct: takeProfit, stop_loss_pct: stopLoss, max_daily_loss: limits.max_daily_loss,
        max_round_trip_cost_pct: maxRoundTripCost,
      }, "PUT");
      setSaved(result);
    } catch (reason) { setError(reason instanceof Error ? reason.message : t.pilotSaveError); }
    finally { setBusy(false); }
  };
  return <div className="modal-backdrop"><form className="wizard card" onSubmit={submit}>
    <div className="modal-head"><div><span className="eyebrow">LIVE PILOT · DRAFT ONLY</span><h2>{t.pilotSettings}</h2></div><button type="button" className="icon-button" onClick={onCancel} aria-label={t.cancel}>×</button></div>
    <p className="note pilot-note">{t.pilotDisabled}</p>
    <p className="note pilot-note">{saved.version ? `${t.pilotVersion}: v${saved.version} · ${utc(saved.created_at)}` : t.pilotMissing}</p>
    <div className="form-grid">
      <FormField id="pilot-max-pairs" label={t.maxPairs} help={help.maxPairs} language={language}><input id="pilot-max-pairs" aria-describedby="pilot-max-pairs-help" required type="number" min="1" max="3" step="1" value={maxPairs} onChange={(e) => setMaxPairs(e.target.value)} /></FormField>
      <FormField id="pilot-max-leverage" label={t.maxLeverage} help={help.maxLeverage} language={language}><input id="pilot-max-leverage" aria-describedby="pilot-max-leverage-help" required type="number" min="1" step="any" value={maxLeverage} onChange={(e) => setMaxLeverage(e.target.value)} /></FormField>
      {limitField("per_pair_notional", t.perPairNotional, "perPairNotional")}
      {limitField("aggregate_notional", t.aggregateNotional, "aggregateNotional")}
      <FormField id="pilot-take-profit" label={t.takeProfitPct} help={help.takeProfitPct} language={language}><input id="pilot-take-profit" aria-describedby="pilot-take-profit-help" required type="number" min="0" max="100" step="any" value={takeProfit} onChange={(e) => setTakeProfit(e.target.value)} /></FormField>
      <FormField id="pilot-stop-loss" label={t.stopLossPct} help={help.stopLossPct} language={language}><input id="pilot-stop-loss" aria-describedby="pilot-stop-loss-help" required type="number" min="0" max="100" step="any" value={stopLoss} onChange={(e) => setStopLoss(e.target.value)} /></FormField>
      <FormField id="pilot-round-trip-cost" label={t.maxRoundTripCostPct} help={help.maxRoundTripCostPct} language={language}><input id="pilot-round-trip-cost" aria-describedby="pilot-round-trip-cost-help" required type="number" min="0" max="100" step="any" value={maxRoundTripCost} onChange={(e) => setMaxRoundTripCost(e.target.value)} /></FormField>
      {limitField("max_daily_loss", t.maxDailyLoss, "maxDailyLoss")}
    </div>
    {error && <p className="error-text" role="alert">{error}</p>}
    <div className="form-actions"><button type="button" className="secondary" onClick={onCancel}>{t.cancel}</button><button type="submit" disabled={busy}>{busy ? t.loading : t.pilotSave}</button></div>
  </form></div>;
}

function CredentialDialog({ t, credential, onDone, onCancel }: { t: typeof words.uk; credential: Row | null; onDone: () => Promise<void>; onCancel: () => void }) {
  const [label, setLabel] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const dismiss = () => { setApiKey(""); setApiSecret(""); onCancel(); };
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true); setError("");
    try {
      if (credential) {
        await api(`exchange-credentials/${credential.id}/rotate`, { api_key: apiKey, api_secret: apiSecret, expected_version: credential.version }, "PUT");
      } else {
        await api("exchange-credentials", { exchange: "mexc_futures", label: label.trim(), api_key: apiKey, api_secret: apiSecret });
      }
      setApiKey(""); setApiSecret("");
      await onDone();
    } catch (reason) { setError(reason instanceof Error ? reason.message : t.connectionError); }
    finally { setBusy(false); }
  };
  return <div className="modal-backdrop"><form className="card simple-modal credential-modal" onSubmit={submit} autoComplete="off">
    <div className="modal-head"><div><span className="eyebrow">MEXC FUTURES · LIVE DISABLED</span><h2>{credential ? t.rotateConnection : t.addConnection}</h2></div><button type="button" className="icon-button" onClick={dismiss} aria-label={t.cancel}>×</button></div>
    <p className="note">{t.connectionsNote}</p>
    {credential ? <p className="note">{credential.label} · {credential.api_key_masked} · v{credential.version}</p> : <><label htmlFor="credential-exchange">{t.exchange}<select id="credential-exchange" value="mexc_futures" disabled><option value="mexc_futures">MEXC Futures</option></select></label><label htmlFor="credential-label">{t.connectionLabel}<input id="credential-label" required maxLength={100} value={label} onChange={(event) => setLabel(event.target.value)} /></label></>}
    <label htmlFor="credential-api-key">{t.apiKey}<input id="credential-api-key" required minLength={8} type="password" autoComplete="off" spellCheck={false} value={apiKey} onChange={(event) => setApiKey(event.target.value)} /></label>
    <label htmlFor="credential-api-secret">{t.apiSecret}<input id="credential-api-secret" required type="password" autoComplete="off" spellCheck={false} value={apiSecret} onChange={(event) => setApiSecret(event.target.value)} /></label>
    {error && <p className="error-text" role="alert">{error}</p>}
    <div className="form-actions"><button type="button" className="secondary" onClick={dismiss}>{t.cancel}</button><button type="submit" disabled={busy}>{busy ? t.loading : credential ? t.rotateConnection : t.addConnection}</button></div>
  </form></div>;
}

function BotCredentialDialog({ t, botId, initial, credentials, onDone, onCancel }: { t: typeof words.uk; botId: string; initial: Row; credentials: Row[]; onDone: (value: Row) => void; onCancel: () => void }) {
  const available = credentials.filter((item) => item.status === "active" && item.exchange === "mexc_futures");
  const [credentialId, setCredentialId] = useState(String(initial.credential_id ?? ""));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const submit = async (event: FormEvent) => {
    event.preventDefault(); if (!credentialId) return;
    setBusy(true); setError("");
    try {
      const value = await api(`bots/${botId}/live-credential`, { credential_id: credentialId, expected_version: initial.version ?? null }, "PUT");
      onDone(value);
    } catch (reason) { setError(reason instanceof Error ? reason.message : t.connectionError); }
    finally { setBusy(false); }
  };
  return <div className="modal-backdrop"><form className="card simple-modal credential-modal" onSubmit={submit}>
    <div className="modal-head"><div><span className="eyebrow">LIVE · DRAFT ONLY</span><h2>{t.selectConnection}</h2></div><button type="button" className="icon-button" onClick={onCancel} aria-label={t.cancel}>×</button></div>
    <p className="note">{t.connectionSelectionNote}</p>
    <p className="note">{initial.credential_id ? initial.selection_valid ? `${initial.credential?.label ?? "—"} · ${initial.credential?.api_key_masked ?? "—"} · v${initial.credential_version}` : t.selectionInvalid : t.selectionMissing}</p>
    <label htmlFor="bot-credential-select">{t.connections}<select id="bot-credential-select" required value={credentialId} onChange={(event) => setCredentialId(event.target.value)}><option value="">{t.selectionMissing}</option>{available.map((item) => <option value={item.id} key={item.id}>{item.label} · {item.api_key_masked} · v{item.version}</option>)}</select></label>
    {!available.length && <p className="note">{t.noConnections}</p>}
    {error && <p className="error-text" role="alert">{error}</p>}
    <div className="form-actions"><button type="button" className="secondary" onClick={onCancel}>{t.cancel}</button><button type="submit" disabled={busy || !credentialId || !available.some((item) => item.id === credentialId)}>{busy ? t.loading : t.saveSelection}</button></div>
  </form></div>;
}

export default function Console() {
  const [language, setLanguage] = useState<Language>("uk");
  const t = words[language];
  const [page, setPage] = useState<Page>("dashboard");
  const [load, setLoad] = useState<Load>("loading");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState("");
  const [health, setHealth] = useState<Row | null>(null);
  const [market, setMarket] = useState<Row[]>([]);
  const [ticker, setTicker] = useState<Row | null>(null);
  const [candles, setCandles] = useState<Row[]>([]);
  const [timeframe, setTimeframe] = useState("1m");
  const [portfolios, setPortfolios] = useState<Row[]>([]);
  const [bots, setBots] = useState<Row[]>([]);
  const [riskState, setRiskState] = useState<Row>({});
  const [selected, setSelected] = useState("");
  const [history, setHistory] = useState<Record<string, Row[]>>({});
  const [wizard, setWizard] = useState(false);
  const [pilotSettings, setPilotSettings] = useState<{ botId: string; value: Row } | null>(null);
  const [credentials, setCredentials] = useState<Row[]>([]);
  const [credentialError, setCredentialError] = useState("");
  const [credentialDialog, setCredentialDialog] = useState<Row | null | "create">(null);
  const [botCredentialDialog, setBotCredentialDialog] = useState<{ botId: string; value: Row } | null>(null);
  const [newPortfolio, setNewPortfolio] = useState(false);
  const [portfolioName, setPortfolioName] = useState("");
  const [portfolioDeposit, setPortfolioDeposit] = useState("1000");
  const [journalTab, setJournalTab] = useState("decisions");
  const [lastUpdated, setLastUpdated] = useState("");
  const [forecasts, setForecasts] = useState<Row[]>([]);
  const [forecast, setForecast] = useState<Row | null>(null);
  const [researchError, setResearchError] = useState("");

  const refreshCredentials = useCallback(async () => {
    try { setCredentials(await api("exchange-credentials")); setCredentialError(""); }
    catch (reason) { setCredentialError(reason instanceof Error ? reason.message : "CREDENTIAL_VAULT_UNAVAILABLE"); }
  }, []);

  const refreshResearch = useCallback(async () => {
    try {
      const [list, latest] = await Promise.all([api("research/forecasts"), api("research/forecasts/latest")]);
      setForecasts(list.items ?? []);
      setForecast(latest.forecast ?? null);
      setResearchError("");
    } catch (reason) {
      setResearchError(reason instanceof Error ? reason.message : "Research unavailable");
    }
  }, []);

  const refresh = useCallback(async () => {
    const names = ["health", "markets", "markets/BTC_USDT/ticker", `markets/BTC_USDT/candles?timeframe=${timeframe}`, "portfolios", "bots", "risk"];
    const results = await Promise.allSettled(names.map((name) => api(name)));
    if (results[0].status === "fulfilled") setHealth(results[0].value);
    if (results[1].status === "fulfilled") setMarket(results[1].value);
    if (results[2].status === "fulfilled") setTicker(results[2].value);
    if (results[3].status === "fulfilled") setCandles(results[3].value.candles ?? []);
    if (results[4].status === "fulfilled") setPortfolios(results[4].value);
    if (results[5].status === "fulfilled") setBots(results[5].value);
    if (results[6].status === "fulfilled") setRiskState(results[6].value);
    const critical = results.slice(0, 3).some((r) => r.status === "rejected") || results.slice(4).some((r) => r.status === "rejected");
    setLoad(critical ? "error" : "ready");
    setLastUpdated(new Date().toISOString());
  }, [timeframe]);

  useEffect(() => { const saved = localStorage.getItem("af-language"); if (saved === "en") setLanguage("en"); const portfolio = localStorage.getItem("af-portfolio"); if (portfolio) setSelected(portfolio); }, []);
  useEffect(() => { refresh(); const timer = setInterval(refresh, 8000); return () => clearInterval(timer); }, [refresh]);
  useEffect(() => { if (page === "research") refreshResearch(); }, [page, refreshResearch]);
  useEffect(() => { if (page === "settings" || page === "bots") void refreshCredentials(); }, [page, refreshCredentials]);
  useEffect(() => { if (!portfolios.length) return; if (!selected || !portfolios.some((p) => p.id === selected)) setSelected(portfolios[0].id); }, [portfolios, selected]);
  useEffect(() => { if (selected && portfolios.some((p) => p.id === selected)) localStorage.setItem("af-portfolio", selected); }, [selected, portfolios]);
  useEffect(() => {
    if (!selected) { setHistory({}); return; }
    let active = true;
    const get = async () => {
      const names = ["positions", "orders", "decisions", "risk", "fills", "ledger"];
      const linkedBot = bots.find((bot) => bot.portfolio_id === selected);
      const urls = names.map((name) => `portfolios/${selected}/${name}`);
      if (linkedBot) urls.push(`bots/${linkedBot.id}/decisions`);
      const data = await Promise.allSettled(urls.map((url) => api(url)));
      if (active) setHistory({ ...Object.fromEntries(names.map((name, i) => [name, data[i].status === "fulfilled" ? data[i].value : []])), bot: linkedBot && data[6]?.status === "fulfilled" ? data[6].value : [] });
    };
    get(); const timer = setInterval(get, 8000);
    return () => { active = false; clearInterval(timer); };
  }, [selected, lastUpdated, bots]);

  const portfolio = portfolios.find((p) => p.id === selected);
  const quality = ticker?.quality ?? market[0]?.quality;
  const eligible = overviewTradeEligible(load, health, quality, ticker);
  const totalEquity = portfolios.length && portfolios.every((p) => p.equity !== null) ? sumStrings(portfolios.map((p) => p.equity)) : null;
  const activeBots = bots.filter((b) => b.status === "RUNNING" || b.status === "RISK_HALTED");
  const paperBots = bots.filter((b) => b.mode === "paper");
  const botDecisions = useMemo(() => history.bot ?? [], [history]);
  const choosePortfolio = (id: string) => { setSelected(id); localStorage.setItem("af-portfolio", id); };

  const command = async (key: string, path: string, body: Row = {}) => {
    setBusy(key); setMessage("");
    try { await api(path, body); await refresh(); setMessage("✓"); }
    catch (reason) { setMessage(reason instanceof Error ? reason.message : t.error); }
    finally { setBusy(""); }
  };
  const createBot = async (body: Row) => { const created = await api("bots", body); choosePortfolio(created.portfolio_id); setWizard(false); await refresh(); setPage("bots"); };
  const openPilotSettings = async (botId: string) => {
    setBusy(botId); setMessage("");
    try { setPilotSettings({ botId, value: await api(`bots/${botId}/live-pilot-settings`) }); }
    catch (reason) { setMessage(reason instanceof Error ? reason.message : t.error); }
    finally { setBusy(""); }
  };
  const openBotCredential = async (botId: string) => {
    setBusy(botId); setMessage("");
    try {
      const [items, value] = await Promise.all([api("exchange-credentials"), api(`bots/${botId}/live-credential`)]);
      setCredentials(items); setCredentialError(""); setBotCredentialDialog({ botId, value });
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : t.error); }
    finally { setBusy(""); }
  };
  const revokeCredential = async (item: Row) => {
    if (!confirm(t.revokeConfirm)) return;
    setBusy(item.id); setCredentialError("");
    try { await api(`exchange-credentials/${item.id}/revoke`, {}); await refreshCredentials(); }
    catch (reason) { setCredentialError(reason instanceof Error ? reason.message : t.error); }
    finally { setBusy(""); }
  };
  const createPortfolio = async (event: FormEvent) => {
    event.preventDefault(); setBusy("portfolio"); setMessage("");
    try { const created = await api("portfolios", { name: portfolioName.trim() || "Paper", deposit: portfolioDeposit, risk_profile: riskState.defaults }); setNewPortfolio(false); choosePortfolio(created.id); await refresh(); }
    catch (reason) { setMessage(reason instanceof Error ? reason.message : t.error); }
    finally { setBusy(""); }
  };
  const close = async (position: Row) => {
    if (!confirm(t.confirmClose)) return;
    await command(`close-${position.id}`, `portfolios/${position.portfolio_id}/positions/${position.id}/close`, { idempotency_key: crypto.randomUUID(), reason: "manual" });
  };
  const toggleKill = async () => {
    if (!confirm(riskState.kill_switch ? t.confirmUnkill : t.confirmKill)) return;
    await command("kill", "risk/kill-switch", { kill_switch: !riskState.kill_switch });
  };
  const nav: Page[] = ["dashboard", "bots", "positions", "journal", "research", "settings"];

  return <div className="shell">
    <aside className="sidebar"><div className="brand"><div className="brand-symbol">◈</div><div><strong>AI FUTURES</strong><small>PAPER CONSOLE</small></div></div><nav aria-label="Main navigation">{nav.map((item) => <button key={item} className={page === item ? "nav active" : "nav"} onClick={() => setPage(item)}><span className="nav-glyph">{({ dashboard: "◫", bots: "⌘", positions: "◎", journal: "≡", research: "◇", settings: "⚙" })[item]}</span>{t[item]}</button>)}</nav><div className="sidebar-bottom"><span className="paper-pill">● {t.paper}</span><p>{t.assumption}</p></div></aside>
    <main className="main"><header className="topbar"><div><div className="eyebrow">AI FUTURES / {t.paper}</div><h1>{t[page]}</h1></div><div className="top-actions"><span className={`quality-pill ${eligible ? "good" : "bad"}`}><i />{eligible ? t.active : t.degraded}</span><button className="secondary" onClick={() => { refresh(); if (page === "research") refreshResearch(); }}>{t.refresh}</button><select aria-label="Language" className="language" value={language} onChange={(event) => { const value = event.target.value as Language; setLanguage(value); localStorage.setItem("af-language", value); document.documentElement.lang = value; }}><option value="uk">UA</option><option value="en">EN</option></select></div></header>
      {load === "loading" && <div className="loading" role="status">{t.loading}</div>}
      {load === "error" && <div className="banner error" role="alert">{t.error} <button onClick={refresh}>{t.retry}</button></div>}
      {!eligible && load !== "loading" && page !== "research" && <div className="banner warning"><strong>{t.degraded}</strong> · {overviewDegradedReason(health, quality, ticker)}. {t.updated}: {utc(ticker?.source_time)}. {t.start}: {t.notAvailable}.</div>}
      {message && <div className={`banner ${message === "✓" ? "success" : "error"}`} role="status">{message}</div>}

      {page === "dashboard" && <div className="page-content"><section className="hero card"><div><span className="eyebrow">BTC_USDT / MEXC PUBLIC</span><div className="hero-price">{ticker?.data?.fairPrice ? decimal(ticker.data.fairPrice, 2) : "—"}<span>USDT</span></div><p>{t.price} · fairPrice · {t.source}: {ticker?.source ?? "—"} · {utc(ticker?.source_time)}</p></div><div className="hero-meta"><span>{t.data}</span><strong className={eligible ? "positive" : "negative"}>{eligible ? t.active : t.degraded}</strong><small>{quality?.transport ?? "—"} · {t.rule}: {market[0]?.rules_version?.slice(0, 10) ?? "—"}</small></div></section>
        <div className="stat-grid"><div className="stat card"><span>{t.equity} · PAPER</span><strong>{totalEquity === null ? "—" : decimal(totalEquity)} <small>USDT</small></strong><small>{totalEquity === null && portfolios.length ? "DATA_STALE / FUNDING_UNRESOLVED" : `${portfolios.length} ${t.portfolio.toLowerCase()}`}</small></div><div className="stat card"><span>{t.balance}</span><strong>{portfolio ? decimal(portfolio.available_balance) : "—"} <small>USDT</small></strong><small>{portfolio?.name ?? t.selectPortfolio}</small></div><div className="stat card"><span>{t.unrealized}</span><strong>{portfolio ? decimal(portfolio.unrealized_pnl) : "—"} <small>USDT</small></strong><small>{portfolio?.valuation_status ?? "—"}</small></div><div className="stat card"><span>{t.bots}</span><strong>{activeBots.length}</strong><small>{activeBots.map((b) => b.status).join(" · ") || "—"}</small></div></div>
        <section className="card bot-results"><div className="section-head"><div><span className="eyebrow">PAPER / USDT</span><h2>{t.botResults}</h2></div><small>{t.resultNote}</small></div>{load === "loading" ? <div className="empty">{t.loading}</div> : load === "error" ? <div className="empty" role="alert">{t.accountUnavailable} · {t.retry}</div> : paperBots.length ? <div className="bot-results-list">{paperBots.map((bot) => { const account = portfolios.find((p) => p.id === bot.portfolio_id); const total = account?.total_pnl_net; const sign = paperSign(total); return <article className="bot-result-row" key={bot.id}><div className="bot-result-identity"><strong>{account?.name ?? `${t.portfolio} ${bot.portfolio_id.slice(0, 8)}`}</strong><span className={`status ${bot.status === "RUNNING" ? "running" : ""}`}>{bot.status}</span><small>{t.currentEpoch}: {account?.epoch ?? "—"} · {t.deposit}: {decimal(account?.initial_deposit)} USDT</small></div><div className="bot-result-values"><div className="bot-result-total"><span>{t.netResult}</span><strong className={sign === "flat" ? "" : sign}>{total === null || total === undefined ? "—" : paperAmount(total)} <small>USDT</small></strong><small>{total === null || total === undefined ? `${t.valuationUnknown} · ${account?.valuation_status ?? t.accountUnavailable}` : sign === "positive" ? t.profit : sign === "negative" ? t.loss : t.flat}</small></div><div className="bot-result-parts"><span>{t.realizedNet}: <strong className={paperSign(account?.realized_pnl_net)}>{paperAmount(account?.realized_pnl_net)} USDT</strong></span><span>{t.unrealizedResult}: <strong>{paperAmount(account?.unrealized_pnl)} USDT</strong></span></div></div><div className="bot-result-actions"><button className="text-button" onClick={() => { choosePortfolio(bot.portfolio_id); setPage("bots"); setTimeout(() => document.getElementById(`bot-${bot.id}`)?.scrollIntoView({ block: "center", behavior: "smooth" }), 0); }}>{t.viewBot} →</button><button className="text-button" onClick={() => { choosePortfolio(bot.portfolio_id); setJournalTab("ledger"); setPage("journal"); }}>{t.viewJournal} →</button></div></article>; })}</div> : <div className="empty">{t.noBots}</div>}</section>
        <section className="card chart-card"><div className="section-head"><div><span className="eyebrow">MEXC / REST FINAL</span><h2>{t.finalCandles}</h2></div><select aria-label={t.timeframe} value={timeframe} onChange={(e) => setTimeframe(e.target.value)}><option>1m</option><option>5m</option><option>15m</option></select></div><CandleChart candles={candles} noCandles={t.noCandles} /></section>
        <div className="two-col"><section className="card"><div className="section-head"><h2>{t.portfolio}</h2><button className="text-button" onClick={() => setNewPortfolio(true)}>+ {t.createPortfolio}</button></div>{portfolios.length ? <div className="stack">{portfolios.map((p) => <button key={p.id} className={`portfolio-row ${p.id === selected ? "selected" : ""}`} onClick={() => choosePortfolio(p.id)}><span><strong>{p.name}</strong><small>{p.id.slice(0, 8)} · epoch {p.epoch}</small></span><span>{decimal(p.equity)} USDT</span></button>)}</div> : <div className="empty">{t.noPortfolio}</div>}</section><section className="card"><div className="section-head"><h2>{t.newest}</h2><button className="text-button" onClick={() => { setJournalTab("bot"); setPage("journal"); }}>→</button></div>{botDecisions.slice(0, 5).map((d) => <div className="feed-row" key={d.id}><span className="feed-dot" /><span><strong>{d.action} · {d.reason}</strong><small>{utc(d.occurred_at)}</small></span><span>{d.risk_reason ?? "—"}</span></div>)}{!botDecisions.length && <div className="empty">{t.noJournal}</div>}</section></div>
      </div>}

      {page === "bots" && <div className="page-content"><div className="section-head intro"><div><p className="eyebrow">PAPER / BTC_USDT</p><p>{t.noProfitClaim}</p></div><button onClick={() => setWizard(true)}>+ {t.createBot}</button></div><div className="bot-grid">{bots.map((bot) => <div className={`card bot-card ${selected === bot.portfolio_id ? "selected" : ""}`} id={`bot-${bot.id}`} key={bot.id}><div className="section-head"><span className="bot-mark">⌘</span><span className={`status ${bot.status === "RUNNING" ? "running" : ""}`}>{bot.status}</span></div><h2>{portfolios.find((p) => p.id === bot.portfolio_id)?.name ?? bot.id.slice(0, 8)}</h2><p>{bot.mode.toUpperCase()} · BTC_USDT · {t.portfolio}: {bot.portfolio_id.slice(0, 8)}</p>{bot.bot_kind === "learned_paper" && <><p className="model-draft-card-note">{t.modelDraftCard}</p><p>{t.maxPositionNotional}: {bot.config?.max_position_notional_usdt ?? "—"} · {t.simpleLeverage}: {bot.config?.leverage ? `${bot.config.leverage}×` : "—"} · {t.simpleStopLoss}: {bot.config?.stop_loss_pct ?? "—"} · {t.simpleTakeProfit}: {bot.config?.take_profit_pct ?? "—"}</p></>}<div className="bot-facts"><span>{t.config}: {bot.active_version ? `v${bot.active_version}` : "—"}</span><span>{t.updatedAt}: {utc(bot.updated_at)}</span></div><div className="bot-actions">{bot.bot_kind !== "learned_paper" && bot.status === "DRAFT" && <button className="secondary" disabled={!!busy} onClick={() => command(bot.id, `bots/${bot.id}/ready`)}>{t.ready}</button>}{bot.bot_kind !== "learned_paper" && ["READY", "PAUSED", "STOPPED", "RISK_HALTED"].includes(bot.status) && <button disabled={!eligible || riskState.kill_switch || !!busy} onClick={() => command(bot.id, `bots/${bot.id}/start`)}>{t.start}</button>}{bot.status === "RUNNING" && <button className="secondary" disabled={!!busy} onClick={() => command(bot.id, `bots/${bot.id}/pause`)}>{t.pause}</button>}{["RUNNING", "PAUSED", "ERROR", "RISK_HALTED"].includes(bot.status) && <button className="secondary" disabled={!!busy} onClick={() => command(bot.id, `bots/${bot.id}/stop`)}>{t.stop}</button>}<button className="text-button" onClick={() => { choosePortfolio(bot.portfolio_id); setPage("journal"); }}>{t.journal} →</button>{bot.bot_kind !== "learned_paper" && <button className="text-button" disabled={!!busy} onClick={() => openPilotSettings(bot.id)}>{t.pilotSettings}</button>}<button className="text-button" disabled={!!busy} onClick={() => openBotCredential(bot.id)}>{t.selectConnection}</button></div></div>)}{!bots.length && <div className="card empty bot-empty">{t.noBots}</div>}</div></div>}

      {page === "positions" && <div className="page-content"><div className="section-head intro"><div><p className="eyebrow">PAPER / {t.positionHistory}</p><p>{t.assumption}</p></div><PortfolioSelect t={t} portfolios={portfolios} selected={selected} onChange={choosePortfolio} /></div><section className="card table-card"><div className="section-head"><h2>{t.positions}</h2><span className="muted">{(history.positions ?? []).filter((p) => !p.closed_at).length} OPEN</span></div>{(history.positions ?? []).filter((p) => !p.closed_at).length ? <div className="table-wrap"><table><thead><tr><th>Symbol</th><th>Side / Qty</th><th>Entry</th><th>SL / TP</th><th>Margin</th><th>Fees / Funding</th><th>{t.actions}</th></tr></thead><tbody>{(history.positions ?? []).filter((p) => !p.closed_at).map((p) => <tr key={p.id}><td>{p.symbol}<small>{p.id.slice(0, 8)}</small></td><td>{p.direction} · {p.quantity}<small>{p.leverage}×</small></td><td>{decimal(p.average_entry)}</td><td>{decimal(p.stop_loss)} / {decimal(p.take_profit)}<small>{t.estimated}: liq {decimal(p.liquidation_estimate)}</small><small>{p.liquidation_provenance?.status ?? "unknown"} · {p.liquidation_provenance?.model ?? "—"} · tier {p.liquidation_provenance?.tier_level ?? "—"}</small><details className="model-provenance"><summary>{language === "uk" ? "Походження оцінки" : "Estimate provenance"}</summary><pre>{JSON.stringify(p.liquidation_provenance ?? {}, null, 2)}</pre></details></td><td>{decimal(p.margin)}</td><td>{decimal(p.fees, 8)} / {decimal(p.funding, 8)}</td><td><button className="secondary small" disabled={!!busy || !eligible} onClick={() => close(p)}>{t.close}</button></td></tr>)}</tbody></table></div> : <div className="empty">{t.noPositions}</div>}</section><section className="card"><div className="section-head"><h2>{t.positionHistory}</h2></div>{(history.positions ?? []).filter((p) => p.closed_at).map((p) => <div className="closed-row" key={p.id}><div><strong>{p.symbol} · {p.direction} · {p.quantity}</strong><small>{utc(p.closed_at)} · {p.id.slice(0, 8)}</small></div><div><span>Entry {decimal(p.average_entry)} · SL {decimal(p.stop_loss)} · TP {decimal(p.take_profit)}</span><small>Fees {decimal(p.fees, 8)} · Funding {decimal(p.funding, 8)}</small></div><strong>{decimal(p.realized_pnl)} USDT</strong></div>)}{!(history.positions ?? []).some((p) => p.closed_at) && <div className="empty">—</div>}</section></div>}

      {page === "journal" && <div className="page-content"><div className="section-head intro"><div><p className="eyebrow">{t.portfolio} / {t.utc}</p><p>{t.assumption}</p></div><PortfolioSelect t={t} portfolios={portfolios} selected={selected} onChange={choosePortfolio} /></div><section className="card journal-card"><div className="tabs">{["bot", "decisions", "risk", "orders", "fills", "ledger"].map((tab) => <button key={tab} className={journalTab === tab ? "selected" : ""} onClick={() => setJournalTab(tab)}>{({ bot: t.botDecision, decisions: t.decision, risk: t.verdict, orders: t.orders, fills: t.fills, ledger: t.ledger } as Row)[tab]}</button>)}</div>{(history[journalTab] ?? []).length ? <div className="journal-list">{(journalTab === "bot" ? [...(history[journalTab] ?? [])] : [...(history[journalTab] ?? [])].reverse()).map((row, i) => <article className="journal-item" key={row.id ?? i}><div className="journal-top"><strong>{row.reason ?? row.outcome ?? row.status ?? row.kind ?? row.action ?? "EVENT"}</strong><span>{utc(row.occurred_at ?? row.updated_at)}</span></div>{journalTab === "fills" && fillSummary(row, language) && <p className="note">{fillSummary(row, language)}</p>}<div className="journal-fields">{Object.entries(row).filter(([key, value]) => value !== null && value !== undefined && !["id", "occurred_at", "updated_at", "payload_hash", "counteraccount"].includes(key)).slice(0, 20).map(([key, value]) => <div key={key}><span>{key}</span><strong>{typeof value === "object" ? JSON.stringify(value) : String(value)}</strong></div>)}</div></article>)}</div> : <div className="empty">{t.noJournal}</div>}</section></div>}

      {page === "research" && <div className="page-content"><section className="card research-card"><span className="eyebrow">BTC_USDT / 4H / COLLECT-ONLY</span><h2>{t.researchStatus}</h2><p>{t.researchNote}</p><p className="note">{t.researchNoExecution}</p></section>{researchError && <div className="banner error" role="alert">{t.error}: {researchError}</div>}<div className="two-col"><section className="card research-list"><h2>{t.newest}</h2>{forecasts.map((row) => <button key={row.forecast_id} className="research-row" onClick={async () => { try { const detail = await api(`research/forecasts/${row.forecast_id}`); setForecast(detail.forecast); setResearchError(""); } catch (reason) { setResearchError(reason instanceof Error ? reason.message : t.error); } }}><strong>{row.direction} · {row.status === "wait" ? "WAIT" : row.reason}</strong><small>{utc(row.anchor)} · {row.quality}{row.test_fixture ? ` · ${t.researchTest}` : ""}</small></button>)}{!forecasts.length && <div className="empty">{t.researchEmpty}</div>}</section><section className="card research-detail"><h2>{forecast ? `${forecast.direction} · ${forecast.reason}` : t.researchEmpty}</h2>{forecast && <><p className="note">{forecast.test_fixture ? t.researchTest : t.researchStatus} · {t.utc} · {utc(forecast.anchor)} · 4h</p><p>{t.researchScores}: {forecast.probabilities ? `DOWN ${forecast.probabilities.DOWN} · NEUTRAL ${forecast.probabilities.NEUTRAL} · UP ${forecast.probabilities.UP}` : "WAIT / —"}</p>{forecast.proposal && forecast.test_fixture && <p className="research-test">{t.researchTest} · {forecast.proposal.direction} · entry {forecast.proposal.entry} · SL {forecast.proposal.stop_loss} · TP {forecast.proposal.take_profit}</p>}<h3>{t.researchProvenance}</h3><dl className="research-provenance">{[["forecast ID", forecast.forecast_id], ["correlation ID", forecast.correlation_id], ["model", forecast.model_version], ["dataset checksum", forecast.dataset_checksum], ["config checksum", forecast.config_checksum], ["source versions", forecast.source_versions?.join(", ")], ["market event IDs", forecast.market_event_ids?.join(", ")], ["news version IDs", forecast.news_version_ids?.join(", ")], ["quality", forecast.quality], ["created UTC", utc(forecast.created_at)]].map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{value || "—"}</dd></div>)}</dl></>}</section></div></div>}

      {page === "settings" && <div className="page-content"><div className="settings-grid"><section className="card settings-card"><span className="eyebrow">RISK ENGINE / PAPER</span><h2>{t.kill}</h2><p>{t.confirmKill}</p><div className="setting-status"><span>{t.status}</span><strong className={riskState.kill_switch ? "negative" : "positive"}>{riskState.kill_switch ? t.enabled : t.disabled}</strong></div><button className={riskState.kill_switch ? "secondary" : "danger"} disabled={!!busy} onClick={toggleKill}>{riskState.kill_switch ? t.turnOff : t.turnOn}</button></section><section className="card settings-card"><span className="eyebrow">SYSTEM</span><h2>{t.mode}: PAPER</h2><p>{t.noAi}</p><div className="setting-status"><span>API</span><strong>{health?.status ?? "—"}</strong></div><div className="setting-status"><span>{t.data}</span><strong>{quality?.status ?? "—"}</strong></div><div className="setting-status"><span>{t.config}</span><strong>{portfolios[0]?.config_version ?? "—"}</strong></div><div className="setting-status"><span>{t.updatedAt}</span><strong>{utc(lastUpdated)}</strong></div></section><section className="card settings-card credentials-card"><div className="section-head"><div><span className="eyebrow">EXCHANGE / DRAFT ONLY</span><h2>{t.connections}</h2></div><button type="button" disabled={!!credentialError} onClick={() => setCredentialDialog("create")}>+ {t.addConnection}</button></div><p>{t.connectionsNote} {t.futureExchanges}</p>{credentialError && <div className="banner error" role="alert">{credentialError === "CREDENTIAL_VAULT_UNAVAILABLE" ? t.vaultUnavailable : credentialError} <button type="button" onClick={refreshCredentials}>{t.retry}</button></div>}{!credentialError && (credentials.length ? <div className="credential-list">{credentials.map((item) => <div className="credential-row" key={item.id}><div><strong>{item.label}</strong><small>MEXC Futures · {item.api_key_masked} · v{item.version} · {item.status === "active" ? t.connectionActive : t.connectionRevoked}</small></div><div className="credential-actions">{item.status === "active" && <><button type="button" className="secondary small" disabled={!!busy} onClick={() => setCredentialDialog(item)}>{t.rotateConnection}</button><button type="button" className="danger small" disabled={!!busy} onClick={() => revokeCredential(item)}>{t.revokeConnection}</button></>}</div></div>)}</div> : <div className="empty">{t.noConnections}</div>)}</section></div></div>}
    </main>
    {pilotSettings && <PilotSettingsDialog key={pilotSettings.botId} t={t} language={language} botId={pilotSettings.botId} initial={pilotSettings.value} onCancel={() => setPilotSettings(null)} />}
    {credentialDialog && <CredentialDialog key={credentialDialog === "create" ? "create" : `${credentialDialog.id}-${credentialDialog.version}`} t={t} credential={credentialDialog === "create" ? null : credentialDialog} onDone={async () => { await refreshCredentials(); setCredentialDialog(null); }} onCancel={() => setCredentialDialog(null)} />}
    {botCredentialDialog && <BotCredentialDialog key={botCredentialDialog.botId} t={t} botId={botCredentialDialog.botId} initial={botCredentialDialog.value} credentials={credentials} onDone={(value) => { setBotCredentialDialog(null); setMessage(value.selection_valid ? "✓" : t.selectionInvalid); }} onCancel={() => setBotCredentialDialog(null)} />}
    {wizard && <BotWizard t={t} language={language} defaults={riskState.defaults ?? {}} onCreate={createBot} onCancel={() => setWizard(false)} />}
    {newPortfolio && <div className="modal-backdrop"><form className="card simple-modal" onSubmit={createPortfolio}><div className="modal-head"><h2>{t.createPortfolio}</h2><button className="icon-button" type="button" onClick={() => setNewPortfolio(false)}>×</button></div><p>{t.assumption}</p><label>{t.name}<input maxLength={100} value={portfolioName} onChange={(e) => setPortfolioName(e.target.value)} /></label><label>{t.deposit}<input required type="number" min="0.01" step="any" value={portfolioDeposit} onChange={(e) => setPortfolioDeposit(e.target.value)} /></label><div className="form-actions"><button type="button" className="secondary" onClick={() => setNewPortfolio(false)}>{t.cancel}</button><button disabled={!!busy}>{t.create}</button></div></form></div>}
  </div>;
}

function PortfolioSelect({ t, portfolios, selected, onChange }: { t: typeof words.uk; portfolios: Row[]; selected: string; onChange: (id: string) => void }) {
  return <select aria-label={t.portfolio} value={selected} onChange={(e) => onChange(e.target.value)}><option value="">{t.selectPortfolio}</option>{portfolios.map((p) => <option key={p.id} value={p.id}>{p.name} · {p.id.slice(0, 8)}</option>)}</select>;
}
