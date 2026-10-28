const uuid = "[0-9a-f-]{36}";
const routes: [RegExp, readonly string[]][] = [
  [/^(health|state|risk|markets|portfolios|bots)$/, ["GET"]],
  [/^risk\/kill-switch$/, ["POST"]],
  [/^markets\/BTC_USDT\/(ticker|quality|candles)$/, ["GET"]],
  [new RegExp(`^portfolios/${uuid}$`), ["GET"]],
  [new RegExp(`^portfolios/${uuid}/(orders|positions|decisions|ledger|risk|fills)$`), ["GET"]],
  [new RegExp(`^portfolios/${uuid}/positions/${uuid}/close$`), ["POST"]],
  [new RegExp(`^bots/${uuid}$`), ["GET"]],
  [new RegExp(`^bots/${uuid}/(config|ready|start|pause|stop)$`), ["POST"]],
  [new RegExp(`^bots/${uuid}/decisions$`), ["GET"]],
  [new RegExp(`^bots/${uuid}/live-pilot-settings$`), ["GET", "PUT"]],
  [/^research\/forecasts(?:\/latest)?$/, ["GET"]],
  [new RegExp(`^research/forecasts/${uuid}$`), ["GET"]],
  [/^exchange-credentials$/, ["GET", "POST"]],
  [new RegExp(`^exchange-credentials/${uuid}$`), ["GET"]],
  [new RegExp(`^exchange-credentials/${uuid}/rotate$`), ["PUT"]],
  [new RegExp(`^exchange-credentials/${uuid}/revoke$`), ["POST"]],
  [new RegExp(`^bots/${uuid}/live-credential$`), ["GET", "PUT"]],
];

export function allowedPathMethod(path: string, method: string): boolean {
  if (path === "portfolios" || path === "bots") return method === "GET" || method === "POST";
  return routes.some(([pattern, methods]) => pattern.test(path) && methods.includes(method));
}
