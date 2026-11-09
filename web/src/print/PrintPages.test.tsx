// The two print sheets rendered with the API stubbed. jsdom has no layout, so the row
// measurement always finds the rows fitting here; web/e2e/print.spec.ts measures in Chromium.
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ActivitiesSheet, LocationSheet } from "../model";
import { get, META, ok, stubApi } from "../testing/api";
import { ACTIVITIES_TITLE, PrintActivitiesPage } from "./PrintActivitiesPage";
import { PrintListPage, spillMessage } from "./PrintListPage";
import { PRINT_HINT } from "./PrintSheet";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const SEP = META.limits.LINE_SEPARATOR;

function sheet(rows: number, overCapacity = false): LocationSheet {
  return {
    title: "Coffee Machine — Adults",
    audience: "adults",
    generated_at: "2026-11-08T12:00:00Z",
    items: Array.from({ length: rows }, (_, index) => ({ name: `Tip ${index + 1}`, action: `Do thing ${index + 1}` })),
    over_capacity: overCapacity,
  };
}

async function renderSheet(payload: LocationSheet) {
  stubApi(get("/api/print/lists/7", ok(payload)));
  render(<PrintListPage listId={7} meta={META} />);
  return screen.findByRole("article", { name: payload.title });
}

describe("PrintListPage", () => {
  it("prints the title, a date line and one checkbox row per tip, in order", async () => {
    const article = await renderSheet(sheet(3));

    expect(within(article).getByRole("heading", { level: 1 }).textContent).toBe("Coffee Machine — Adults");
    expect(within(article).getByText(/^Printed /)).toBeTruthy();
    const rows = within(article).getAllByRole("listitem");
    expect(rows.map((row) => row.textContent)).toEqual([1, 2, 3].map((n) => `Tip ${n}${SEP}Do thing ${n}`));
    for (const row of rows) {
      expect(row.querySelector(".print-box")).not.toBeNull();
      expect(row.querySelector("b")?.textContent).toMatch(/^Tip \d$/);
      expect(row.getAttribute("data-overflow")).toBe("false");
    }
    await waitFor(() => expect(article.getAttribute("data-measured")).toBe("true"));
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("offers a Print button with the A4 hint", async () => {
    await renderSheet(sheet(1));
    const print = vi.fn();
    vi.stubGlobal("print", print);

    fireEvent.click(screen.getByRole("button", { name: "Print" }));

    expect(print).toHaveBeenCalledTimes(1);
    expect(screen.getByText(PRINT_HINT)).toBeTruthy();
    expect(PRINT_HINT).toBe("Print on A4 at 100% scale, with browser headers and footers turned off.");
    expect(screen.getByRole("link", { name: "← Back to Lists" }).getAttribute("href")).toBe("#/lists");
  });

  it("warns that an over-capacity sheet spills onto a second page", async () => {
    const rows = META.print_capacity + 1;
    await renderSheet(sheet(rows, true));

    const alert = screen.getByRole("alert");
    expect(alert.textContent).toBe(spillMessage(rows, META.print_capacity));
    expect(alert.textContent).toContain("spills onto a second page");
  });

  it("does not warn for a sheet that fits", async () => {
    await renderSheet(sheet(META.print_capacity, false));
    expect(screen.queryByText(/spills onto a second page/)).toBeNull();
  });

  it("says when nothing prints yet", async () => {
    await renderSheet(sheet(0));
    expect(screen.getByText(/Nothing to print yet/)).toBeTruthy();
  });

  it("shows the server's message for an unknown list", async () => {
    const message = "there is no list #99";
    stubApi(
      get("/api/print/lists/99", {
        status: 404,
        body: { error: { code: "CC_E_NOT_FOUND", message, fields: [] } },
      }),
    );
    render(<PrintListPage listId={99} meta={META} />);
    expect((await screen.findByRole("alert")).textContent).toBe(`Cannot load the sheet of list #99: ${message}`);
  });
});

describe("PrintActivitiesPage", () => {
  const activities: ActivitiesSheet = {
    generated_at: "2026-11-08T12:00:00Z",
    groups: [
      { audience: "kids+", items: [{ name: "Plan A Family Walk", action: "Pick one evening for a walk" }] },
      {
        audience: "adults",
        items: [
          { name: "Call An Old Friend", action: "Phone a friend" },
          { name: "Weekly Money Check", action: "Look over the spending" },
        ],
      },
    ],
  };

  it("shows the Kids+ and Adults groups, one line per activity and no checkboxes", async () => {
    stubApi(get("/api/print/activities", ok(activities)));
    render(<PrintActivitiesPage meta={META} />);
    const article = await screen.findByRole("article", { name: ACTIVITIES_TITLE });

    expect(within(article).getByRole("heading", { level: 1 }).textContent).toBe("Good to do now and then");
    const kids = within(article).getByRole("region", { name: "Kids+" });
    const adults = within(article).getByRole("region", { name: "Adults" });
    expect(within(kids).getAllByRole("listitem").map((row) => row.textContent)).toEqual([
      `Plan A Family Walk${SEP}Pick one evening for a walk`,
    ]);
    expect(within(adults).getAllByRole("listitem")).toHaveLength(2);
    expect(article.querySelector(".print-box")).toBeNull();
    // Rows are numbered across both groups.
    expect([...article.querySelectorAll(".print-text")].map((text) => text.getAttribute("data-row"))).toEqual([
      "0",
      "1",
      "2",
    ]);
  });
});

describe("row overflow measurement", () => {
  // jsdom has no layout: give each .print-text a 698.140625px line (Chromium's width of the
  // printed line next to a checkbox) and its contents the width `contentWidth` returns.
  const LINE = 698.140625;
  let contentWidth: (text: string) => number = () => 0;

  function stubLayout() {
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
      const width = this.classList.contains("print-text") ? LINE : 0;
      return DOMRect.fromRect({ width, height: 20 });
    });
    Object.defineProperty(Range.prototype, "getBoundingClientRect", {
      configurable: true,
      value: function (this: Range) {
        return DOMRect.fromRect({ width: contentWidth(this.startContainer.textContent ?? ""), height: 20 });
      },
    });
  }

  afterEach(() => {
    Reflect.deleteProperty(Range.prototype, "getBoundingClientRect");
    Reflect.deleteProperty(document, "fonts");
    contentWidth = () => 0;
  });

  it("flags a row that runs past its line by a fraction of a pixel, and not one that fits exactly", async () => {
    stubLayout();
    contentWidth = (text) => (text.startsWith("Tip 2") ? LINE + 0.046875 : LINE);
    const article = await renderSheet(sheet(2));

    await waitFor(() => expect(article.getAttribute("data-measured")).toBe("true"));
    const rows = within(article).getAllByRole("listitem");
    expect(rows.map((row) => row.getAttribute("data-overflow"))).toEqual(["false", "true"]);
    expect(within(screen.getByRole("alert")).getAllByRole("listitem").map((item) => item.textContent)).toEqual([
      "Tip 2",
    ]);
  });

  it("counts the rows as measured only after the fonts are ready, with the widths they give", async () => {
    stubLayout();
    let fontsReady: () => void = () => undefined;
    const ready = new Promise<void>((resolve) => {
      fontsReady = resolve;
    });
    Object.defineProperty(document, "fonts", { configurable: true, value: { ready, status: "loading" } });
    const article = await renderSheet(sheet(1));

    // Measured before the fonts load: it fits, but that is not the final answer yet.
    expect(article.getAttribute("data-measured")).toBe("false");
    expect(within(article).getByRole("listitem").getAttribute("data-overflow")).toBe("false");

    // The loaded font is wider, so the measurement after `fonts.ready` finds the row cut.
    contentWidth = () => LINE + 0.5;
    fontsReady();

    await waitFor(() => expect(article.getAttribute("data-measured")).toBe("true"));
    expect(within(article).getByRole("listitem").getAttribute("data-overflow")).toBe("true");
  });
});
