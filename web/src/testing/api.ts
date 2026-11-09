// Test helpers: the demo server's payloads and a fetch stub that answers by method and path.
// fixtures/demoReview.json is what `checklist-care serve --demo` returns for /api/meta (minus
// the version), /api/sources, /api/lists and /api/tips?status=pending;
// tests/integration/test_api_review.py fails when the two differ.
import { vi } from "vitest";
import demo from "../fixtures/demoReview.json";
import type { Meta } from "../meta";
import type { ListInfo, SourceInfo, Tip } from "../model";

export const META: Meta = { version: "0.0.0-test", ...(demo.meta as Omit<Meta, "version">) };
export const DEMO_SOURCES = demo.sources as SourceInfo[];
export const DEMO_LISTS = demo.lists as ListInfo[];
export const DEMO_TIPS = demo.tips as Tip[];

/** A recorded request: its method, its path (with the query) and its parsed JSON body. */
export interface SentRequest {
  method: string;
  url: string;
  body: unknown;
}

/** An answer: a status and a JSON body (a string is sent as an HTML page). */
export interface StubAnswer {
  status: number;
  body?: unknown;
}

export type StubRoute = (request: SentRequest) => StubAnswer | undefined;

/** JSON 200 with `body`. */
export function ok(body: unknown): StubAnswer {
  return { status: 200, body };
}

/**
 * Replace `fetch` with a stub that asks each route in turn and answers with the first match;
 * an unmatched request is a 404 API error. Returns the list of requests it received.
 */
export function stubApi(...routes: StubRoute[]): SentRequest[] {
  const sent: SentRequest[] = [];
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    const request: SentRequest = {
      method: init?.method ?? "GET",
      url,
      body: typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
    };
    sent.push(request);
    const answer = routes.map((route) => route(request)).find((item) => item !== undefined) ?? {
      status: 404,
      body: { error: { code: "CC_E_NOT_FOUND", message: `no stub for ${request.method} ${url}`, fields: [] } },
    };
    if (answer.status === 204) return Promise.resolve(new Response(null, { status: 204 }));
    const isPage = typeof answer.body === "string";
    return Promise.resolve(
      new Response(isPage ? (answer.body as string) : JSON.stringify(answer.body ?? null), {
        status: answer.status,
        headers: { "Content-Type": isPage ? "text/html" : "application/json" },
      }),
    );
  });
  vi.stubGlobal("fetch", fetchMock);
  return sent;
}

/** A route answering `GET <url>` (exact path and query). */
export function get(url: string, answer: StubAnswer): StubRoute {
  return (request) => (request.method === "GET" && request.url === url ? answer : undefined);
}

/** The demo inbox: /api/meta, /api/sources, /api/lists and the pending tips. */
export function demoRoutes(tips: readonly Tip[] = DEMO_TIPS): StubRoute[] {
  return [
    get("/api/meta", ok(META)),
    get("/api/sources", ok(DEMO_SOURCES)),
    get("/api/lists", ok(DEMO_LISTS)),
    get("/api/tips?status=pending", ok(tips)),
  ];
}
