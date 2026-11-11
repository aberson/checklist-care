// The Lists page, rendered from the demo server's own payloads (fixtures/demoReview.json, kept
// equal to `serve --demo` by tests/integration/test_api_review.py). The API is a small stand-in
// for the server over its own copy of the demo lists, items and daily tips: it stores what the
// page sends and answers the reads from that, refusing a tip a kids list may not hold with the
// server's 409, as the real routes do (tests/integration/test_api_lists.py checks the real ones,
// through to the print sheets and `checklist-care context`).
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ListInfo, ListItem, ListPatch, NewList, Tip } from "../model";
import { compatible } from "../model";
import type { SentRequest, StubAnswer, StubRoute } from "../testing/api";
import { DEMO_DAILY, DEMO_LIST_ITEMS, DEMO_LISTS, DEMO_UNPLACED, demoRoutes, META, ok, stubApi } from "../testing/api";
import {
  addChoices,
  kidsRule,
  listDraftProblem,
  ListsPage,
  moved,
  orderPatches,
  overlongMessage,
  printedItems,
} from "./ListsPage";

const { limits } = META;
const SEP = limits.LINE_SEPARATOR;

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function required<T>(value: T | undefined, what: string): T {
  if (value === undefined) throw new Error(`the demo fixture has no ${what}`);
  return value;
}

function listTitled(title: string): ListInfo {
  return required(
    DEMO_LISTS.find((item) => item.title === title),
    `list titled ${title}`,
  );
}

function demoItems(list: ListInfo): ListItem[] {
  return DEMO_LIST_ITEMS[String(list.id)] ?? [];
}

const COFFEE = listTitled("Coffee Machine — Adults");
const BATHROOM = listTitled("Adult Bathroom — Adults");
const OFFICE = listTitled("My Office — Adults");
const KIDS_BATHROOM = listTitled("Kids Bathroom — Kids");
const SPILLS = listTitled("Capacity Spills — Adults");
const FITS = listTitled("Capacity Fits — Adults");

function apiError(status: number, code: string, message: string, fields: string[] = []): StubAnswer {
  return { status, body: { error: { code, message, fields } } };
}

