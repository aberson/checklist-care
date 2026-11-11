// The A4 print sheets in Chromium against `checklist-care serve --demo` (plan §4.7, §5,
// Step 7), whose bundle `pretest:print` builds first. Every PDF is made with page.pdf({format: "A4", preferCSSPageSize: true,
// printBackground: true}) and its pages are counted as `/Type /Page` objects (never the
// `/Type /Pages` tree nodes). The demo's capacity lists hold PRINT_CAPACITY (list 8) and
// PRINT_CAPACITY + 1 (list 9) approved tips whose lines are exactly LINE_MAX characters.
// The red anchors stub the sheet payload with page.route to show that both the page count and
// the overflow flag can fail, down to a row that runs past its line by under a pixel.
// The Lists page measures each list's rows with the same sheet markup, so a stubbed overlong row
// is flagged on its list card while the demo's own rows fit there too (Step 11).
import { expect, test } from "@playwright/test";
import type { APIRequestContext, Locator, Page } from "@playwright/test";

const COFFEE_LIST = 7;
const FITS_LIST = 8;
const SPILLS_LIST = 9;

interface Meta {
  print_capacity: number;
  limits: { ACTION_MAX: number; LINE_MAX: number; LINE_SEPARATOR: string; NAME_MAX: number };
}

interface PrintItem {
  name: string;
  action: string;
}

interface LocationSheet {
  title: string;
  audience: string;
  generated_at: string;
  items: PrintItem[];
  over_capacity: boolean;
}

/** The pages of a PDF: its `/Type /Page` objects. A `/Type /Pages` node is not a page. */
function countPdfPages(pdf: Uint8Array): number {
  const text = new TextDecoder("latin1").decode(pdf);
  return text.match(/\/Type\s*\/Page(?![A-Za-z])/g)?.length ?? 0;
}

async function printedPages(page: Page): Promise<number> {
  const pdf = await page.pdf({ format: "A4", preferCSSPageSize: true, printBackground: true });
  return countPdfPages(pdf);
}

async function loadMeta(request: APIRequestContext): Promise<Meta> {
  const response = await request.get("/api/meta");
  expect(response.ok()).toBe(true);
  return (await response.json()) as Meta;
}

async function loadSheet(request: APIRequestContext, listId: number): Promise<LocationSheet> {
  const response = await request.get(`/api/print/lists/${listId}`);
  expect(response.ok()).toBe(true);
  return (await response.json()) as LocationSheet;
}

/** Open a sheet and wait until its rows were measured, so "no overflow" means something. */
async function openSheet(page: Page, hash: string) {
  await page.goto(`/${hash}`);
  const sheet = page.locator("article.print-sheet");
  await expect(sheet).toHaveAttribute("data-measured", "true");
  return sheet;
}

/** Characters as Python counts them (code points), as LINE_MAX does. */
function length(text: string): number {
  return [...text].length;
}

/** A row of demo text measured against the printed line, in fractional CSS pixels. */
interface HairlineRow extends PrintItem {
  /** The width of the row's text laid out without a limit (`width: max-content`). */
  natural: number;
  /** The width of the line the text gets on the sheet. */
  line: number;
  /** The whole-pixel measure: what `scrollWidth > clientWidth` saw. */
  scrollWidth: number;
  clientWidth: number;
}

/**
 * In an open sheet, look through demo names and capitals actions (cut to NAME_MAX, ACTION_MAX
 * and LINE_MAX, so a stored tip could be this wide) for two rows within a pixel of the line:
 * `over` runs past it by under a pixel while `scrollWidth` and `clientWidth` (whole pixels)
 * read it as fitting, and `under` fits by under a pixel. The widths come from probe rows with
 * the sheet's own classes, removed again afterwards; the app's measurement is not used.
 */
