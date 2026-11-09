// The review inbox, rendered from the demo server's own payloads (fixtures/demoReview.json, kept
// equal to `serve --demo` by tests/integration/test_api_review.py), with the API stubbed.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { PatchResult, Tip } from "../model";
import type { SentRequest, StubRoute } from "../testing/api";
import { DEMO_LISTS, DEMO_SOURCES, DEMO_TIPS, demoRoutes, META, ok, stubApi } from "../testing/api";
import { groupBySource, ReviewPage } from "./ReviewPage";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function tipNamed(name: string): Tip {
  const tip = DEMO_TIPS.find((item) => item.name === name);
  if (tip === undefined) throw new Error(`the demo fixture has no tip named ${name}`);
  return tip;
}

function card(tip: Tip): HTMLElement {
  return screen.getByRole("listitem", { name: `Tip #${tip.id} ${tip.name}` });
}

function queryCard(tip: Tip): HTMLElement | null {
  return screen.queryByRole("listitem", { name: `Tip #${tip.id} ${tip.name}` });
}

function route(method: string, url: string, answer: (request: SentRequest) => unknown): StubRoute {
  return (request) => (request.method === method && request.url === url ? ok(answer(request)) : undefined);
}

function patched(tip: Tip, change: Partial<Tip>, extra: Partial<PatchResult> = {}): PatchResult {
  return { tip: { ...tip, ...change }, removed_placements: [], cleared_duplicate_of: null, ...extra };
}

async function renderInbox(...routes: StubRoute[]): Promise<SentRequest[]> {
  const sent = stubApi(...routes, ...demoRoutes());
  render(<ReviewPage meta={META} />);
  await screen.findByText(/tips waiting from/);
  return sent;
}

function writes(sent: readonly SentRequest[]): { method: string; url: string; body: unknown }[] {
  return sent.filter((request) => request.method !== "GET");
}

const SHOES = tipNamed("Shoes By The Door");
const DUPLICATE = DEMO_TIPS.find((tip) => tip.duplicate_of !== null);
const NEAR = DEMO_TIPS.find((tip) => tip.possible_duplicate_of !== null);

