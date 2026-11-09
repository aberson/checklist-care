// The activities sheet `#/print/activities` (plan §4.7): the header "Good to do now and then",
// then a Kids+ group and an Adults group of the approved occasional (bucket-2) tips, one line
// each in the location-sheet format but without checkboxes. Rows too long for one line are
// flagged as on the location sheet. Data: GET /api/print/activities.
import { useEffect, useMemo, useRef, useState } from "react";
import { apiGet, errorText } from "../api";
import type { Meta } from "../meta";
import type { ActivitiesSheet } from "../model";
import { audienceLabel } from "../model";
import { OverflowWarning, PrintControls, PrintRow, SheetHeader, useDocumentTitle, useRowOverflow } from "./PrintSheet";

export const ACTIVITIES_TITLE = "Good to do now and then";

type SheetState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; sheet: ActivitiesSheet };

function Sheet({ sheet, meta }: { sheet: ActivitiesSheet; meta: Meta }) {
  const ref = useRef<HTMLElement>(null);
  // Rows are numbered across the groups, so each row has its own measurement.
  const rows = useMemo(() => sheet.groups.flatMap((group) => group.items), [sheet]);
  const { overflowing, measured } = useRowOverflow(ref, rows);
  let row = 0;
  return (
    <>
      <OverflowWarning items={rows} overflowing={overflowing} />
      <div className="print-preview">
        <article ref={ref} className="print-sheet" aria-label={ACTIVITIES_TITLE} data-measured={measured ? "true" : "false"}>
          <SheetHeader title={ACTIVITIES_TITLE} generatedAt={sheet.generated_at} />
          {sheet.groups.map((group) => {
            const label = audienceLabel(group.audience);
            const headingId = `print-group-${group.audience.replace(/[^a-z]/g, "")}`;
            return (
              <section key={group.audience} className="print-group" aria-labelledby={headingId}>
                <h2 id={headingId} className="print-group-title">
                  {label}
                </h2>
                {group.items.length === 0 ? (
                  <p className="print-empty screen-only">No approved activities for {label} yet.</p>
                ) : (
                  <ul className="print-rows">
                    {group.items.map((item) => {
                      const index = row++;
                      return (
                        <PrintRow
                          key={index}
                          item={item}
                          row={index}
                          separator={meta.limits.LINE_SEPARATOR}
                          checkbox={false}
                          overflowing={overflowing.has(index)}
                        />
                      );
                    })}
                  </ul>
                )}
              </section>
            );
          })}
        </article>
      </div>
    </>
  );
}

export function PrintActivitiesPage({ meta }: { meta: Meta | null }) {
  const [state, setState] = useState<SheetState>({ status: "loading" });

  useEffect(() => {
    let active = true;
    apiGet<ActivitiesSheet>("/print/activities").then(
      (sheet) => {
        if (!active) return;
        if (!Array.isArray(sheet?.groups)) {
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
  }, []);

  useDocumentTitle(state.status === "ready" ? ACTIVITIES_TITLE : null);

  return (
    <section className="print-page" aria-label="Print preview">
      <PrintControls />
      {(state.status === "loading" || (state.status === "ready" && meta === null)) && (
        <p className="screen-only">Loading the sheet…</p>
      )}
      {state.status === "error" && (
        <p role="alert" className="app-alert screen-only">
          Cannot load the activities sheet: {state.message}
        </p>
      )}
      {state.status === "ready" && meta !== null && <Sheet sheet={state.sheet} meta={meta} />}
    </section>
  );
}
