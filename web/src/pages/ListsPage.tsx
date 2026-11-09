// The Lists index (plan §5, Step 7), read only: every list with its cue, its tips in print
// order and a link to its A4 sheet (#/print/list/<id>), a link to the activities sheet, and
// the "Not on any list yet" panel of approved daily tips that will not print until they are
// placed. List editing comes later (Step 11). Data: GET /api/lists, GET /api/lists/{id}/items
// and GET /api/tips?unplaced=true.
import { useEffect, useState } from "react";
import { apiGet, errorText } from "../api";
import type { Meta } from "../meta";
import type { ListInfo, ListItem, Tip } from "../model";
import { PRINT_ACTIVITIES_HREF, printListHref } from "../router";

type IndexState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; lists: ListInfo[]; items: ReadonlyMap<number, ListItem[]>; unplaced: Tip[] };

function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`;
}

/** The items of a list that print on its sheet: approved daily tips (the server's rule). */
export function printedItems(items: readonly ListItem[], meta: Meta | null): ListItem[] {
  if (meta === null) return [];
  return items.filter(
    (item) => item.status === meta.limits.TIP_STATUS_APPROVED && item.bucket === meta.limits.BUCKET_DAILY,
  );
}

async function loadIndex(): Promise<IndexState> {
  const [lists, unplaced] = await Promise.all([apiGet<ListInfo[]>("/lists"), apiGet<Tip[]>("/tips?unplaced=true")]);
  if (!Array.isArray(lists) || !Array.isArray(unplaced)) {
    return { status: "error", message: "the server sent an unexpected reply" };
  }
  const items = await Promise.all(lists.map((list) => apiGet<ListItem[]>(`/lists/${list.id}/items`)));
  if (!items.every((entry) => Array.isArray(entry))) {
    return { status: "error", message: "the server sent an unexpected reply" };
  }
  return {
    status: "ready",
    lists,
    items: new Map(lists.map((list, index) => [list.id, items[index] ?? []])),
    unplaced,
  };
}

function ListCard({ list, items, meta }: { list: ListInfo; items: readonly ListItem[]; meta: Meta | null }) {
  const printed = printedItems(items, meta);
  const waiting = items.length - printed.length;
  const sep = meta?.limits.LINE_SEPARATOR ?? " ";
  return (
    <li className="list-card" aria-label={list.title}>
      <div className="list-card-header">
        <h2>{list.title}</h2>
        <a className="list-print" href={printListHref(list.id)} aria-label={`Print ${list.title}`}>
          Print sheet
        </a>
      </div>
      {list.cue !== "" && <p className="list-cue">When: {list.cue}</p>}
      <p className="list-count">
        {meta === null
          ? plural(list.item_count, "tip", "tips")
          : `${plural(printed.length, "tip prints", "tips print")}${waiting > 0 ? `; ${waiting} more waiting for review` : ""}.`}
      </p>
      {list.over_capacity && (
        <p className="list-warning" role="note">
          {meta === null
            ? "Too many tips for one A4 page: the sheet spills onto a second page."
            : `More than ${meta.print_capacity} tips: the sheet spills onto a second page.`}
        </p>
      )}
      {items.length > 0 && (
        <details className="list-items">
          <summary>Show the {plural(items.length, "tip", "tips")}</summary>
          <ol>
            {items.map((item) => (
              <li key={item.tip_id}>
                <b>{item.name}</b>
                {sep}
                {item.action}
                {meta !== null && item.status !== meta.limits.TIP_STATUS_APPROVED && (
                  <span className="badge list-pending">waiting for review</span>
                )}
              </li>
            ))}
          </ol>
        </details>
      )}
    </li>
  );
}

function UnplacedPanel({ tips, meta }: { tips: readonly Tip[]; meta: Meta | null }) {
  const sep = meta?.limits.LINE_SEPARATOR ?? " ";
  return (
    <section className="unplaced" aria-labelledby="unplaced-heading">
      <h2 id="unplaced-heading">Not on any list yet</h2>
      <p className="lede">Approved daily tips print only once they are on a list.</p>
      {tips.length === 0 ? (
        <p>Every approved daily tip is on a list.</p>
      ) : (
        <ul aria-label="Not on any list yet">
          {tips.map((tip) => (
            <li key={tip.id}>
              <b>{tip.name}</b>
              {sep}
              {tip.action}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export function ListsPage({ meta }: { meta: Meta | null }) {
  const [state, setState] = useState<IndexState>({ status: "loading" });

  useEffect(() => {
    let active = true;
    loadIndex().then(
      (loaded) => {
        if (active) setState(loaded);
      },
      (error: unknown) => {
        if (active) setState({ status: "error", message: errorText(error) });
      },
    );
    return () => {
      active = false;
    };
  }, []);

  return (
    <section className="lists">
      <h1>Lists</h1>
      <p className="lede">The checklist for each room and audience, ready to print. Only approved tips print.</p>
      <p className="lists-activities">
        <a href={PRINT_ACTIVITIES_HREF}>Print the activities sheet</a>: the approved occasional activities, for
        Kids+ and Adults.
      </p>
      {state.status === "loading" && <p>Loading the lists…</p>}
      {state.status === "error" && (
        <p role="alert" className="app-alert">
          Cannot load the lists: {state.message}
        </p>
      )}
      {state.status === "ready" && (
        <>
          <ul className="list-cards" aria-label="Lists">
            {state.lists.map((list) => (
              <ListCard key={list.id} list={list} items={state.items.get(list.id) ?? []} meta={meta} />
            ))}
          </ul>
          <UnplacedPanel tips={state.unplaced} meta={meta} />
        </>
      )}
    </section>
  );
}