describe("ReviewPage on the demo fixture", () => {
  it("groups the pending tips by source, with the duplicate badges and timestamp links", async () => {
    await renderInbox();

    const groups = groupBySource(DEMO_TIPS, DEMO_SOURCES);
    expect(groups.length).toBeGreaterThanOrEqual(2);
    const headings = screen.getAllByRole("heading", { level: 2 }).map((heading) => heading.textContent);
    expect(headings).toEqual(groups.map((group) => group.source.title));
    for (const group of groups) {
      const region = screen.getByRole("region", { name: group.source.title ?? "" });
      const names = within(region)
        .getAllByRole("listitem")
        .map((item) => item.getAttribute("aria-label"));
      expect(names).toEqual(group.tips.map((tip) => `Tip #${tip.id} ${tip.name}`));
    }
    expect(screen.getByText(`${DEMO_TIPS.length} tips waiting from ${groups.length} sources.`)).toBeTruthy();

    // The duplicate badge names the original and opens it.
    expect(DUPLICATE?.duplicate_of_tip).not.toBeNull();
    if (DUPLICATE === undefined || DUPLICATE.duplicate_of_tip === null) throw new Error("no duplicate");
    const original = DUPLICATE.duplicate_of_tip;
    const badge = within(card(DUPLICATE)).getByRole("button", {
      name: `Duplicate of #${original.id} · ${original.name}`,
    });
    expect(badge.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(badge);
    expect(badge.getAttribute("aria-expanded")).toBe("true");
    expect(within(card(DUPLICATE)).getByText(original.action, { exact: false })).toBeTruthy();

    // The possible duplicate keeps its own bucket and shows its near match.
    if (NEAR === undefined || NEAR.possible_duplicate_of_tip === null) throw new Error("no near match");
    const near = NEAR.possible_duplicate_of_tip;
    expect(
      within(card(NEAR)).getByRole("button", { name: `Possible duplicate of #${near.id} · ${near.name}` }),
    ).toBeTruthy();

    // YouTube tips link to their moment in the video.
    const watch = within(card(SHOES)).getByRole("link", { name: "Watch from 0:30 on YouTube" });
    expect(watch.getAttribute("href")).toBe(SHOES.start_url);
    expect(SHOES.start_url).toMatch(/^https:\/\/www\.youtube\.com\/watch\?v=[A-Za-z0-9_-]{11}&t=30s$/);

    // Lists and buckets are shown by name.
    const listTitles = SHOES.list_ids.map((id) => DEMO_LISTS.find((list) => list.id === id)?.title);
    expect(within(card(SHOES)).getByText(`On ${listTitles.join(", ")}`)).toBeTruthy();
    expect(within(card(SHOES)).getByText("Daily checklist")).toBeTruthy();
  });

  it("removes a tip after edit then Approve, saving the edit first", async () => {
    const renamed = "Shoes Lined Up By The Door";
    const sent = await renderInbox(
      route("PATCH", `/api/tips/${SHOES.id}`, () => patched(SHOES, { name: renamed })),
      route("POST", "/api/review/approve", () => ({ approved: 1 })),
    );

    fireEvent.click(within(card(SHOES)).getByRole("button", { name: "Edit" }));
    fireEvent.change(within(card(SHOES)).getByLabelText("Name"), { target: { value: renamed } });
    fireEvent.click(within(card(SHOES)).getByRole("button", { name: "Approve" }));

    await waitFor(() => expect(queryCard(SHOES)).toBeNull());
    expect(writes(sent)).toEqual([
      { method: "PATCH", url: `/api/tips/${SHOES.id}`, body: { name: renamed } },
      { method: "POST", url: "/api/review/approve", body: { tip_ids: [SHOES.id] } },
    ]);
    expect(screen.getByRole("status").textContent).toBe(`Approved “${renamed}”.`);
    expect(screen.getByText(`${DEMO_TIPS.length - 1} tips waiting`, { exact: false })).toBeTruthy();
  });

  it("removes a tip marked Left out", async () => {
    const router = tipNamed("Read The Router Manual");
    const sent = await renderInbox(route("POST", "/api/review/left-out", () => ({ left_out: 1 })));

    fireEvent.click(within(card(router)).getByRole("button", { name: "Left out" }));

    await waitFor(() => expect(queryCard(router)).toBeNull());
    expect(writes(sent)).toEqual([
      { method: "POST", url: "/api/review/left-out", body: { tip_ids: [router.id] } },
    ]);
    expect(screen.getByRole("status").textContent).toBe(`Left out “${router.name}”.`);
    expect(card(SHOES)).toBeTruthy();
  });

  it("removes an edited tip marked Left out, saving the edit first", async () => {
    const cables = tipNamed("Tidy The Desk Cables");
    const sent = await renderInbox(
      route("PATCH", `/api/tips/${cables.id}`, () => patched(cables, { action: "Bundle the desk cables" })),
      route("POST", "/api/review/left-out", () => ({ left_out: 1 })),
    );

    fireEvent.click(within(card(cables)).getByRole("button", { name: "Edit" }));
    fireEvent.change(within(card(cables)).getByLabelText("What to do"), {
      target: { value: "Bundle the desk cables" },
    });
    fireEvent.click(within(card(cables)).getByRole("button", { name: "Left out" }));

    await waitFor(() => expect(queryCard(cables)).toBeNull());
    expect(writes(sent).map((request) => `${request.method} ${request.url}`)).toEqual([
      `PATCH /api/tips/${cables.id}`,
      "POST /api/review/left-out",
    ]);
  });

  it("approves every tip of one source", async () => {
    const groups = groupBySource(DEMO_TIPS, DEMO_SOURCES);
    const last = groups[groups.length - 1];
    if (last === undefined) throw new Error("no groups");
    const sent = await renderInbox(route("POST", "/api/review/approve", () => ({ approved: last.tips.length })));
    const region = screen.getByRole("region", { name: last.source.title ?? "" });

    fireEvent.click(within(region).getByRole("button", { name: "Approve all for this source" }));

    await waitFor(() => expect(screen.queryByRole("region", { name: last.source.title ?? "" })).toBeNull());
    expect(writes(sent)).toEqual([
      { method: "POST", url: "/api/review/approve", body: { tip_ids: last.tips.map((tip) => tip.id) } },
    ]);
    expect(card(SHOES)).toBeTruthy();
  });

  it("disables Approve all while a tip of that source is being edited", async () => {
    await renderInbox();
    const region = screen.getByRole("region", { name: DEMO_SOURCES.find((s) => s.id === SHOES.source_id)?.title ?? "" });
    const approveAll = within(region).getByRole("button", { name: "Approve all for this source" }) as HTMLButtonElement;
    expect(approveAll.disabled).toBe(false);

    fireEvent.click(within(card(SHOES)).getByRole("button", { name: "Edit" }));
    expect(approveAll.disabled).toBe(true);
    fireEvent.click(within(card(SHOES)).getByRole("button", { name: "Cancel" }));
    expect(approveAll.disabled).toBe(false);
  });

  it("reports the placements a bucket change removed", async () => {
    const sent = await renderInbox(
      route("PATCH", `/api/tips/${SHOES.id}`, () =>
        patched(SHOES, { bucket: META.limits.BUCKET_IDS[1] ?? 2, list_ids: [] }, { removed_placements: SHOES.list_ids }),
      ),
    );

    fireEvent.click(within(card(SHOES)).getByRole("button", { name: "Edit" }));
    fireEvent.change(within(card(SHOES)).getByLabelText("Bucket"), { target: { value: "2" } });
    expect(within(card(SHOES)).getByText("Only daily-checklist tips go on lists.")).toBeTruthy();
    fireEvent.click(within(card(SHOES)).getByRole("button", { name: "Save" }));

    const titles = SHOES.list_ids.map((id) => DEMO_LISTS.find((list) => list.id === id)?.title);
    expect(await within(card(SHOES)).findByText(`Taken off ${titles.join(", ")}: it no longer fits there.`)).toBeTruthy();
    // No list_ids: the server drops the placements the bucket change invalidates and says which.
    expect(writes(sent)).toEqual([{ method: "PATCH", url: `/api/tips/${SHOES.id}`, body: { bucket: 2 } }]);
    expect(within(card(SHOES)).getByText("Occasional activity")).toBeTruthy();
  });

  it("reports a cleared duplicate after the duplicate moves to bucket 1", async () => {
    if (DUPLICATE === undefined || DUPLICATE.duplicate_of === null) throw new Error("no duplicate");
    const originalId = DUPLICATE.duplicate_of;
    await renderInbox(
      route("PATCH", `/api/tips/${DUPLICATE.id}`, () =>
        patched(
          DUPLICATE,
          { bucket: 1, duplicate_of: null, duplicate_of_tip: null },
          { cleared_duplicate_of: originalId },
        ),
      ),
    );

    fireEvent.click(within(card(DUPLICATE)).getByRole("button", { name: "Edit" }));
    fireEvent.change(within(card(DUPLICATE)).getByLabelText("Bucket"), { target: { value: "1" } });
    fireEvent.click(within(card(DUPLICATE)).getByRole("button", { name: "Save" }));

    expect(await within(card(DUPLICATE)).findByText(`No longer marked as a duplicate of #${originalId}.`)).toBeTruthy();
    expect(within(card(DUPLICATE)).queryByRole("button", { name: /^Duplicate of/ })).toBeNull();
  });

  it("offers only the lists a tip may go on, and counts the printed line", async () => {
    await renderInbox();
    const adultsTip = tipNamed("Phone Sleeps Outside");
    fireEvent.click(within(card(adultsTip)).getByRole("button", { name: "Edit" }));
    const editor = card(adultsTip);

    const offered = within(editor)
      .getAllByRole("checkbox")
      .map((box) => box.parentElement?.textContent);
    const adultsLists = DEMO_LISTS.filter((list) => list.audience !== META.limits.AUDIENCE_KIDS).map((list) => list.title);
    expect(offered).toEqual(adultsLists);

    const { LINE_MAX, LINE_SEPARATOR } = META.limits;
    const length = adultsTip.name.length + LINE_SEPARATOR.length + adultsTip.action.length;
    expect(within(editor).getByText(`Printed line: ${length} / ${LINE_MAX} characters`)).toBeTruthy();
    fireEvent.change(within(editor).getByLabelText("What to do"), { target: { value: "x".repeat(META.limits.ACTION_MAX) } });
    expect(within(editor).getByText(/too long for one line/)).toBeTruthy();
    expect((within(editor).getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(true);
    expect((within(editor).getByRole("button", { name: "Approve" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("keeps the tip and shows the error when the server refuses", async () => {
    await renderInbox((request) =>
      request.method === "POST" && request.url === "/api/review/approve"
        ? { status: 404, body: { error: { code: "CC_E_NOT_FOUND", message: "there is no tip #5", fields: [] } } }
        : undefined,
    );

    await act(async () => {
      fireEvent.click(within(card(SHOES)).getByRole("button", { name: "Approve" }));
    });

    expect((await within(card(SHOES)).findByRole("alert")).textContent).toContain("there is no tip #5");
  });

  it("says so when nothing is pending", async () => {
    stubApi(...demoRoutes([]));
    render(<ReviewPage meta={META} />);
    expect(await screen.findByText(/Nothing to review/)).toBeTruthy();
  });

  it("shows an alert when the inbox cannot load", async () => {
    stubApi((request) =>
      request.url === "/api/tips?status=pending"
        ? { status: 503, body: { error: { code: "CC_E_VALIDATION", message: "database is busy", fields: [] } } }
        : undefined,
      ...demoRoutes(),
    );
    render(<ReviewPage meta={META} />);
    expect((await screen.findByRole("alert")).textContent).toContain("database is busy");
  });
});
