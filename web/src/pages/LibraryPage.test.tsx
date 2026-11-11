// The Library, rendered from the demo server's own payloads (fixtures/demoReview.json, kept equal
// to `serve --demo` by tests/integration/test_api_review.py). /api/tips is a small stand-in for the
// server: GET filters by the query the page sends (so a `kids+` sent without encoding matches
// nothing), POST stores an approved manual tip, and DELETE drops a tip and notes it on its
// duplicates, as the real routes do.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { forgetMeta } from "../meta";
import type { NewTip, PatchResult, Tip } from "../model";
import type { SentRequest, StubAnswer, StubRoute } from "../testing/api";
import { DEMO_LISTS, DEMO_SOURCES, DEMO_TIPS, DEMO_UNPLACED, get, META, ok, stubApi } from "../testing/api";
import { LibraryPage, NO_FILTERS, shownUnder, tipsPath } from "./LibraryPage";

const { limits } = META;
const MANUAL = DEMO_SOURCES.find((source) => source.kind === limits.SOURCE_KIND_MANUAL);

function required<T>(value: T | undefined | null, what: string): T {
  if (value === undefined || value === null) throw new Error(`the demo fixture has no ${what}`);
  return value;
}

function tipNamed(name: string): Tip {
  return required(
    [...DEMO_TIPS, ...DEMO_UNPLACED].find((tip) => tip.name === name),
    `tip named ${name}`,
  );
}

function bucketName(id: number): string {
  return required(META.buckets.find((bucket) => bucket.id === id), `bucket ${id}`).title;
}

function listTitle(id: number): string {
  return required(DEMO_LISTS.find((list) => list.id === id), `list ${id}`).title;
}

const DUPLICATE = required(
  DEMO_TIPS.find((tip) => tip.duplicate_of_tip !== null),
  "duplicate",
);

/** The approved tip the demo duplicate repeats, as GET /api/tips shows it. */
const ORIGINAL: Tip = (() => {
  const brief = required(DUPLICATE.duplicate_of_tip, "original");
  const template = required(DEMO_UNPLACED[0], "approved tip");
  const source = required(
    DEMO_SOURCES.find((item) => item.id === brief.source_id),
    "source of the original",
  );
  return {
    ...template,
    id: brief.id,
    name: brief.name,
    action: brief.action,
    bucket: brief.bucket,
    status: brief.status,
    source_id: brief.source_id,
    source_ref: source.ref,
    list_ids: [],
    note: null,
  };
})();

/** Every tip the stand-in server stores: the demo inbox, the approved unplaced tips and the original. */
const LIBRARY: readonly Tip[] = [...DEMO_TIPS, ...DEMO_UNPLACED, ORIGINAL].sort((a, b) => a.id - b.id);

const SHOES = tipNamed("Shoes By The Door");

function label(tip: Tip): string {
  return `Tip #${tip.id} ${tip.name}`;
}

/** What the stand-in server does for /api/tips, over its own copy of `initial`. */
function tipsServer(initial: readonly Tip[]): StubRoute[] {
  let stored = [...initial];
  const list: StubRoute = (request) => {
    const url = new URL(request.url, "http://127.0.0.1");
    if (request.method !== "GET" || url.pathname !== "/api/tips") return undefined;
    const query = url.searchParams;
    const matches = (tip: Tip) =>
      (!query.has("status") || tip.status === query.get("status")) &&
      (!query.has("bucket") || tip.bucket === Number(query.get("bucket"))) &&
      (!query.has("audience") || tip.audience === query.get("audience")) &&
      (!query.has("source_id") || tip.source_id === Number(query.get("source_id")));
    return ok(stored.filter(matches));
  };
  const add: StubRoute = (request) => {
    if (request.method !== "POST" || request.url !== "/api/tips") return undefined;
    const body = request.body as NewTip;
    const created: Tip = {
      ...required(DEMO_UNPLACED[0], "approved tip"),
      id: Math.max(...stored.map((tip) => tip.id)) + 1,
      name: body.name,
      action: body.action,
      reason: body.reason ?? null,
      bucket: body.bucket,
      audience: body.audience,
      status: limits.TIP_STATUS_APPROVED,
      source_id: required(MANUAL, "manual source").id,
      source_ref: required(MANUAL, "manual source").ref,
      start_s: null,
      start_url: null,
      note: null,
      list_ids: [...body.list_ids].sort((a, b) => a - b),
    };
    stored = [...stored, created];
    return { status: 201, body: created };
  };
  const remove: StubRoute = (request) => {
    const match = /^\/api\/tips\/(\d+)$/.exec(request.url);
    if (request.method !== "DELETE" || match === null) return undefined;
    const id = Number(match[1]);
    const gone = stored.find((tip) => tip.id === id);
    if (gone === undefined) return { status: 404, body: { error: { code: "CC_E_NOT_FOUND", message: `there is no tip #${id}`, fields: [] } } };
    const marker = `(original #${id} "${gone.name}" deleted)`;
    stored = stored
      .filter((tip) => tip.id !== id)
      .map((tip) =>
        tip.duplicate_of === id
          ? { ...tip, duplicate_of: null, duplicate_of_tip: null, note: tip.note === null ? marker : `${tip.note} ${marker}` }
          : tip,
      );
    return { status: 204 };
  };
  return [list, add, remove];
}