/** What the stand-in server does for the Lists page's routes, over its own copy of the demo. */
function listsServer(): StubRoute[] {
  let lists: ListInfo[] = DEMO_LISTS.map((list) => ({ ...list }));
  const items = new Map<number, ListItem[]>(lists.map((list) => [list.id, demoItems(list).map((item) => ({ ...item }))]));
  const tips = DEMO_DAILY.map((tip) => ({ ...tip }));

  const title = (place: string, audience: string) => `${place}${SEP}${audience.charAt(0).toUpperCase()}${audience.slice(1)}`;
  const onLists = (tipId: number) =>
    lists.filter((list) => (items.get(list.id) ?? []).some((item) => item.tip_id === tipId)).map((list) => list.id);
  const daily = (): Tip[] => tips.map((tip) => ({ ...tip, list_ids: onLists(tip.id).sort((a, b) => a - b) }));
  const shown = (list: ListInfo): ListInfo => {
    const held = items.get(list.id) ?? [];
    return {
      ...list,
      item_count: held.length,
      over_capacity: printedItems(held, META).length > META.print_capacity,
    };
  };
  const ordered = () => [...lists].sort((a, b) => a.sort_order - b.sort_order || a.id - b.id).map(shown);
  const store = (listId: number, tipIds: readonly number[]) =>
    items.set(
      listId,
      tipIds.map((tipId, index) => {
        const tip = required(
          tips.find((item) => item.id === tipId),
          `tip #${tipId}`,
        );
        const { name, action, status, bucket, audience } = tip;
        return { tip_id: tipId, position: index + 1, name, action, status, bucket, audience };
      }),
    );
  const listId = (request: SentRequest, pattern: RegExp) => {
    const match = pattern.exec(request.url);
    return match === null ? null : Number(match[1]);
  };

  return [
    (request) => (request.method === "GET" && request.url === "/api/lists" ? ok(ordered()) : undefined),
    (request) =>
      request.method === "GET" && request.url === "/api/tips?unplaced=true"
        ? ok(daily().filter((tip) => tip.status === limits.TIP_STATUS_APPROVED && tip.list_ids.length === 0))
        : undefined,
    (request) =>
      request.method === "GET" && request.url === `/api/tips?bucket=${limits.BUCKET_DAILY}` ? ok(daily()) : undefined,
    (request) => {
      const id = listId(request, /^\/api\/lists\/(\d+)\/items$/);
      if (id === null) return undefined;
      const list = lists.find((item) => item.id === id);
      if (list === undefined) return apiError(404, "CC_E_NOT_FOUND", `there is no list #${id}`);
      if (request.method === "GET") return ok(items.get(id) ?? []);
      if (request.method !== "PUT") return undefined;
      const tipIds = (request.body as { tip_ids: number[] }).tip_ids;
      const refused = tipIds
        .map((tipId) => tips.find((tip) => tip.id === tipId))
        .filter((tip) => tip !== undefined && !compatible(tip.bucket, tip.audience, list.audience, META));
      if (refused.length > 0) {
        const names = refused.map((tip) => `tip #${tip?.id} "${tip?.name}" is ${tip?.audience}`).join("; ");
        return apiError(409, "CC_E_CONFLICT", `${names}; nothing was changed`, ["tip_ids"]);
      }
      store(id, tipIds);
      return ok(items.get(id));
    },
    (request) => {
      if (request.method !== "POST" || request.url !== "/api/lists") return undefined;
      const body = request.body as NewList;
      const clash = lists.find((list) => list.place === body.place && list.audience === body.audience);
      if (clash !== undefined) {
        return apiError(400, "CC_E_VALIDATION", `there is already a list ${clash.title}`, ["place", "audience"]);
      }
      const id = Math.max(...lists.map((list) => list.id)) + 1;
      const sortOrder = Math.max(...lists.map((list) => list.sort_order)) + 1;
      const created: ListInfo = {
        id,
        place: body.place,
        audience: body.audience,
        title: title(body.place, body.audience),
        cue: body.cue,
        sort_order: sortOrder,
        item_count: 0,
        over_capacity: false,
      };
      lists = [...lists, created];
      items.set(id, []);
      return { status: 201, body: created };
    },
    (request) => {
      const id = listId(request, /^\/api\/lists\/(\d+)$/);
      if (id === null || (request.method !== "PATCH" && request.method !== "DELETE")) return undefined;
      const current = lists.find((list) => list.id === id);
      if (current === undefined) return apiError(404, "CC_E_NOT_FOUND", `there is no list #${id}`);
      if (request.method === "DELETE") {
        lists = lists.filter((list) => list.id !== id);
        items.delete(id);
        return { status: 204 };
      }
      const body = request.body as ListPatch;
      const next: ListInfo = { ...current, ...body };
      next.title = title(next.place, next.audience);
      const held = items.get(id) ?? [];
      const removed = held.filter((item) => !compatible(item.bucket, item.audience, next.audience, META));
      store(
        id,
        held.filter((item) => !removed.includes(item)).map((item) => item.tip_id),
      );
      lists = lists.map((list) => (list.id === id ? next : list));
      return ok({ list: shown(next), removed_tip_ids: removed.map((item) => item.tip_id) });
    },
  ];
}

async function renderPage(...routes: StubRoute[]): Promise<SentRequest[]> {
  const sent = stubApi(...routes, ...listsServer());
  render(<ListsPage meta={META} />);
  await screen.findByRole("list", { name: "Lists" });
  return sent;
}

function cards(): HTMLElement[] {
  return within(screen.getByRole("list", { name: "Lists" }))
    .getAllByRole("listitem")
    .filter((item) => item.classList.contains("list-card"));
}

function cardTitles(): (string | null)[] {
  return cards().map((item) => item.getAttribute("aria-label"));
}

function card(title: string): HTMLElement {
  return within(screen.getByRole("list", { name: "Lists" })).getByRole("listitem", { name: title });
}

/** The tips shown on a list, as `name — action` lines, in order. */
function shownTips(title: string): string[] {
  const rows = within(card(title)).queryByRole("list", { name: `Tips on ${title}` });
  if (rows === null) return [];
  return within(rows)
    .getAllByRole("listitem")
    .map((row) => row.querySelector(".list-item-text")?.textContent ?? "");
}

