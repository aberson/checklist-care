// Shared parts of the A4 print sheets (plan §4.7): the screen controls (Print button and
// hint), the sheet header with its date line, one-line rows, and the per-row overflow
// measurement. Each row is measured on screen at the printed width, in fractional pixels
// (`overflowPx`), and a row that would be cut is flagged on the row and listed in a warning,
// so the operator shortens it before printing. Screen-only parts carry `screen-only`, which
// print.css hides on paper.
import type { ReactNode, RefObject } from "react";
import { useEffect, useLayoutEffect, useState } from "react";
import type { PrintItem } from "../model";
import { href } from "../router";
import "./print.css";

export const PRINT_HINT = "Print on A4 at 100% scale, with browser headers and footers turned off.";

/** The 8pt date line of a sheet generated at `generatedAt` (an ISO timestamp). */
export function printedDate(generatedAt: string): string {
  const moment = new Date(generatedAt);
  if (Number.isNaN(moment.getTime())) return `Printed ${generatedAt}`;
  return `Printed ${moment.toLocaleDateString(undefined, { dateStyle: "long" })}`;
}

/** While mounted with a title, the browser tab (and a saved PDF's name) shows it. */
export function useDocumentTitle(title: string | null): void {
  useEffect(() => {
    if (title === null) return;
    const previous = document.title;
    document.title = `${title} · checklist-care`;
    return () => {
      document.title = previous;
    };
  }, [title]);
}

export interface RowOverflow {
  /** The `data-row` numbers of the rows whose text does not fit on one line. */
  overflowing: ReadonlySet<number>;
  /**
   * True once the current rows were measured with their final fonts (after
   * `document.fonts.ready`): "nothing overflows" means something only then.
   */
  measured: boolean;
}

/**
 * How far the content of `text` runs past the end of its line, in fractional CSS pixels; any
 * amount above 0 is cut off behind the ellipsis. `scrollWidth` and `clientWidth` are rounded
 * to whole pixels, so they read an overflow of under about half a pixel as fitting although
 * Chromium still cuts it. A Range over the contents keeps their full, unclipped width.
 */
export function overflowPx(text: HTMLElement): number {
  const range = document.createRange();
  range.selectNodeContents(text);
  // jsdom has no Range layout (nor any layout): fall back to the whole-pixel measure there.
  if (typeof range.getBoundingClientRect !== "function") return text.scrollWidth - text.clientWidth;
  return range.getBoundingClientRect().width - text.getBoundingClientRect().width;
}

/**
 * Measure every row text (`.print-text[data-row]`) inside `sheet` after each render of new
 * `rows` (before paint), and again once the fonts are ready; the rows count as `measured`
 * only after that second measurement. A row overflows when `overflowPx` is above 0.
 */
export function useRowOverflow(sheet: RefObject<HTMLElement | null>, rows: unknown): RowOverflow {
  const [result, setResult] = useState<{
    rows: unknown;
    overflowing: ReadonlySet<number>;
    settled: boolean;
  } | null>(null);
  useLayoutEffect(() => {
    let active = true;
    const measure = (settled: boolean) => {
      const root = sheet.current;
      if (!active || root === null) return;
      const overflowing = new Set<number>();
      for (const text of root.querySelectorAll<HTMLElement>(".print-text[data-row]")) {
        if (overflowPx(text) > 0) overflowing.add(Number(text.dataset.row));
      }
      setResult({ rows, overflowing, settled });
    };
    // jsdom has no document.fonts: there the first measurement is final.
    const fonts: FontFaceSet | undefined = document.fonts;
    measure(fonts === undefined);
    fonts?.ready.then(
      () => measure(true),
      () => measure(true),
    );
    return () => {
      active = false;
    };
  }, [sheet, rows]);
  const current = result !== null && result.rows === rows;
  return { overflowing: current ? result.overflowing : new Set(), measured: current && result.settled };
}

/** The Print button and the A4 hint, above the sheet; neither prints. */
export function PrintControls() {
  return (
    <div className="print-controls screen-only">
      <a href={href("lists")}>← Back to Lists</a>
      <button type="button" className="print-button" onClick={() => window.print()}>
        Print
      </button>
      <p className="print-hint">{PRINT_HINT}</p>
    </div>
  );
}

/** The sheet header: the title in 16pt bold and the 8pt date line. */
export function SheetHeader({ title, generatedAt }: { title: string; generatedAt: string }) {
  return (
    <header className="print-header">
      <h1 className="print-title">{title}</h1>
      <p className="print-date">{printedDate(generatedAt)}</p>
    </header>
  );
}

/** One printed line: an optional 10pt checkbox, the name in bold, the separator, the action. */
export function PrintRow({
  item,
  row,
  separator,
  checkbox,
  overflowing,
}: {
  item: PrintItem;
  row: number;
  separator: string;
  checkbox: boolean;
  overflowing: boolean;
}) {
  return (
    <li
      className={overflowing ? "print-row print-row-overflow" : "print-row"}
      data-overflow={overflowing ? "true" : "false"}
    >
      {checkbox && <span className="print-box" aria-hidden="true" />}
      <span className="print-text" data-row={row}>
        <b className="print-name">{item.name}</b>
        {separator}
        {item.action}
      </span>
    </li>
  );
}

/** A screen-only warning block; `kind` names it for the tests (`data-warning`). */
export function SheetWarning({ kind, children }: { kind: string; children: ReactNode }) {
  return (
    <div className="print-warning screen-only" role="alert" data-warning={kind}>
      {children}
    </div>
  );
}

/** Names the rows that do not fit on one line; nothing when every row fits. */
export function OverflowWarning({ items, overflowing }: { items: readonly PrintItem[]; overflowing: ReadonlySet<number> }) {
  if (overflowing.size === 0) return null;
  const rows = [...overflowing].sort((a, b) => a - b);
  const count = rows.length;
  return (
    <SheetWarning kind="overflow">
      <p>
        {count === 1 ? "1 row is" : `${count} rows are`} too long for one line, so {count === 1 ? "its" : "their"} end
        would be cut off on paper. Shorten {count === 1 ? "it" : "them"} before printing:
      </p>
      <ul>
        {rows.map((row) => (
          <li key={row}>{items[row]?.name ?? `Row ${row + 1}`}</li>
        ))}
      </ul>
    </SheetWarning>
  );
}