async function findHairlineRows(
  page: Page,
  items: readonly PrintItem[],
  limits: Meta["limits"],
): Promise<{ over: HairlineRow | null; under: HairlineRow | null }> {
  return page.evaluate(
    ({ items, limits }) => {
      const list = document.querySelector("article.print-sheet .print-rows");
      const real = list?.querySelector(".print-text");
      if (!list || !real) throw new Error("the sheet has no rows to measure against");
      const line = real.getBoundingClientRect().width;
      const probe = (style: string) => {
        const row = document.createElement("li");
        row.className = "print-row";
        const box = document.createElement("span");
        box.className = "print-box";
        const text = document.createElement("span");
        text.className = "print-text";
        text.style.cssText = style;
        const name = document.createElement("b");
        name.className = "print-name";
        const rest = document.createTextNode("");
        text.append(name, rest);
        row.append(box, text);
        list.append(row);
        return {
          row,
          text,
          show(item: PrintItem) {
            name.textContent = item.name;
            rest.data = `${limits.LINE_SEPARATOR}${item.action}`;
          },
        };
      };
      const unlimited = probe("flex: none; width: max-content; overflow: visible");
      const onLine = probe("");
      const natural = (item: PrintItem) => {
        unlimited.show(item);
        return unlimited.text.getBoundingClientRect().width;
      };
      const measured = (item: PrintItem): HairlineRow => {
        onLine.show(item);
        return { ...item, natural: natural(item), line, scrollWidth: onLine.text.scrollWidth, clientWidth: onLine.text.clientWidth };
      };
      const fitsLimits = (item: PrintItem) =>
        [...item.name].length <= limits.NAME_MAX &&
        [...item.action].length <= limits.ACTION_MAX &&
        [...`${item.name}${limits.LINE_SEPARATOR}${item.action}`].length <= limits.LINE_MAX &&
        !/\s$/.test(item.action);
      let over: HairlineRow | null = null;
      let under: HairlineRow | null = null;
      try {
        for (const first of items) {
          for (const second of items) {
            if (first === second) continue;
            const tail = second.action.toUpperCase();
            if (natural({ name: first.name, action: tail }) <= line) continue;
            // The shortest cut of `tail` that runs past the line.
            let low = 1;
            let high = tail.length;
            while (low < high) {
              const middle = (low + high) >> 1;
              if (natural({ name: first.name, action: tail.slice(0, middle) }) > line) high = middle;
              else low = middle + 1;
            }
            const past = { name: first.name, action: tail.slice(0, low) };
            const short = { name: first.name, action: tail.slice(0, low - 1) };
            if (over === null && fitsLimits(past)) {
              const row = measured(past);
              if (row.natural - line < 1 && row.scrollWidth <= row.clientWidth) over = row;
            }
            if (under === null && fitsLimits(short) && short.name !== over?.name) {
              const row = measured(short);
              if (line - row.natural < 1) under = row;
            }
            if (over !== null && under !== null) return { over, under };
          }
        }
        return { over, under };
      } finally {
        unlimited.row.remove();
        onLine.row.remove();
      }
    },
    { items, limits },
  );
}

/** Whether Chromium draws an ellipsis on `text`: its pixels change when the cut is a plain clip. */
async function drawsEllipsis(text: Locator): Promise<boolean> {
  const ellipsis = new Uint8Array(await text.screenshot());
  await text.evaluate((element) => {
    element.style.textOverflow = "clip";
  });
  const clipped = new Uint8Array(await text.screenshot());
  await text.evaluate((element) => {
    element.style.textOverflow = "";
  });
  return !(ellipsis.length === clipped.length && ellipsis.every((byte, index) => byte === clipped[index]));
}

test("the page counter counts /Type /Page objects, not the /Pages tree", () => {
  const twoPages = "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >> << /Type /Page >> <</Type/Page/Parent 2 0 R>>";
  expect(countPdfPages(new TextEncoder().encode(twoPages))).toBe(2);
  expect(countPdfPages(new TextEncoder().encode("<< /Type /Pages /Count 0 >>"))).toBe(0);
});

test("list 8 holds PRINT_CAPACITY rows at LINE_MAX, prints as exactly one A4 page, and no row overflows", async ({
  page,
  request,
}) => {
  const meta = await loadMeta(request);
  const sheet = await openSheet(page, `#/print/list/${FITS_LIST}`);

  const rows = sheet.locator(".print-row");
  await expect(rows).toHaveCount(meta.print_capacity);
  const lines = await sheet.locator(".print-text").allTextContents();
  expect(lines.map(length)).toEqual(Array(meta.print_capacity).fill(meta.limits.LINE_MAX));
  await expect(sheet.locator(".print-box")).toHaveCount(meta.print_capacity);
  await expect(sheet.locator('[data-overflow="true"]')).toHaveCount(0);
  await expect(sheet.locator('[data-overflow="false"]')).toHaveCount(meta.print_capacity);
  await expect(page.locator("[data-warning]")).toHaveCount(0);

  expect(await printedPages(page)).toBe(1);
});

test("list 9's screen view warns that it spills onto a second page, and it prints on two", async ({
  page,
  request,
}) => {
  const meta = await loadMeta(request);
  const sheet = await openSheet(page, `#/print/list/${SPILLS_LIST}`);

  await expect(sheet.locator(".print-row")).toHaveCount(meta.print_capacity + 1);
  const warning = page.locator('[data-warning="capacity"]');
  await expect(warning).toBeVisible();
  await expect(warning).toContainText("spills onto a second page");
  await expect(sheet.locator('[data-overflow="true"]')).toHaveCount(0);

  expect(await printedPages(page)).toBe(2);
});