function libraryRoutes(): StubRoute[] {
  return [
    get("/api/meta", ok(META)),
    get("/api/sources", ok(DEMO_SOURCES)),
    get("/api/lists", ok(DEMO_LISTS)),
    ...tipsServer(LIBRARY),
  ];
}

function route(method: string, url: string, answer: (request: SentRequest) => StubAnswer): StubRoute {
  return (request) => (request.method === method && request.url === url ? answer(request) : undefined);
}

function patched(tip: Tip, change: Partial<Tip>, extra: Partial<PatchResult> = {}): PatchResult {
  return { tip: { ...tip, ...change }, removed_placements: [], cleared_duplicate_of: null, ...extra };
}

async function renderLibrary(...routes: StubRoute[]): Promise<SentRequest[]> {
  const sent = stubApi(...routes, ...libraryRoutes());
  render(<LibraryPage meta={META} />);
  await screen.findByRole("list", { name: "Tips" });
  return sent;
}

function shownLabels(): string[] {
  const list = screen.queryByRole("list", { name: "Tips" });
  if (list === null) return [];
  return within(list)
    .getAllByRole("listitem")
    .map((item) => item.getAttribute("aria-label") ?? "");
}

function card(tip: Tip): HTMLElement {
  return screen.getByRole("listitem", { name: label(tip) });
}

function filters(): HTMLElement {
  return screen.getByRole("group", { name: "Filters" });
}

function setFilter(name: string, value: string): void {
  fireEvent.change(within(filters()).getByLabelText(name), { target: { value } });
}

function tipsUrls(sent: readonly SentRequest[]): string[] {
  return sent.filter((request) => request.method === "GET" && request.url.startsWith("/api/tips")).map((r) => r.url);
}

function writes(sent: readonly SentRequest[]): { method: string; url: string; body: unknown }[] {
  return sent.filter((request) => request.method !== "GET");
}

