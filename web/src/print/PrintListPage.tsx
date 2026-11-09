// The location sheet `#/print/list/:id` (plan §4.7): the list title, the date line, then one
// line per approved daily tip on the list, in list order: a 10pt checkbox, the name in bold,
// then the action. Screen-only warnings say when the sheet spills onto a second page (more
// than PRINT_CAPACITY rows) and which rows are too long for one line. Data: GET
// /api/print/lists/{id}.
import { useEffect, useRef, useState } from "react";
import { apiGet, errorText } from "../api";
import type { Meta } from "../meta";
import type { LocationSheet } from "../model";
import {
  OverflowWarning,
  PrintControls,
  PrintRow,
  SheetHeader,
  SheetWarning,
  useDocumentTitle,
  useRowOverflow,
} from "./PrintSheet";

type SheetState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; sheet: LocationSheet };

/** The screen warning of a sheet with more rows than one A4 page holds. */
export function spillMessage(rows: number, capacity: number): string {
  return `This sheet has ${rows} rows, but one A4 page holds ${capacity}, so it spills onto a second page. Take some tips off this list before printing.`;
}

function Sheet({ sheet, meta }: { sheet: LocationSheet; meta: Meta }) {
  const ref = useRef<HTMLElement>(null);
  const { overflowing, measured } = useRowOverflow(ref, sheet.items);
  return (
    <>
      {sheet.over_capacity && (
        <SheetWarning kind="capacity">
          <p>{spillMessage(sheet.items.length, meta.print_capacity)}</p>
        </SheetWarning>
      )}
      <OverflowWarning items={sheet.items} overflowing={overflowing} />
      <div className="print-preview">
        <article ref={ref} className="print-sheet" aria-label={sheet.title} data-measured={measured ? "true" : "false"}>
          <SheetHeader title={sheet.title} generatedAt={sheet.generated_at} />
          {sheet.items.length === 0 ? (
            <p className="print-empty screen-only">
              Nothing to print yet: only approved daily-checklist tips on this list print.
            </p>
          ) : (
            <ul className="print-rows" aria-label="Checklist">
              {sheet.items.map((item, row) => (
                <PrintRow
                  key={row}
                  item={item}
                  row={row}
                  separator={meta.limits.LINE_SEPARATOR}
                  checkbox
                  overflowing={overflowing.has(row)}
                />
              ))}
            </ul>
          )}
        </article>
      </div>
    </>
  );
}

export function PrintListPage({ listId, meta }: { listId: number; meta: Meta | null }) {
  const [state, setState] = useState<SheetState>({ status: "loading" });

  useEffect(() => {
    let active = true;
    setState({ status: "loading" });
    apiGet<LocationSheet>(`/print/lists/${listId}`).then(
      (sheet) => {
        if (!active) return;
        if (!Array.isArray(sheet?.items)) {
          setState({ status: "error", message: "the server sent an unexpected reply" });
          return;
        }
        setState({ status: "ready", sheet });
      },
      (error: unknown) => {
        if (active) setState({ status: "error", message: errorText(error) });
      },
    );
    return () => {
      active = false;
    };
  }, [listId]);

  useDocumentTitle(state.status === "ready" ? state.sheet.title : null);

  return (
    <section className="print-page" aria-label="Print preview">
      <PrintControls />
      {(state.status === "loading" || (state.status === "ready" && meta === null)) && (
        <p className="screen-only">Loading the sheet…</p>
      )}
      {state.status === "error" && (
        <p role="alert" className="app-alert screen-only">
          Cannot load the sheet of list #{listId}: {state.message}
        </p>
      )}
      {state.status === "ready" && meta !== null && <Sheet sheet={state.sheet} meta={meta} />}
    </section>
  );
}