test("red anchor: a stubbed sheet of 2 x PRINT_CAPACITY rows prints on at least two pages", async ({
  page,
  request,
}) => {
  const meta = await loadMeta(request);
  const fits = await loadSheet(request, FITS_LIST);
  const doubled: LocationSheet = { ...fits, items: [...fits.items, ...fits.items], over_capacity: true };
  expect(doubled.items).toHaveLength(2 * meta.print_capacity);
  await page.route(`**/api/print/lists/${FITS_LIST}`, (route) => route.fulfill({ json: doubled }));

  const sheet = await openSheet(page, `#/print/list/${FITS_LIST}`);
  await expect(sheet.locator(".print-row")).toHaveCount(2 * meta.print_capacity);

  expect(await printedPages(page)).toBeGreaterThanOrEqual(2);
});

test("red anchor: a stubbed 80-character all-capitals row with a bold name is flagged as overflowing", async ({
  page,
  request,
}) => {
  const meta = await loadMeta(request);
  const { LINE_MAX, LINE_SEPARATOR, NAME_MAX } = meta.limits;
  const fits = await loadSheet(request, FITS_LIST);
  const control = fits.items[0];
  if (control === undefined) throw new Error("list 8 has no rows");
  const name = "WASH YOUR HANDS BEFORE EVERY SINGLE MEAL";
  const action = "SCRUB WITH SOAP AND WARM WATER, THEN DRY THEM".slice(0, LINE_MAX - name.length - LINE_SEPARATOR.length);
  expect(name).toHaveLength(NAME_MAX);
  expect(length(`${name}${LINE_SEPARATOR}${action}`)).toBe(LINE_MAX);
  expect(action).toBe(action.toUpperCase());
  const stub: LocationSheet = { ...fits, items: [control, { name, action }], over_capacity: false };
  await page.route(`**/api/print/lists/${FITS_LIST}`, (route) => route.fulfill({ json: stub }));

  const sheet = await openSheet(page, `#/print/list/${FITS_LIST}`);
  const rows = sheet.locator(".print-row");
  await expect(rows).toHaveCount(2);
  await expect(rows.nth(0)).toHaveAttribute("data-overflow", "false");
  await expect(rows.nth(1)).toHaveAttribute("data-overflow", "true");
  await expect(rows.nth(1).locator("b")).toHaveText(name);
  expect(await rows.nth(1).locator("b").evaluate((element) => getComputedStyle(element).fontWeight)).toBe("700");
  const [scrollWidth, clientWidth] = await rows
    .nth(1)
    .locator(".print-text")
    .evaluate((element) => [element.scrollWidth, element.clientWidth]);
  expect(scrollWidth).toBeGreaterThan(clientWidth ?? Number.POSITIVE_INFINITY);
  const warning = page.locator('[data-warning="overflow"]');
  await expect(warning).toBeVisible();
  await expect(warning).toContainText(name);
  await expect(warning).not.toContainText(control.name);
});

test("red anchor: a row that runs past its line by under a pixel is flagged, and one that fits by under a pixel is not", async ({
  page,
  request,
}) => {
  const meta = await loadMeta(request);
  const fits = await loadSheet(request, FITS_LIST);
  const control = fits.items[0];
  if (control === undefined) throw new Error("list 8 has no rows");
  await openSheet(page, `#/print/list/${FITS_LIST}`);
  const { over, under } = await findHairlineRows(page, fits.items, meta.limits);
  if (over === null || under === null) throw new Error("no demo text lands within a pixel of the line");
  // The whole-pixel measure reads `over` as fitting: this is the row it missed.
  expect(over.natural - over.line).toBeGreaterThan(0);
  expect(over.natural - over.line).toBeLessThan(1);
  expect(over.scrollWidth).toBeLessThanOrEqual(over.clientWidth);
  expect(under.line - under.natural).toBeGreaterThanOrEqual(0);
  expect(under.line - under.natural).toBeLessThan(1);

  const items = [control, over, under].map(({ name, action }) => ({ name, action }));
  const stub: LocationSheet = { ...fits, items, over_capacity: false };
  await page.route(`**/api/print/lists/${FITS_LIST}`, (route) => route.fulfill({ json: stub }));
  await page.reload();
  const sheet = page.locator("article.print-sheet");
  await expect(sheet).toHaveAttribute("data-measured", "true");

  const rows = sheet.locator(".print-row");
  await expect(rows).toHaveCount(3);
  await expect(rows.nth(1).locator(".print-text")).toHaveText(`${over.name}${meta.limits.LINE_SEPARATOR}${over.action}`);
  // Chromium cuts `over` (an ellipsis is drawn) and not `under` or the control row.
  expect(await drawsEllipsis(rows.nth(1).locator(".print-text"))).toBe(true);
  expect(await drawsEllipsis(rows.nth(2).locator(".print-text"))).toBe(false);
  expect(await drawsEllipsis(rows.nth(0).locator(".print-text"))).toBe(false);
  await expect(rows.nth(0)).toHaveAttribute("data-overflow", "false");
  await expect(rows.nth(1)).toHaveAttribute("data-overflow", "true");
  await expect(rows.nth(2)).toHaveAttribute("data-overflow", "false");
  const warning = page.locator('[data-warning="overflow"]');
  await expect(warning.locator("li")).toHaveText([over.name]);
});

