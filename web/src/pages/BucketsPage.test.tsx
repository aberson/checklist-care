// The Buckets page, rendered from the demo server's own payloads (fixtures/demoReview.json, kept
// equal to `serve --demo` by tests/integration/test_api_review.py). /api/buckets is a small
// stand-in for the server: PUT stores the fields sent and refuses an empty one with the server's
// 400, and /api/meta reports the stored titles, as the real routes do
// (tests/integration/test_rubric_roundtrip.py checks the real ones, through to `context`).
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { forgetMeta } from "../meta";
import type { Bucket, BucketPut } from "../model";
import type { SentRequest, StubAnswer, StubRoute } from "../testing/api";
import { DEMO_BUCKETS, DEMO_LISTS, DEMO_SOURCES, get, META, ok, stubApi } from "../testing/api";
import { BucketsPage, bucketDraftProblem, bucketPutOf, changedAt } from "./BucketsPage";

const { limits } = META;
const SAVED_AT = "2026-11-11T09:30:00Z";

function required<T>(value: T | undefined, what: string): T {
  if (value === undefined) throw new Error(`the demo fixture has no ${what}`);
  return value;
}

const DAILY = required(
  DEMO_BUCKETS.find((bucket) => bucket.id === limits.BUCKET_DAILY),
  "daily bucket",
);
const OCCASIONAL = required(DEMO_BUCKETS[1], "second bucket");

const NEW_RUBRIC =
  "Worth doing every day, or each time its cue comes round: two minutes or less.\nOne clear physical action.";

function refused(field: string, message: string): StubAnswer {
  return { status: 400, body: { error: { code: "CC_E_VALIDATION", message, fields: [field] } } };
}

/** What the stand-in server does for /api/buckets and /api/meta, over its own copy of the demo buckets. */
function bucketsServer(): StubRoute[] {
  let stored: Bucket[] = DEMO_BUCKETS.map((bucket) => ({ ...bucket }));
  const list = (request: SentRequest) =>
    request.method === "GET" && request.url === "/api/buckets" ? ok(stored) : undefined;
  const meta = (request: SentRequest) =>
    request.method === "GET" && request.url === "/api/meta"
      ? ok({ ...META, buckets: stored.map(({ id, title }) => ({ id, title })) })
      : undefined;
  const put = (request: SentRequest): StubAnswer | undefined => {
    const match = /^\/api\/buckets\/(\d+)$/.exec(request.url);
    if (request.method !== "PUT" || match === null) return undefined;
    const id = Number(match[1]);
    const body = request.body as BucketPut;
    for (const field of ["title", "rubric"] as const) {
      const value = body[field];
      if (value !== undefined && value.trim() === "") return refused(field, `${field}: must not be empty`);
    }
    const current = stored.find((bucket) => bucket.id === id);
    if (current === undefined) {
      return { status: 404, body: { error: { code: "CC_E_NOT_FOUND", message: `there is no bucket #${id}`, fields: [] } } };
    }
    const updated: Bucket = { ...current, ...body, updated_at: SAVED_AT };
    stored = stored.map((bucket) => (bucket.id === id ? updated : bucket));
    return ok(updated);
  };
  return [list, meta, put];
}

async function renderBuckets(...routes: StubRoute[]): Promise<SentRequest[]> {
  const sent = stubApi(...routes, ...bucketsServer());
  render(<BucketsPage meta={META} />);
  await screen.findByRole("list", { name: "Buckets" });
  return sent;
}

function card(bucket: Bucket): HTMLElement {
  return screen.getByRole("listitem", { name: `${bucket.id}. ${bucket.title}` });
}

function titleBox(bucket: Bucket): HTMLInputElement {
  return within(card(bucket)).getByLabelText("Title") as HTMLInputElement;
}

function rubricBox(bucket: Bucket): HTMLTextAreaElement {
  return within(card(bucket)).getByLabelText("Rubric") as HTMLTextAreaElement;
}

function button(bucket: Bucket, name: string): HTMLButtonElement {
  return within(card(bucket)).getByRole("button", { name }) as HTMLButtonElement;
}

