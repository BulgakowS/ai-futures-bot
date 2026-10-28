import { NextRequest, NextResponse } from "next/server";
import { permittedHost } from "../host";
import { allowedPathMethod } from "../policy";

export const dynamic = "force-dynamic";

async function relay(request: NextRequest, segments: string[], method: "GET" | "POST" | "PUT") {
  const path = segments.join("/");
  if (!allowedPathMethod(path, method)) {
    return NextResponse.json({ code: "NOT_FOUND" }, { status: 404, headers: { "Cache-Control": "no-store" } });
  }
  const host = request.headers.get("host") ?? "";
  if (!permittedHost(host, process.env.APP_ENV, process.env.AF_BFF_TEST_PORT)) {
    return NextResponse.json({ code: "LOCAL_ONLY" }, { status: 403, headers: { "Cache-Control": "no-store" } });
  }
  if (method === "POST" || method === "PUT") {
    const origin = request.headers.get("origin");
    if (!origin || origin !== `http://${host}` || !request.headers.get("content-type")?.startsWith("application/json")) {
      return NextResponse.json({ code: "INVALID_ORIGIN" }, { status: 403, headers: { "Cache-Control": "no-store" } });
    }
  }
  const token = process.env.BACKEND_AUTH_TOKEN;
  const base = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";
  if (!token) return NextResponse.json({ code: "BFF_NOT_CONFIGURED" }, { status: 503, headers: { "Cache-Control": "no-store" } });
  try {
    const target = new URL(`/api/v1/${path}`, base);
    if (method === "GET" && path.endsWith("/candles")) {
      const timeframe = request.nextUrl.searchParams.get("timeframe") ?? "1m";
      if (!["1m", "5m", "15m"].includes(timeframe)) return NextResponse.json({ code: "INVALID_TIMEFRAME" }, { status: 422, headers: { "Cache-Control": "no-store" } });
      target.searchParams.set("timeframe", timeframe);
      target.searchParams.set("limit", "100");
    }
    const response = await fetch(target, {
      method,
      headers: { Authorization: `Bearer ${token}`, ...(method !== "GET" ? { "Content-Type": "application/json" } : {}) },
      body: method !== "GET" ? await request.text() : undefined,
      cache: "no-store",
      signal: AbortSignal.timeout(10000),
    });
    const body = await response.text();
    return new NextResponse(body, {
      status: response.status,
      headers: { "Content-Type": response.headers.get("content-type") ?? "application/json", "Cache-Control": "no-store" },
    });
  } catch {
    return NextResponse.json({ code: "BACKEND_UNAVAILABLE", retryable: true }, { status: 503, headers: { "Cache-Control": "no-store" } });
  }
}

export async function GET(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  return relay(request, (await context.params).path, "GET");
}

export async function POST(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  return relay(request, (await context.params).path, "POST");
}

export async function PUT(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  return relay(request, (await context.params).path, "PUT");
}
