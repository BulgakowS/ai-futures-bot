type Snapshot = Record<string, any> | null;

export async function parseOverviewResponse(path: string, response: Response): Promise<any> {
  const value = await response.json().catch(() => ({}));
  const degradedHealth = path === "health" && response.status === 503 && value?.status === "degraded"
    && value?.mode === "paper" && value?.dependencies && typeof value.dependencies === "object"
    && !Array.isArray(value.dependencies);
  if (!response.ok && !degradedHealth) throw new Error(value.code || value.message || `HTTP ${response.status}`);
  return value;
}

export function overviewDegradedReason(health: Snapshot, quality: Snapshot, ticker: Snapshot): string {
  if (quality?.trade_eligible === false || (ticker?.quality_status && ticker.quality_status !== "ok")) {
    return quality?.reasons?.join(", ") || ticker?.quality_status || "API";
  }
  if (health?.status === "degraded") {
    const failures = Object.entries(health.dependencies ?? {}).filter(([, status]) => status !== "ok");
    if (failures.length) return failures.map(([name, status]) => `${name}: ${status}`).join(", ");
  }
  return quality?.reasons?.join(", ") || ticker?.quality_status || "API";
}

export function overviewTradeEligible(load: string, health: Snapshot, quality: Snapshot, ticker: Snapshot): boolean {
  return load === "ready" && health?.status === "ok" && quality?.trade_eligible === true
    && ticker?.quality_status === "ok";
}