function writes(sent: readonly SentRequest[]): SentRequest[] {
  return sent.filter((request) => request.method !== "GET");
}

function status(): string {
  return screen.getByRole("status").textContent ?? "";
}

beforeEach(() => {
  forgetMeta();
  window.location.hash = "";
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("BucketsPage on the demo fixture", () => {
  it("shows the four buckets with their stored titles and rubrics, nothing to save", async () => {
    const sent = await renderBuckets();

    const list = screen.getByRole("list", { name: "Buckets" });
    expect(within(list).getAllByRole("heading", { level: 2 }).map((heading) => heading.textContent)).toEqual(
      DEMO_BUCKETS.map((bucket) => `${bucket.id}. ${bucket.title}`),
    );
    for (const bucket of DEMO_BUCKETS) {
      expect(titleBox(bucket).value).toBe(bucket.title);
      expect(rubricBox(bucket).value).toBe(bucket.rubric);
      expect(titleBox(bucket).maxLength).toBe(limits.BUCKET_TITLE_MAX);
      expect(rubricBox(bucket).maxLength).toBe(limits.RUBRIC_MAX);
      expect(button(bucket, "Save").disabled).toBe(true);
      expect(within(card(bucket)).getByText(`Last changed ${changedAt(bucket.updated_at)}.`)).toBeTruthy();
    }
    expect(sent.map((request) => `${request.method} ${request.url}`)).toEqual(["GET /api/buckets"]);
  });

  it("edits a rubric and saves it: only the rubric is sent, and the saved text stays", async () => {
    const sent = await renderBuckets();
    const before = `${DAILY.rubric.length} / ${limits.RUBRIC_MAX}`;
    expect(within(card(DAILY)).getByText(before)).toBeTruthy();

    fireEvent.change(rubricBox(DAILY), { target: { value: `  ${NEW_RUBRIC}\n` } });
    expect(button(DAILY, "Save").disabled).toBe(false);
    expect(button(DAILY, "Undo changes").disabled).toBe(false);
    fireEvent.click(button(DAILY, "Save"));

    await waitFor(() => expect(status()).toContain("Saved bucket 1"));
    expect(writes(sent)).toEqual([{ method: "PUT", url: `/api/buckets/${DAILY.id}`, body: { rubric: NEW_RUBRIC } }]);
    expect(status()).toBe(`Saved bucket 1, “${DAILY.title}”. The skill sorts its next ingest by the new rubric.`);
    expect(rubricBox(DAILY).value).toBe(NEW_RUBRIC);
    expect(titleBox(DAILY).value).toBe(DAILY.title);
    expect(button(DAILY, "Save").disabled).toBe(true);
    expect(within(card(DAILY)).getByText(`Last changed ${changedAt(SAVED_AT)}.`)).toBeTruthy();
    // The other buckets are untouched, and the rubric edit fetched no new meta.
    expect(rubricBox(OCCASIONAL).value).toBe(OCCASIONAL.rubric);
    expect(sent.filter((request) => request.url === "/api/meta")).toHaveLength(0);
  });

  it("will not save an empty rubric or title, and Undo changes brings the stored text back", async () => {
    const sent = await renderBuckets();

    fireEvent.change(rubricBox(DAILY), { target: { value: "  \n  " } });
    expect(button(DAILY, "Save").disabled).toBe(true);
    expect(within(card(DAILY)).getByText(/The rubric is empty/)).toBeTruthy();
    fireEvent.change(titleBox(DAILY), { target: { value: "" } });
    expect(within(card(DAILY)).getByText("The title is empty.")).toBeTruthy();

    fireEvent.click(button(DAILY, "Undo changes"));

    expect(rubricBox(DAILY).value).toBe(DAILY.rubric);
    expect(titleBox(DAILY).value).toBe(DAILY.title);
    expect(button(DAILY, "Save").disabled).toBe(true);
    expect(writes(sent)).toEqual([]);
  });

  it("shows the server's refusal and keeps the draft", async () => {
    const message = "rubric: may hold line breaks (LF) and tabs, but no carriage return";
    const sent = await renderBuckets((request) =>
      request.method === "PUT" ? refused("rubric", message) : undefined,
    );

    fireEvent.change(rubricBox(DAILY), { target: { value: "Fine here, refused there" } });
    fireEvent.click(button(DAILY, "Save"));

    const alert = await within(card(DAILY)).findByRole("alert");
    expect(alert.textContent).toBe(`Could not save: ${message}`);
    expect(rubricBox(DAILY).value).toBe("Fine here, refused there");
    expect(button(DAILY, "Save").disabled).toBe(false);
    expect(status()).toBe("");
    expect(writes(sent)).toHaveLength(1);
  });

  it("says so when the buckets cannot be loaded", async () => {
    stubApi(get("/api/buckets", { status: 503, body: "down" }));
    render(<BucketsPage meta={META} />);

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Cannot load the buckets");
    expect(screen.queryByRole("list", { name: "Buckets" })).toBeNull();
  });
});

describe("bucket form rules", () => {
  it("sends only the changed fields, trimmed", () => {
    expect(bucketPutOf(DAILY, { title: DAILY.title, rubric: DAILY.rubric })).toEqual({});
    expect(bucketPutOf(DAILY, { title: ` ${DAILY.title} `, rubric: `${DAILY.rubric}\n` })).toEqual({});
    expect(bucketPutOf(DAILY, { title: "Every day", rubric: DAILY.rubric })).toEqual({ title: "Every day" });
  });

  it("measures the limits from /api/meta", () => {
    const longest = { title: "t".repeat(limits.BUCKET_TITLE_MAX), rubric: "r".repeat(limits.RUBRIC_MAX) };
    expect(bucketDraftProblem(longest, META)).toBeNull();
    expect(bucketDraftProblem({ ...longest, title: `${longest.title}t` }, META)).toBe(
      `The title is ${limits.BUCKET_TITLE_MAX + 1} characters; ${limits.BUCKET_TITLE_MAX} fit.`,
    );
    expect(bucketDraftProblem({ ...longest, rubric: `${longest.rubric}r` }, META)).toBe(
      `The rubric is ${limits.RUBRIC_MAX + 1} characters; ${limits.RUBRIC_MAX} fit.`,
    );
  });
});

describe("renaming a bucket in the app", () => {
  it("shows the new title on the other pages without a reload", async () => {
    const sent = stubApi(
      ...bucketsServer(),
      get("/api/sources", ok(DEMO_SOURCES)),
      get("/api/lists", ok(DEMO_LISTS)),
      get("/api/tips", ok([])),
    );
    window.location.hash = "#/buckets";
    render(<App />);
    await screen.findByRole("list", { name: "Buckets" });

    fireEvent.change(titleBox(OCCASIONAL), { target: { value: "Weekly activity" } });
    fireEvent.click(button(OCCASIONAL, "Save"));

    await waitFor(() => expect(status()).toContain("Weekly activity"));
    expect(status()).toBe(`Saved bucket ${OCCASIONAL.id}, “Weekly activity” (was “${OCCASIONAL.title}”).`);
    expect(writes(sent)).toEqual([
      { method: "PUT", url: `/api/buckets/${OCCASIONAL.id}`, body: { title: "Weekly activity" } },
    ]);
    expect(screen.getByRole("heading", { level: 2, name: `${OCCASIONAL.id}. Weekly activity` })).toBeTruthy();
    await waitFor(() => expect(sent.filter((request) => request.url === "/api/meta")).toHaveLength(2));

    await act(async () => {
      window.location.hash = "#/library";
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    });
    const bucketFilter = await screen.findByLabelText("Bucket");
    await waitFor(() =>
      expect(within(bucketFilter).getByRole("option", { name: `${OCCASIONAL.id}. Weekly activity` })).toBeTruthy(),
    );
    expect(within(bucketFilter).queryByRole("option", { name: `${OCCASIONAL.id}. ${OCCASIONAL.title}` })).toBeNull();
  });
});