function line(item: { name: string; action: string }): string {
  return `${item.name}${SEP}${item.action}`;
}

function writes(sent: readonly SentRequest[]): SentRequest[] {
  return sent.filter((request) => request.method !== "GET");
}

function status(): string {
  return screen.getByRole("status").textContent ?? "";
}

async function announced(text: string): Promise<void> {
  await waitFor(() => expect(status()).toBe(text));
}

/** Tip names in tip id order, the order the server lists unplaced tips in. */
function byTipId(names: readonly string[]): string[] {
  const id = (name: string) => DEMO_DAILY.find((tip) => tip.name === name)?.id ?? 0;
  return [...names].sort((a, b) => id(a) - id(b));
}

function unplacedNames(): string[] {
  const panel = screen.getByRole("region", { name: "Not on any list yet" });
  const rows = within(panel).queryAllByRole("listitem");
  return rows.map((row) => row.querySelector("b")?.textContent ?? "");
}

describe("the stand-in server", () => {
  it("answers the reads exactly as the demo server did", async () => {
    stubApi(...listsServer());
    const read = async (url: string) => (await fetch(url)).json();
    expect(await read("/api/lists")).toEqual(DEMO_LISTS);
    expect(await read("/api/tips?unplaced=true")).toEqual(DEMO_UNPLACED);
    expect(await read(`/api/tips?bucket=${limits.BUCKET_DAILY}`)).toEqual(DEMO_DAILY);
    for (const list of DEMO_LISTS) expect(await read(`/api/lists/${list.id}/items`)).toEqual(demoItems(list));
  });
});

describe("ListsPage on the demo fixture", () => {
  it("links the Coffee Machine — Adults entry to #/print/list/7", async () => {
    await renderPage();

    const print = within(card(COFFEE.title)).getByRole("link", { name: "Print Coffee Machine — Adults" });
    expect(print.getAttribute("href")).toBe("#/print/list/7");
    expect(COFFEE.id).toBe(7);
  });

  it("shows every list in order, each with a Print link to its own sheet, and writes nothing", async () => {
    const sent = await renderPage();

    expect(cardTitles()).toEqual(DEMO_LISTS.map((list) => list.title));
    for (const list of DEMO_LISTS) {
      const link = within(card(list.title)).getByRole("link", { name: `Print ${list.title}` });
      expect(link.getAttribute("href")).toBe(`#/print/list/${list.id}`);
    }
    const urls = sent.map((request) => request.url);
    for (const list of DEMO_LISTS) expect(urls).toContain(`/api/lists/${list.id}/items`);
    expect(urls).toContain(`/api/tips?bucket=${limits.BUCKET_DAILY}`);
    expect(writes(sent)).toEqual([]);
  });

  it("shows each list's cue, counts the tips that print and lists them in print order", async () => {
    await renderPage();
    const items = demoItems(COFFEE);
    expect(printedItems(items, META)).toHaveLength(6);

    const coffee = card(COFFEE.title);
    expect(within(coffee).getByText("6 tips print.")).toBeTruthy();
    expect(within(coffee).getByText(`When: ${COFFEE.cue}`)).toBeTruthy();
    const byPosition = [...items].sort((a, b) => a.position - b.position);
    expect(shownTips(COFFEE.title)).toEqual(byPosition.map(line));

    // One approved tip and one pending: only the approved one counts and prints.
    const mixed = demoItems(BATHROOM);
    expect(mixed.map((item) => [item.name, item.status])).toEqual([
      ["Brush Your Teeth", limits.TIP_STATUS_APPROVED],
      ["Phone Sleeps Outside", limits.TIP_STATUS_PENDING],
    ]);
    const bathroom = card(BATHROOM.title);
    expect(within(bathroom).getByText("1 tip prints; 1 more waiting for review.")).toBeTruthy();
    const badges = bathroom.querySelectorAll(".list-pending");
    expect(badges).toHaveLength(1);
    expect(badges[0]?.closest("li")?.querySelector("b")?.textContent).toBe("Phone Sleeps Outside");
  });

  it("warns only on the list whose sheet spills onto a second page", async () => {
    await renderPage();
    expect(SPILLS.over_capacity).toBe(true);
    expect(FITS.over_capacity).toBe(false);

    const warning = card(SPILLS.title).querySelector('[data-warning="capacity"]');
    expect(warning?.textContent).toBe(`More than ${META.print_capacity} tips: the sheet spills onto a second page.`);
    expect(card(FITS.title).querySelector('[data-warning="capacity"]')).toBeNull();
    expect(within(card(FITS.title)).getByText(`${META.print_capacity} tips print.`)).toBeTruthy();
  });

  it("shows the approved daily tips that are on no list", async () => {
    await renderPage();
    expect(DEMO_UNPLACED.length).toBeGreaterThan(0);
    for (const tip of DEMO_UNPLACED) {
      expect(tip.status).toBe(limits.TIP_STATUS_APPROVED);
      expect(tip.bucket).toBe(limits.BUCKET_DAILY);
      expect(tip.list_ids).toEqual([]);
    }
    expect(unplacedNames()).toEqual(DEMO_UNPLACED.map((tip) => tip.name));
  });

  it("links to the activities sheet", async () => {
    await renderPage();
    expect(screen.getByRole("link", { name: "Print the activities sheet" }).getAttribute("href")).toBe(
      "#/print/activities",
    );
  });

  it("says so when every approved daily tip is placed", async () => {
    stubApi((request) => (request.url === "/api/tips?unplaced=true" ? ok([]) : undefined), ...demoRoutes());
    render(<ListsPage meta={META} />);
    const panel = await screen.findByRole("region", { name: "Not on any list yet" });
    expect(within(panel).getByText("Every approved daily tip is on a list.")).toBeTruthy();
  });

  it("shows an alert when the lists cannot load", async () => {
    stubApi((request) =>
      request.url === "/api/lists" ? apiError(503, "CC_E_VALIDATION", "database is busy") : undefined,
    );
    render(<ListsPage meta={META} />);
    expect((await screen.findByRole("alert")).textContent).toContain("database is busy");
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Lists");
  });

  it("waits for the app settings before it loads anything", () => {
    const sent = stubApi(...listsServer());
    render(<ListsPage meta={null} />);
    expect(screen.getByText("Loading the app settings…")).toBeTruthy();
    expect(sent).toEqual([]);
  });
});

