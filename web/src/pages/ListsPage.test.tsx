// The read-only Lists index, rendered from the demo server's own payloads
// (fixtures/demoReview.json, kept equal to `serve --demo` by tests/integration/test_api_review.py),
// with the API stubbed.
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ListInfo } from "../model";
import { DEMO_LIST_ITEMS, DEMO_LISTS, DEMO_UNPLACED, demoRoutes, META, stubApi } from "../testing/api";
import { ListsPage, printedItems } from "./ListsPage";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function listTitled(title: string): ListInfo {
  const list = DEMO_LISTS.find((item) => item.title === title);
  if (list === undefined) throw new Error(`the demo fixture has no list titled ${title}`);
  return list;
}

async function renderIndex() {
  const sent = stubApi(...demoRoutes());
  render(<ListsPage meta={META} />);
  await screen.findByRole("list", { name: "Lists" });
  return sent;
}

function card(title: string): HTMLElement {
  return within(screen.getByRole("list", { name: "Lists" })).getByRole("listitem", { name: title });
}

describe("ListsPage on the demo fixture", () => {
  it("links the Coffee Machine — Adults entry to #/print/list/7", async () => {
    await renderIndex();

    const coffee = card("Coffee Machine — Adults");
    const print = within(coffee).getByRole("link", { name: "Print Coffee Machine — Adults" });
    expect(print.getAttribute("href")).toBe("#/print/list/7");
    expect(listTitled("Coffee Machine — Adults").id).toBe(7);
  });

  it("shows every list in order, each with a Print link to its own sheet", async () => {
    const sent = await renderIndex();

    const cards = within(screen.getByRole("list", { name: "Lists" }))
      .getAllByRole("listitem")
      .filter((item) => item.classList.contains("list-card"));
    expect(cards.map((item) => item.getAttribute("aria-label"))).toEqual(DEMO_LISTS.map((list) => list.title));
    for (const list of DEMO_LISTS) {
      const link = within(card(list.title)).getByRole("link", { name: `Print ${list.title}` });
      expect(link.getAttribute("href")).toBe(`#/print/list/${list.id}`);
    }
    // One read of each list's items; nothing is written.
    const urls = sent.map((request) => request.url);
    for (const list of DEMO_LISTS) expect(urls).toContain(`/api/lists/${list.id}/items`);
    expect(sent.every((request) => request.method === "GET")).toBe(true);
  });

  it("counts the tips that print and lists them in print order", async () => {
    await renderIndex();
    const coffee = listTitled("Coffee Machine — Adults");
    const items = DEMO_LIST_ITEMS[String(coffee.id)] ?? [];
    const printed = printedItems(items, META);
    expect(printed).toHaveLength(6);

    const region = card(coffee.title);
    expect(within(region).getByText("6 tips print.")).toBeTruthy();
    expect(within(region).getByText(`When: ${coffee.cue}`)).toBeTruthy();
    const shown = within(region)
      .getAllByRole("listitem")
      .map((item) => item.textContent);
    const sep = META.limits.LINE_SEPARATOR;
    const byPosition = [...items].sort((a, b) => a.position - b.position);
    expect(shown).toEqual(byPosition.map((item) => `${item.name}${sep}${item.action}`));

    // A list with one approved tip and one pending: only the approved one counts and prints.
    const bathroom = listTitled("Adult Bathroom — Adults");
    const mixed = DEMO_LIST_ITEMS[String(bathroom.id)] ?? [];
    expect(mixed.map((item) => [item.name, item.status])).toEqual([
      ["Brush Your Teeth", META.limits.TIP_STATUS_APPROVED],
      ["Phone Sleeps Outside", "pending"],
    ]);
    expect(printedItems(mixed, META).map((item) => item.name)).toEqual(["Brush Your Teeth"]);

    const mixedCard = card(bathroom.title);
    expect(within(mixedCard).getByText("1 tip prints; 1 more waiting for review.")).toBeTruthy();
    const badges = mixedCard.querySelectorAll(".list-pending");
    expect(badges).toHaveLength(1);
    expect(badges[0]?.closest("li")?.querySelector("b")?.textContent).toBe("Phone Sleeps Outside");
  });

  it("warns only on the list that spills onto a second page", async () => {
    await renderIndex();
    const spills = listTitled("Capacity Spills — Adults");
    const fits = listTitled("Capacity Fits — Adults");
    expect(spills.over_capacity).toBe(true);
    expect(fits.over_capacity).toBe(false);

    expect(within(card(spills.title)).getByRole("note").textContent).toBe(
      `More than ${META.print_capacity} tips: the sheet spills onto a second page.`,
    );
    expect(within(card(fits.title)).queryByRole("note")).toBeNull();
    expect(within(card(fits.title)).getByText(`${META.print_capacity} tips print.`)).toBeTruthy();
  });

  it("shows the approved daily tips that are on no list", async () => {
    await renderIndex();
    expect(DEMO_UNPLACED.length).toBeGreaterThan(0);
    for (const tip of DEMO_UNPLACED) {
      expect(tip.status).toBe(META.limits.TIP_STATUS_APPROVED);
      expect(tip.bucket).toBe(META.limits.BUCKET_DAILY);
      expect(tip.list_ids).toEqual([]);
    }

    const panel = screen.getByRole("region", { name: "Not on any list yet" });
    const names = within(panel)
      .getAllByRole("listitem")
      .map((item) => item.querySelector("b")?.textContent);
    expect(names).toEqual(DEMO_UNPLACED.map((tip) => tip.name));
  });

  it("links to the activities sheet", async () => {
    await renderIndex();
    expect(screen.getByRole("link", { name: "Print the activities sheet" }).getAttribute("href")).toBe(
      "#/print/activities",
    );
  });

  it("says so when every approved daily tip is placed", async () => {
    stubApi((request) => (request.url === "/api/tips?unplaced=true" ? { status: 200, body: [] } : undefined), ...demoRoutes());
    render(<ListsPage meta={META} />);
    const panel = await screen.findByRole("region", { name: "Not on any list yet" });
    expect(within(panel).getByText("Every approved daily tip is on a list.")).toBeTruthy();
  });

  it("shows an alert when the lists cannot load", async () => {
    stubApi((request) =>
      request.url === "/api/lists"
        ? { status: 503, body: { error: { code: "CC_E_VALIDATION", message: "database is busy", fields: [] } } }
        : undefined,
    );
    render(<ListsPage meta={META} />);
    expect((await screen.findByRole("alert")).textContent).toContain("database is busy");
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Lists");
  });
});