test("the Lists page measures each list's rows as its sheet prints them and flags a stubbed overlong row", async ({
  page,
  request,
}) => {
  const meta = await loadMeta(request);
  const { LINE_MAX, LINE_SEPARATOR, NAME_MAX } = meta.limits;
  const listsResponse = await request.get("/api/lists");
  expect(listsResponse.ok()).toBe(true);
  const lists = (await listsResponse.json()) as { id: number; title: string }[];
  const itemsResponse = await request.get(`/api/lists/${COFFEE_LIST}/items`);
  expect(itemsResponse.ok()).toBe(true);
  const items = (await itemsResponse.json()) as (PrintItem & { tip_id: number; position: number })[];
  const control = items[0];
  if (control === undefined) throw new Error("list 7 has no tips");
  // The same 80-character all-capitals row the print-sheet red anchor flags.
  const name = "WASH YOUR HANDS BEFORE EVERY SINGLE MEAL";
  const action = "SCRUB WITH SOAP AND WARM WATER, THEN DRY THEM".slice(0, LINE_MAX - name.length - LINE_SEPARATOR.length);
  expect(name).toHaveLength(NAME_MAX);
  expect(length(`${name}${LINE_SEPARATOR}${action}`)).toBe(LINE_MAX);
  const long = { ...control, tip_id: 999_999, position: items.length + 1, name, action };
  await page.route(`**/api/lists/${COFFEE_LIST}/items`, (route) => route.fulfill({ json: [...items, long] }));

  await page.goto("/#/lists");
  const cards = page.locator(".list-card");
  await expect(cards).toHaveCount(lists.length);
  await expect(page.locator('.list-card[data-measured="true"]')).toHaveCount(lists.length);

  const coffee = page.getByRole("listitem", { name: "Coffee Machine — Adults" });
  const warning = coffee.locator('[data-warning="overflow"]');
  await expect(warning).toBeVisible();
  await expect(warning).toContainText(name);
  await expect(warning).not.toContainText(control.name);
  const rows = coffee.locator(".list-item");
  await expect(rows).toHaveCount(items.length + 1);
  await expect(rows.last()).toHaveAttribute("data-overflow", "true");
  await expect(rows.first()).toHaveAttribute("data-overflow", "false");
  // Only that row: the demo's own rows, the two full capacity lists at LINE_MAX included, fit.
  await expect(page.locator('[data-warning="overflow"]')).toHaveCount(1);
  await expect(page.locator('.list-item[data-overflow="true"]')).toHaveCount(1);
  // The measuring copy never shows: not on screen, and not on paper.
  await expect(coffee.locator(".list-measure")).toBeHidden();
});

test("the activities sheet shows the Kids+ and Adults groups, one line each and no checkboxes", async ({ page }) => {
  const sheet = await openSheet(page, "#/print/activities");

  await expect(sheet.getByRole("heading", { level: 1 })).toHaveText("Good to do now and then");
  for (const label of ["Kids+", "Adults"]) {
    const group = sheet.getByRole("region", { name: label });
    await expect(group.getByRole("heading", { level: 2 })).toHaveText(label);
    expect(await group.locator(".print-row").count()).toBeGreaterThanOrEqual(2);
  }
  await expect(sheet.locator(".print-box")).toHaveCount(0);
  await expect(sheet.locator('[data-overflow="true"]')).toHaveCount(0);

  expect(await printedPages(page)).toBe(1);
});

test("the Coffee Machine sheet shows its controls on screen and only the sheet on paper", async ({ page }) => {
  const sheet = await openSheet(page, `#/print/list/${COFFEE_LIST}`);
  await expect(sheet.getByRole("heading", { level: 1 })).toHaveText("Coffee Machine — Adults");
  await expect(sheet.locator(".print-row")).toHaveCount(6);
  const hint = page.getByText("Print on A4 at 100% scale, with browser headers and footers turned off.");
  await expect(hint).toBeVisible();
  await expect(page.getByRole("button", { name: "Print" })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Main" })).toBeVisible();

  await page.emulateMedia({ media: "print" });
  await expect(hint).toBeHidden();
  await expect(page.getByRole("button", { name: "Print" })).toBeHidden();
  await expect(page.getByRole("navigation", { name: "Main" })).toBeHidden();
  await expect(sheet.locator(".print-row").first()).toBeVisible();
  expect(await printedPages(page)).toBe(1);
});