describe("tips on a list", () => {
  it("reorders a list's tips: each move is one PUT of the new order, and the page shows the stored order", async () => {
    const sent = await renderPage();
    const items = demoItems(COFFEE);
    const [first, second] = items;
    if (first === undefined || second === undefined) throw new Error("the Coffee list needs two tips");
    const coffee = card(COFFEE.title);
    expect((within(coffee).getByRole("button", { name: `Move “${first.name}” up` }) as HTMLButtonElement).disabled).toBe(
      true,
    );

    fireEvent.click(within(coffee).getByRole("button", { name: `Move “${first.name}” down` }));
    await announced(`Moved “${first.name}” down on ${COFFEE.title}.`);

    const swapped = [second, first, ...items.slice(2)];
    expect(writes(sent)).toEqual([
      { method: "PUT", url: `/api/lists/${COFFEE.id}/items`, body: { tip_ids: swapped.map((item) => item.tip_id) } },
    ]);
    expect(shownTips(COFFEE.title)).toEqual(swapped.map(line));

    // The last tip moves up past the one before it.
    const last = required(items.at(-1), "last Coffee tip");
    const beforeLast = required(items.at(-2), "second-to-last Coffee tip");
    expect(
      (within(card(COFFEE.title)).getByRole("button", { name: `Move “${last.name}” down` }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    fireEvent.click(within(card(COFFEE.title)).getByRole("button", { name: `Move “${last.name}” up` }));
    await announced(`Moved “${last.name}” up on ${COFFEE.title}.`);

    const final = [second, first, ...items.slice(2, -2), last, beforeLast];
    expect(writes(sent).at(-1)?.body).toEqual({ tip_ids: final.map((item) => item.tip_id) });
    expect(shownTips(COFFEE.title)).toEqual(final.map(line));
    expect(within(card(COFFEE.title)).getByText("6 tips print.")).toBeTruthy();
  });

  it("removes a tip; one that is then on no list shows under Not on any list yet", async () => {
    const sent = await renderPage();
    const items = demoItems(COFFEE);
    const water = required(
      items.find((item) => item.name === "Glass Of Water First"),
      "Glass Of Water First",
    );

    fireEvent.click(within(card(COFFEE.title)).getByRole("button", { name: `Remove “${water.name}”` }));
    await announced(`Took “${water.name}” off ${COFFEE.title}. It is now under “Not on any list yet”.`);

    expect(writes(sent)).toEqual([
      {
        method: "PUT",
        url: `/api/lists/${COFFEE.id}/items`,
        body: { tip_ids: items.filter((item) => item !== water).map((item) => item.tip_id) },
      },
    ]);
    expect(shownTips(COFFEE.title)).toEqual(items.filter((item) => item !== water).map(line));
    expect(within(card(COFFEE.title)).getByText("5 tips print.")).toBeTruthy();
    expect(unplacedNames()).toEqual(byTipId([...DEMO_UNPLACED.map((tip) => tip.name), water.name]));
  });

  it("adds a daily tip from the picker at the end of the list, which takes it off Not on any list yet", async () => {
    const sent = await renderPage();
    const coffee = card(COFFEE.title);
    const picker = within(coffee).getByLabelText("Add a tip") as HTMLSelectElement;
    const groups = [...picker.querySelectorAll("optgroup")].map((group) => group.label);
    expect(groups).toEqual(["Not on any list yet", "On other lists"]);
    const lights = required(
      DEMO_UNPLACED.find((tip) => tip.name === "Lights Off As You Leave"),
      "Lights Off As You Leave",
    );
    const add = within(coffee).getByRole("button", { name: "Add to list" }) as HTMLButtonElement;
    expect(add.disabled).toBe(true);

    fireEvent.change(picker, { target: { value: String(lights.id) } });
    fireEvent.click(add);
    await announced(`Added “${lights.name}” to ${COFFEE.title}.`);

    const ids = demoItems(COFFEE).map((item) => item.tip_id);
    expect(writes(sent)).toEqual([
      { method: "PUT", url: `/api/lists/${COFFEE.id}/items`, body: { tip_ids: [...ids, lights.id] } },
    ]);
    expect(shownTips(COFFEE.title).at(-1)).toBe(line(lights));
    expect(unplacedNames()).not.toContain(lights.name);
    expect((within(card(COFFEE.title)).getByLabelText("Add a tip") as HTMLSelectElement).value).toBe("");
  });

  it("offers only the daily tips a list accepts that are not on it yet", async () => {
    await renderPage();

    const kidsPicker = within(card(KIDS_BATHROOM.title)).getByLabelText("Add a tip") as HTMLSelectElement;
    const offered = [...kidsPicker.querySelectorAll("option")].map((option) => option.value).filter((value) => value !== "");
    const choices = addChoices(KIDS_BATHROOM, demoItems(KIDS_BATHROOM), DEMO_DAILY, META);
    expect(offered).toEqual([...choices.unplaced, ...choices.elsewhere].map((tip) => String(tip.id)));
    expect(offered.length).toBeGreaterThan(0);
    const onKidsList = new Set(demoItems(KIDS_BATHROOM).map((item) => String(item.tip_id)));
    for (const value of offered) {
      const tip = required(
        DEMO_DAILY.find((item) => String(item.id) === value),
        `tip ${value}`,
      );
      expect(tip.audience).toBe(limits.AUDIENCE_KIDS_PLUS);
      expect(onKidsList.has(value)).toBe(false);
    }
    // An adults list may take both audiences, pending tips included (marked as such).
    const coffeeChoices = addChoices(COFFEE, demoItems(COFFEE), DEMO_DAILY, META);
    const audiences = new Set([...coffeeChoices.unplaced, ...coffeeChoices.elsewhere].map((tip) => tip.audience));
    expect(audiences).toEqual(new Set(limits.AUDIENCES_TIP));
    const coffeePicker = within(card(COFFEE.title)).getByLabelText("Add a tip");
    const pendingOption = [...coffeePicker.querySelectorAll("option")].find((option) =>
      option.textContent?.includes("Phone Sleeps Outside"),
    );
    expect(pendingOption?.textContent).toMatch(/\(waiting for review\)$/);
  });

  it("shows the server's 409 on the card and keeps the list as it was", async () => {
    const message = 'tip #8 "Phone Sleeps Outside" is adults; nothing was changed';
    const sent = await renderPage((request) =>
      request.method === "PUT" && request.url === `/api/lists/${OFFICE.id}/items`
        ? apiError(409, "CC_E_CONFLICT", message, ["tip_ids"])
        : undefined,
    );
    const office = card(OFFICE.title);
    const lights = required(DEMO_UNPLACED[0], "an unplaced tip");

    fireEvent.change(within(office).getByLabelText("Add a tip"), { target: { value: String(lights.id) } });
    fireEvent.click(within(office).getByRole("button", { name: "Add to list" }));

    expect((await within(card(OFFICE.title)).findByRole("alert")).textContent).toBe(`Could not add the tip: ${message}`);
    expect(writes(sent)).toHaveLength(1);
    expect(shownTips(OFFICE.title)).toEqual(demoItems(OFFICE).map(line));
    expect(status()).toBe("");
  });
});

describe("managing lists", () => {
  it("creates a list with a cue at the end of the page", async () => {
    const sent = await renderPage();
    fireEvent.click(screen.getByRole("button", { name: "New list" }));
    const panel = screen.getByRole("region", { name: "New list" });
    const create = within(panel).getByRole("button", { name: "Create list" }) as HTMLButtonElement;
    expect(create.disabled).toBe(true);
    expect((within(panel).getByLabelText("Audience") as HTMLSelectElement).value).toBe(limits.AUDIENCE_ADULTS);
    expect((within(panel).getByLabelText("Place") as HTMLInputElement).maxLength).toBe(limits.PLACE_MAX);
    expect((within(panel).getByLabelText("Cue") as HTMLInputElement).maxLength).toBe(limits.CUE_MAX);

    // A place already listed for this audience is refused before anything is sent.
    fireEvent.change(within(panel).getByLabelText("Place"), { target: { value: "Coffee Machine" } });
    expect(within(panel).getByText(`There is already a list ${COFFEE.title}.`)).toBeTruthy();
    expect(create.disabled).toBe(true);

    const cue = "Getting ready for bed or getting up";
    fireEvent.change(within(panel).getByLabelText("Place"), { target: { value: " Bedroom " } });
    fireEvent.change(within(panel).getByLabelText("Cue"), { target: { value: cue } });
    fireEvent.click(create);
    await announced("Created Bedroom — Adults. Add daily tips to it below; the skill reads its cue on the next ingest.");

    expect(writes(sent)).toEqual([
      { method: "POST", url: "/api/lists", body: { place: "Bedroom", audience: limits.AUDIENCE_ADULTS, cue } },
    ]);
    expect(cardTitles().at(-1)).toBe("Bedroom — Adults");
    const bedroom = card("Bedroom — Adults");
    expect(within(bedroom).getByText(`When: ${cue}`)).toBeTruthy();
    expect(within(bedroom).getByText("0 tips print.")).toBeTruthy();
    expect(within(bedroom).getByRole("link", { name: "Print Bedroom — Adults" }).getAttribute("href")).toBe(
      `#/print/list/${Math.max(...DEMO_LISTS.map((list) => list.id)) + 1}`,
    );
    expect(screen.queryByRole("region", { name: "New list" })).toBeNull();
  });

  it("edits a list's cue: only the cue is sent, and the card shows the stored one", async () => {
    const sent = await renderPage();
    fireEvent.click(within(card(COFFEE.title)).getByRole("button", { name: "Edit list" }));
    const form = within(card(COFFEE.title)).getByRole("group", { name: `Edit ${COFFEE.title}` });
    expect((within(form).getByLabelText("Cue") as HTMLInputElement).value).toBe(COFFEE.cue);
    expect((within(form).getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(true);

    const cue = "Waiting for the kettle or the coffee machine";
    fireEvent.change(within(form).getByLabelText("Cue"), { target: { value: cue } });
    fireEvent.click(within(form).getByRole("button", { name: "Save" }));
    await announced(`Saved ${COFFEE.title}. The skill reads the new cue on its next ingest.`);

    expect(writes(sent)).toEqual([{ method: "PATCH", url: `/api/lists/${COFFEE.id}`, body: { cue } }]);
    expect(within(card(COFFEE.title)).getByText(`When: ${cue}`)).toBeTruthy();
    expect(within(card(COFFEE.title)).queryByRole("group", { name: `Edit ${COFFEE.title}` })).toBeNull();
  });

  it("changing a list to kids warns first, then names the adults tips the server took off", async () => {
    const sent = await renderPage();
    const phone = required(
      demoItems(BATHROOM).find((item) => item.audience === limits.AUDIENCE_ADULTS),
      "an adults tip on the bathroom list",
    );
    fireEvent.click(within(card(BATHROOM.title)).getByRole("button", { name: "Edit list" }));
    const form = within(card(BATHROOM.title)).getByRole("group", { name: `Edit ${BATHROOM.title}` });

    fireEvent.change(within(form).getByLabelText("Audience"), { target: { value: limits.AUDIENCE_KIDS } });
    expect(form.querySelector('[data-warning="audience"]')?.textContent).toBe(
      `Saving takes this tip off the list, since ${kidsRule(META)}: “${phone.name}”.`,
    );
    fireEvent.click(within(form).getByRole("button", { name: "Save" }));
    const kidsTitle = `Adult Bathroom${SEP}Kids`;
    await announced(`Saved ${kidsTitle}. Took 1 tip off it, since ${kidsRule(META)}: “${phone.name}”.`);

    expect(writes(sent)).toEqual([
      { method: "PATCH", url: `/api/lists/${BATHROOM.id}`, body: { audience: limits.AUDIENCE_KIDS } },
    ]);
    expect(cardTitles()[0]).toBe(kidsTitle);
    expect(shownTips(kidsTitle)).toEqual(demoItems(BATHROOM).filter((item) => item !== phone).map(line));
  });

  it("moves a list down: the page order is stored as each list's sort_order", async () => {
    const sent = await renderPage();
    const first = card(BATHROOM.title);
    expect((within(first).getByRole("button", { name: "Move list up" }) as HTMLButtonElement).disabled).toBe(true);
    expect(
      (within(card(SPILLS.title)).getByRole("button", { name: "Move list down" }) as HTMLButtonElement).disabled,
    ).toBe(true);

    fireEvent.click(within(first).getByRole("button", { name: "Move list down" }));
    await announced(`Moved ${BATHROOM.title} down.`);

    expect(orderPatches(moved(DEMO_LISTS, 0, 1))).toEqual([
      { id: OFFICE.id, sort_order: 1 },
      { id: BATHROOM.id, sort_order: 2 },
    ]);
    expect(writes(sent)).toEqual([
      { method: "PATCH", url: `/api/lists/${OFFICE.id}`, body: { sort_order: 1 } },
      { method: "PATCH", url: `/api/lists/${BATHROOM.id}`, body: { sort_order: 2 } },
    ]);
    expect(cardTitles().slice(0, 3)).toEqual([OFFICE.title, BATHROOM.title, KIDS_BATHROOM.title]);
  });

  it("deletes a list after a confirmation; its approved tips on no other list become unplaced", async () => {
    const sent = await renderPage();
    const coffee = card(COFFEE.title);

    fireEvent.click(within(coffee).getByRole("button", { name: "Delete list" }));
    const confirm = within(coffee).getByRole("group", { name: `Delete ${COFFEE.title}` });
    fireEvent.click(within(confirm).getByRole("button", { name: "Keep it" }));
    expect(writes(sent)).toEqual([]);
    fireEvent.click(within(card(COFFEE.title)).getByRole("button", { name: "Delete list" }));
    fireEvent.click(within(card(COFFEE.title)).getByRole("button", { name: "Delete for good" }));
    await announced(`Deleted the list ${COFFEE.title}. Its tips are still in the Library.`);

    expect(writes(sent)).toEqual([{ method: "DELETE", url: `/api/lists/${COFFEE.id}`, body: undefined }]);
    expect(cardTitles()).not.toContain(COFFEE.title);
    expect(unplacedNames()).toEqual(
      byTipId([...DEMO_UNPLACED.map((tip) => tip.name), ...demoItems(COFFEE).map((item) => item.name)]),
    );
  });

  it("checks a list draft against the limits and the other lists", () => {
    const draft = { place: "Bedroom", audience: limits.AUDIENCE_ADULTS, cue: "" };
    expect(listDraftProblem(draft, DEMO_LISTS, META, null)).toBeNull();
    expect(listDraftProblem({ ...draft, place: "  " }, DEMO_LISTS, META, null)).toBe("The place is empty.");
    expect(listDraftProblem({ ...draft, place: "p".repeat(limits.PLACE_MAX + 1) }, DEMO_LISTS, META, null)).toBe(
      `The place is ${limits.PLACE_MAX + 1} characters; ${limits.PLACE_MAX} fit.`,
    );
    expect(listDraftProblem({ ...draft, cue: "c".repeat(limits.CUE_MAX + 1) }, DEMO_LISTS, META, null)).toBe(
      `The cue is ${limits.CUE_MAX + 1} characters; ${limits.CUE_MAX} fit.`,
    );
    // Its own place and audience are no clash for the list being edited.
    const own = { place: COFFEE.place, audience: COFFEE.audience, cue: "" };
    expect(listDraftProblem(own, DEMO_LISTS, META, COFFEE.id)).toBeNull();
    expect(listDraftProblem(own, DEMO_LISTS, META, null)).toBe(`There is already a list ${COFFEE.title}.`);
  });
});

describe("overlong rows", () => {
  // jsdom has no layout: give each measured .print-text the line width of the printed sheet
  // and its contents the width `contentWidth` returns (see print/PrintPages.test.tsx).
  const LINE = 698.140625;

  function stubLayout(contentWidth: (text: string) => number) {
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
      return DOMRect.fromRect({ width: this.classList.contains("print-text") ? LINE : 0, height: 20 });
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
  });

  it("measures each list's rows at the printed width and names the ones that would be cut", async () => {
    const long = required(demoItems(COFFEE)[2], "a third Coffee tip");
    stubLayout((text) => (text.startsWith(long.name) ? LINE + 0.25 : LINE - 1));
    await renderPage();

    const coffee = card(COFFEE.title);
    await waitFor(() => expect(coffee.getAttribute("data-measured")).toBe("true"));
    await waitFor(() => expect(coffee.querySelector('[data-warning="overflow"]')).not.toBeNull());
    expect(coffee.querySelector('[data-warning="overflow"]')?.textContent).toBe(
      `${overlongMessage([long.name])} Shorten it in the Library before printing.`,
    );
    const rows = within(within(coffee).getByRole("list", { name: `Tips on ${COFFEE.title}` })).getAllByRole("listitem");
    expect(rows.map((row) => row.getAttribute("data-overflow"))).toEqual(
      demoItems(COFFEE).map((item) => (item === long ? "true" : "false")),
    );
    expect(rows[2]?.querySelector(".list-overlong")?.textContent).toBe("too long for one line");
    // The other lists fit.
    for (const list of DEMO_LISTS.filter((item) => item !== COFFEE)) {
      expect(card(list.title).querySelector('[data-warning="overflow"]')).toBeNull();
    }
    // The measured rows are the sheet's own: a checkbox row per tip, inside a print sheet.
    const measured = coffee.querySelectorAll(".list-measure .print-sheet .print-row .print-text");
    expect([...measured].map((text) => text.textContent)).toEqual(demoItems(COFFEE).map(line));
    expect(coffee.querySelectorAll(".list-measure .print-box")).toHaveLength(demoItems(COFFEE).length);
  });

  it("finds nothing to flag when every row fits", async () => {
    stubLayout(() => LINE);
    await renderPage();
    const coffee = card(COFFEE.title);
    await waitFor(() => expect(coffee.getAttribute("data-measured")).toBe("true"));
    expect(document.querySelectorAll('[data-warning="overflow"]')).toHaveLength(0);
    expect(overlongMessage(["A", "B"])).toBe(
      "2 tips are too long for one printed line, so their end would be cut off: A, B.",
    );
  });
});
