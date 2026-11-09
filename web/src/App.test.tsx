// The app shell: nav, hash routing and the one /api/meta load.
import { act, cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, apiGet } from "./api";
import { App } from "./App";
import type { Meta } from "./meta";
import { forgetMeta, loadMeta } from "./meta";
import { DEFAULT_PAGE, NAV, parseRoute } from "./router";

const META: Meta = {
  version: "0.1.0",
  limits: {
    NAME_MAX: 40,
    ACTION_MAX: 60,
    LINE_MAX: 80,
    LINE_SEPARATOR: " — ",
    REASON_MAX: 110,
    PRINT_CAPACITY: 48,
    AUDIENCES_TIP: ["kids+", "adults"],
    AUDIENCES_LIST: ["kids", "adults"],
    BUCKET_IDS: [1, 2, 3, 4],
    TIP_STATUSES: ["pending", "approved"],
  },
  buckets: [
    { id: 1, title: "Daily checklist" },
    { id: 2, title: "Occasional activity" },
    { id: 3, title: "Optional / when time allows" },
    { id: 4, title: "Left out" },
  ],
  print_capacity: 48,
};

function stubFetch(status: number, body: unknown) {
  const fetchMock = vi.fn((_url: string, _init?: RequestInit) =>
    Promise.resolve(
      new Response(typeof body === "string" ? body : JSON.stringify(body), {
        status,
        headers: { "Content-Type": typeof body === "string" ? "text/html" : "application/json" },
      }),
    ),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

async function goTo(hash: string) {
  await act(async () => {
    window.location.hash = hash;
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  });
}

beforeEach(() => {
  forgetMeta();
  window.location.hash = "";
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("App", () => {
  it("renders the nav with Review, Library, Lists and Buckets as hash links", async () => {
    stubFetch(200, META);
    render(<App />);
    const nav = screen.getByRole("navigation", { name: "Main" });
    const links = within(nav).getAllByRole("link");
    expect(links.map((link) => link.textContent)).toEqual(["Review", "Library", "Lists", "Buckets"]);
    expect(links.map((link) => link.getAttribute("href"))).toEqual([
      "#/review",
      "#/library",
      "#/lists",
      "#/buckets",
    ]);
    // An empty hash shows the review inbox, marked as the current page.
    expect(await screen.findByRole("heading", { name: "Review inbox" })).toBeTruthy();
    expect(within(nav).getByRole("link", { name: "Review" }).getAttribute("aria-current")).toBe("page");
  });

  it("follows the hash to each page and marks it current", async () => {
    stubFetch(200, META);
    render(<App />);
    const nav = screen.getByRole("navigation", { name: "Main" });
    for (const [hash, heading, label] of [
      ["#/library", "Library", "Library"],
      ["#/lists", "Lists", "Lists"],
      ["#/review", "Review inbox", "Review"],
    ] as const) {
      await goTo(hash);
      expect(screen.getByRole("heading", { level: 1 }).textContent).toBe(heading);
      expect(within(nav).getByRole("link", { name: label }).getAttribute("aria-current")).toBe("page");
      const others = within(nav)
        .getAllByRole("link")
        .filter((link) => link.textContent !== label);
      expect(others.every((link) => link.getAttribute("aria-current") === null)).toBe(true);
    }
  });

  it("shows the bucket titles from /api/meta, fetched once", async () => {
    const fetchMock = stubFetch(200, META);
    window.location.hash = "#/buckets";
    render(<App />);
    const list = await screen.findByRole("list", { name: "Buckets" });
    expect(within(list).getAllByRole("listitem").map((item) => item.textContent)).toEqual(
      META.buckets.map((bucket) => bucket.title),
    );
    await goTo("#/review");
    await goTo("#/buckets");
    await screen.findByRole("list", { name: "Buckets" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/meta");
  });

  it("shows an unknown hash as not found, with a link to the review inbox", async () => {
    stubFetch(200, META);
    window.location.hash = "#/nowhere";
    render(<App />);
    expect(screen.getByRole("heading", { name: "Page not found" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "Go to the review inbox" }).getAttribute("href")).toBe("#/review");
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).getAllByRole("link").every((link) => link.getAttribute("aria-current") === null)).toBe(
      true,
    );
  });

  it("shows an alert when /api/meta fails", async () => {
    stubFetch(404, { error: { code: "CC_E_NOT_FOUND", message: "no API route /api/meta", fields: [] } });
    render(<App />);
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("no API route /api/meta");
  });
});

describe("parseRoute", () => {
  it("maps every nav page and ignores slashes at either end", () => {
    for (const item of NAV) {
      expect(parseRoute(`#/${item.page}`)).toEqual({ kind: "page", page: item.page });
      expect(parseRoute(`#/${item.page}/`)).toEqual({ kind: "page", page: item.page });
      expect(parseRoute(`#${item.page}`)).toEqual({ kind: "page", page: item.page });
    }
  });

  it("sends an empty hash to the default page and anything else to not found", () => {
    for (const hash of ["", "#", "#/", "#//"]) {
      expect(parseRoute(hash)).toEqual({ kind: "page", page: DEFAULT_PAGE });
    }
    expect(DEFAULT_PAGE).toBe("review");
    expect(parseRoute("#/Review")).toEqual({ kind: "not-found", path: "Review" });
    expect(parseRoute("#/review/extra")).toEqual({ kind: "not-found", path: "review/extra" });
  });
});

describe("api and meta", () => {
  it("turns an API error body into an ApiError with its code and fields", async () => {
    stubFetch(400, { error: { code: "CC_E_VALIDATION", message: "name is too long", fields: ["name"] } });
    const error = await apiGet("/tips").catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 400, code: "CC_E_VALIDATION", message: "name is too long", fields: ["name"] });
  });

  it("turns a non-JSON error into an ApiError without a code", async () => {
    stubFetch(502, "<html>Bad gateway</html>");
    const error = await apiGet("/meta").catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 502, code: null, fields: [] });
  });

  it("retries /api/meta after a failed load", async () => {
    stubFetch(503, "down");
    await expect(loadMeta()).rejects.toBeInstanceOf(ApiError);
    const fetchMock = stubFetch(200, META);
    await expect(loadMeta()).resolves.toEqual(META);
    await loadMeta();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