function status(): string {
  return screen.getByRole("status").textContent ?? "";
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("LibraryPage on the demo fixture", () => {
  it("shows every stored tip except Left out, and Left out under its own bucket", async () => {
    const sent = await renderLibrary();

    const kept = LIBRARY.filter((tip) => tip.bucket !== limits.BUCKET_LEFT_OUT);
    expect(shownLabels()).toEqual(kept.map(label));
    expect(screen.getByText(`Showing ${kept.length} tips.`, { exact: false })).toBeTruthy();
    expect(tipsUrls(sent)).toEqual(["/api/tips"]);
    // Every bucket a reader can browse is here, Optional included.
    expect(new Set(kept.map((tip) => tip.bucket))).toEqual(new Set([1, 2, 3]));
    expect(within(card(SHOES)).getByText(`On ${SHOES.list_ids.map(listTitle).join(", ")}`)).toBeTruthy();
    expect(within(card(SHOES)).getByRole("link", { name: "Waiting for review" }).getAttribute("href")).toBe("#/review");
    expect(within(card(ORIGINAL)).getByText("Approved")).toBeTruthy();

    setFilter("Bucket", String(limits.BUCKET_LEFT_OUT));

    const leftOut = LIBRARY.filter((tip) => tip.bucket === limits.BUCKET_LEFT_OUT);
    await waitFor(() => expect(shownLabels()).toEqual(leftOut.map(label)));
    expect(tipsUrls(sent).at(-1)).toBe(`/api/tips?bucket=${limits.BUCKET_LEFT_OUT}`);
    const original = required(DUPLICATE.duplicate_of_tip, "original");
    expect(within(card(DUPLICATE)).getByText(`Duplicate of #${original.id} · ${original.name}`)).toBeTruthy();
  });

  it("sends the status, audience and source filters to the server and shows what matches", async () => {
    const sent = await renderLibrary();
    const lights = tipNamed("Lights Off As You Leave");

    setFilter("Status", limits.TIP_STATUS_APPROVED);
    setFilter("Audience", limits.AUDIENCE_KIDS_PLUS);
    setFilter("Source", String(lights.source_id));

    await waitFor(() => expect(shownLabels()).toEqual([label(lights)]));
    expect(tipsUrls(sent).at(-1)).toBe(`/api/tips?status=approved&audience=kids%2B&source_id=${lights.source_id}`);
    const shown = LIBRARY.filter((tip) =>
      shownUnder(tip, { ...NO_FILTERS, status: "approved", audience: "kids+", sourceId: lights.source_id }, META),
    );
    expect(shown.map(label)).toEqual([label(lights)]);

    fireEvent.click(within(filters()).getByRole("button", { name: "Clear filters" }));
    await waitFor(() => expect(tipsUrls(sent).at(-1)).toBe("/api/tips"));
    expect(shownLabels()).toHaveLength(LIBRARY.filter((tip) => tip.bucket !== limits.BUCKET_LEFT_OUT).length);
  });

  it("adds my own tip, approved and straight onto the chosen lists", async () => {
    const sent = await renderLibrary();
    const before = shownLabels().length;

    fireEvent.click(screen.getByRole("button", { name: "Add my own tip" }));
    const form = screen.getByRole("region", { name: "Add my own tip" });
    const addButton = within(form).getByRole("button", { name: "Add tip" }) as HTMLButtonElement;
    expect(addButton.disabled).toBe(true);
    fireEvent.change(within(form).getByLabelText("Name"), { target: { value: "Brush your teeth" } });
    fireEvent.change(within(form).getByLabelText("What to do"), {
      target: { value: "Brush for two full minutes, morning and night" },
    });
    expect(addButton.disabled).toBe(false);
    // A new tip starts as an adults tip, so no kids list is offered until it is kids+.
    expect(within(form).queryByLabelText(listTitle(3))).toBeNull();
    fireEvent.change(within(form).getByLabelText("Audience"), { target: { value: limits.AUDIENCE_KIDS_PLUS } });
    fireEvent.click(within(form).getByLabelText(listTitle(3)));
    fireEvent.click(within(form).getByLabelText(listTitle(1)));
    fireEvent.click(addButton);

    await waitFor(() => expect(screen.queryByRole("region", { name: "Add my own tip" })).toBeNull());
    expect(writes(sent)).toEqual([
      {
        method: "POST",
        url: "/api/tips",
        body: {
          name: "Brush your teeth",
          action: "Brush for two full minutes, morning and night",
          bucket: limits.BUCKET_DAILY,
          audience: limits.AUDIENCE_KIDS_PLUS,
          list_ids: [3, 1],
        },
      },
    ]);
    const labels = shownLabels();
    expect(labels).toHaveLength(before + 1);
    const added = screen.getByRole("listitem", { name: labels.at(-1) ?? "" });
    expect(labels.at(-1)).toMatch(/^Tip #\d+ Brush your teeth$/);
    const onLists = `${listTitle(1)}, ${listTitle(3)}`;
    expect(within(added).getByText(`On ${onLists}`)).toBeTruthy();
    expect(within(added).getByText(required(MANUAL, "manual source").title ?? "")).toBeTruthy();
    expect(within(added).getByText("Approved")).toBeTruthy();
    expect(status()).toBe(`Added “Brush your teeth” to ${bucketName(1)}, approved. It prints on ${onLists}.`);
    // The add button is back for the next tip.
    expect(screen.getByRole("button", { name: "Add my own tip" })).toBeTruthy();
  });

  it("sends a reason when one is given and keeps the form when the server refuses", async () => {
    const sent = await renderLibrary(
      route("POST", "/api/tips", () => ({
        status: 409,
        body: { error: { code: "CC_E_CONFLICT", message: "list 3 accepts only kids+ tips", fields: ["list_ids"] } },
      })),
    );

    fireEvent.click(screen.getByRole("button", { name: "Add my own tip" }));
    const form = screen.getByRole("region", { name: "Add my own tip" });
    fireEvent.change(within(form).getByLabelText("Name"), { target: { value: "Floss" } });
    fireEvent.change(within(form).getByLabelText("What to do"), { target: { value: "Floss after brushing" } });
    fireEvent.change(within(form).getByLabelText("Why"), { target: { value: "  Clean between the teeth.  " } });
    await act(async () => {
      fireEvent.click(within(form).getByRole("button", { name: "Add tip" }));
    });

    expect((await within(form).findByRole("alert")).textContent).toBe(
      "Could not add the tip: list 3 accepts only kids+ tips",
    );
    expect((within(form).getByLabelText("Name") as HTMLInputElement).value).toBe("Floss");
    expect(writes(sent)).toEqual([
      {
        method: "POST",
        url: "/api/tips",
        body: {
          name: "Floss",
          action: "Floss after brushing",
          reason: "Clean between the teeth.",
          bucket: limits.BUCKET_DAILY,
          audience: limits.AUDIENCE_ADULTS,
          list_ids: [],
        },
      },
    ]);
  });

  it("says so when a new tip is hidden by the current filters", async () => {
    await renderLibrary();
    setFilter("Bucket", String(limits.BUCKET_DAILY));
    await waitFor(() => expect(shownLabels().length).toBeGreaterThan(0));
    const daily = shownLabels();

    fireEvent.click(screen.getByRole("button", { name: "Add my own tip" }));
    const form = screen.getByRole("region", { name: "Add my own tip" });
    fireEvent.change(within(form).getByLabelText("Name"), { target: { value: "Label The Fuse Box" } });
    fireEvent.change(within(form).getByLabelText("What to do"), { target: { value: "Write each room on the fuse box" } });
    fireEvent.change(within(form).getByLabelText("Bucket"), { target: { value: "3" } });
    fireEvent.click(within(form).getByRole("button", { name: "Add tip" }));

    await waitFor(() => expect(status()).toContain("Added “Label The Fuse Box”"));
    expect(status()).toBe(`Added “Label The Fuse Box” to ${bucketName(3)}, approved. The current filters hide it.`);
    expect(shownLabels()).toEqual(daily);
  });

  it("moves a tip to bucket 3 and reports the lists it left; the Daily filter then hides it", async () => {
    const optional = 3;
    const sent = await renderLibrary(
      route("PATCH", `/api/tips/${SHOES.id}`, () =>
        ok(patched(SHOES, { bucket: optional, list_ids: [] }, { removed_placements: SHOES.list_ids })),
      ),
    );
    setFilter("Bucket", String(limits.BUCKET_DAILY));
    await waitFor(() => expect(tipsUrls(sent).at(-1)).toBe("/api/tips?bucket=1"));
    await screen.findByRole("listitem", { name: label(SHOES) });

    const move = within(card(SHOES)).getByLabelText("Move to") as HTMLSelectElement;
    const offered = [...move.options].map((option) => option.value);
    expect(offered).toEqual(["", "2", "3", "4"]);
    fireEvent.change(move, { target: { value: String(optional) } });
    fireEvent.click(within(card(SHOES)).getByRole("button", { name: "Move" }));

    await waitFor(() => expect(screen.queryByRole("listitem", { name: label(SHOES) })).toBeNull());
    expect(writes(sent)).toEqual([{ method: "PATCH", url: `/api/tips/${SHOES.id}`, body: { bucket: optional } }]);
    expect(status()).toBe(
      `Moved “${SHOES.name}” to ${bucketName(optional)}. Taken off ${SHOES.list_ids.map(listTitle).join(", ")}: ` +
        "it no longer fits there. The current filters hide it.",
    );
  });

  it("edits a tip inline and shows the saved version", async () => {
    const phone = tipNamed("Phone Sleeps Outside");
    const renamed = "Phone Sleeps In The Hall";
    const sent = await renderLibrary(
      route("PATCH", `/api/tips/${phone.id}`, () => ok(patched(phone, { name: renamed }))),
    );

    fireEvent.click(within(card(phone)).getByRole("button", { name: "Edit" }));
    expect(within(card(phone)).queryByLabelText("Move to")).toBeNull();
    fireEvent.change(within(card(phone)).getByLabelText("Name"), { target: { value: renamed } });
    fireEvent.click(within(card(phone)).getByRole("button", { name: "Save" }));

    const saved = await screen.findByRole("listitem", { name: `Tip #${phone.id} ${renamed}` });
    expect(within(saved).getByText(renamed)).toBeTruthy();
    expect(writes(sent)).toEqual([{ method: "PATCH", url: `/api/tips/${phone.id}`, body: { name: renamed } }]);
    expect(status()).toBe(`Saved “${renamed}”.`);
  });

  it("deletes a tip only after confirming, then shows the note on the tip that repeated it", async () => {
    const sent = await renderLibrary();

    fireEvent.click(within(card(ORIGINAL)).getByRole("button", { name: "Delete" }));
    const confirm = within(card(ORIGINAL)).getByRole("group", { name: `Delete #${ORIGINAL.id}` });
    expect(confirm.textContent).toContain("This cannot be undone");
    fireEvent.click(within(confirm).getByRole("button", { name: "Keep it" }));
    expect(writes(sent)).toEqual([]);

    fireEvent.click(within(card(ORIGINAL)).getByRole("button", { name: "Delete" }));
    fireEvent.click(within(card(ORIGINAL)).getByRole("button", { name: "Delete for good" }));

    await waitFor(() => expect(screen.queryByRole("listitem", { name: label(ORIGINAL) })).toBeNull());
    expect(writes(sent)).toEqual([{ method: "DELETE", url: `/api/tips/${ORIGINAL.id}`, body: undefined }]);
    expect(status()).toBe(`Deleted #${ORIGINAL.id} “${ORIGINAL.name}”.`);
    // The page reloads the tips, so the duplicate's note comes from the server.
    await waitFor(() => expect(tipsUrls(sent)).toEqual(["/api/tips", "/api/tips"]));

    setFilter("Bucket", String(limits.BUCKET_LEFT_OUT));
    await waitFor(() => expect(shownLabels()).toContain(label(DUPLICATE)));
    const marker = `(original #${ORIGINAL.id} "${ORIGINAL.name}" deleted)`;
    expect(within(card(DUPLICATE)).getByText(`Note: ${DUPLICATE.note ?? ""} ${marker}`)).toBeTruthy();
    expect(within(card(DUPLICATE)).queryByText(/^Duplicate of/)).toBeNull();
  });

  it("shows an alert when the tips cannot load", async () => {
    stubApi(
      (request) =>
        request.url === "/api/tips"
          ? { status: 503, body: { error: { code: "CC_E_VALIDATION", message: "database is busy", fields: [] } } }
          : undefined,
      ...libraryRoutes(),
    );
    render(<LibraryPage meta={META} />);
    expect((await screen.findByRole("alert")).textContent).toBe("Cannot load the tips: database is busy");
  });
});

describe("tipsPath and shownUnder", () => {
  it("puts each chosen filter in the query, encoding kids+", () => {
    expect(tipsPath(NO_FILTERS)).toBe("/tips");
    expect(tipsPath({ bucket: 1, status: "approved", audience: "kids+", sourceId: 3 })).toBe(
      "/tips?status=approved&bucket=1&audience=kids%2B&source_id=3",
    );
  });

  it("hides Left out tips unless the Left out bucket is chosen", () => {
    expect(shownUnder(DUPLICATE, NO_FILTERS, META)).toBe(false);
    expect(shownUnder(DUPLICATE, { ...NO_FILTERS, bucket: limits.BUCKET_LEFT_OUT }, META)).toBe(true);
    expect(shownUnder(SHOES, NO_FILTERS, META)).toBe(true);
    expect(shownUnder(SHOES, { ...NO_FILTERS, audience: limits.AUDIENCE_ADULTS }, META)).toBe(false);
  });
});

describe("the Library in the app", () => {
  beforeEach(() => {
    forgetMeta();
    window.location.hash = "#/library";
  });

  it("opens at #/library with the app's settings, ready to add a tip", async () => {
    stubApi(...libraryRoutes());
    render(<App />);

    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Library");
    expect(await screen.findByRole("button", { name: "Add my own tip" })).toBeTruthy();
    expect(await screen.findByRole("list", { name: "Tips" })).toBeTruthy();
    expect(within(filters()).getByLabelText("Bucket")).toBeTruthy();
  });
});
