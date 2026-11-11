// The Lists page (plan §4.1, §4.7, §5; Steps 7 and 11): every checklist with its cue, its tips in
// print order and a link to its A4 sheet (#/print/list/<id>), a link to the activities sheet, and
// the "Not on any list yet" panel of approved daily tips that will not print until they are
// placed. Lists are managed here: create one (place, audience, cue), rename it, change its
// audience (a kids list keeps only kids+ tips, so the server takes adults tips off and reports
// them), edit its cue (which the skill reads on its next ingest), move it up or down, or delete
// it; and add, remove and reorder the tips on it. Each list warns when its sheet spills onto a
// second page (the server's over_capacity) and names the tips too long for one printed line,
// measured at the printed width with the print sheets' own row measurement (PrintSheet.tsx).
// Data: GET /api/lists, /api/lists/{id}/items, /api/tips?unplaced=true and /api/tips?bucket=1;
// changes: POST /api/lists, PATCH and DELETE /api/lists/{id}, PUT /api/lists/{id}/items. After
// each change the page reloads these, so what it shows is what the server stored.
import { useCallback, useEffect, useRef, useState } from "react";
import { apiGet, apiSend, errorText } from "../api";
import type { Meta } from "../meta";
import type { ListInfo, ListItem, ListPatch, ListPatchResult, NewList, Tip } from "../model";
import { audienceLabel, compatible, textLength } from "../model";
import { PrintRow, useRowOverflow } from "../print/PrintSheet";
import { href, PRINT_ACTIVITIES_HREF, printListHref } from "../router";

/** What the page shows: the lists in order, their items, the unplaced tips and every daily tip. */
export interface ListsData {
  lists: ListInfo[];
  items: ReadonlyMap<number, ListItem[]>;
  unplaced: Tip[];
  daily: Tip[];
}

type IndexState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; data: ListsData };

/** A list's tips section starts open when it holds at most this many tips. */
const OPEN_UP_TO = 8;

const NO_ITEMS: ListItem[] = [];

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

/** A list's display title, as the server builds it: `Coffee Machine — Adults`. */
export function listTitle(place: string, audience: string, meta: Meta): string {
  return `${place}${meta.limits.LINE_SEPARATOR}${audienceLabel(audience)}`;
}

/** The editable fields of a list, as the form holds them. */
export interface ListDraft {
  place: string;
  audience: string;
  cue: string;
}

/** The empty "New list" form: a list for adults (the audience that takes every tip). */
export function blankListDraft(meta: Meta): ListDraft {
  return { place: "", audience: meta.limits.AUDIENCE_ADULTS, cue: "" };
}

export function listDraftOf(list: ListInfo): ListDraft {
  return { place: list.place, audience: list.audience, cue: list.cue };
}

/** The `POST /api/lists` body for `draft`, trimmed. */
export function newListOf(draft: ListDraft): NewList {
  return { place: draft.place.trim(), audience: draft.audience, cue: draft.cue.trim() };
}

/** The `PATCH /api/lists/{id}` body that turns `list` into `draft`: only the changed fields, trimmed. */
export function listPatchOf(list: ListInfo, draft: ListDraft): ListPatch {
  const patch: ListPatch = {};
  const place = draft.place.trim();
  const cue = draft.cue.trim();
  if (place !== list.place) patch.place = place;
  if (draft.audience !== list.audience) patch.audience = draft.audience;
  if (cue !== list.cue) patch.cue = cue;
  return patch;
}

/**
 * Why the draft cannot be saved yet, or null when it can. `listId` is the list being edited
 * (null for a new one); another list with the same place and audience is a clash.
 */
export function listDraftProblem(
  draft: ListDraft,
  lists: readonly ListInfo[],
  meta: Meta,
  listId: number | null,
): string | null {
  const { PLACE_MAX, CUE_MAX } = meta.limits;
  const place = draft.place.trim();
  const cue = draft.cue.trim();
  if (place === "") return "The place is empty.";
  if (textLength(place) > PLACE_MAX) return `The place is ${textLength(place)} characters; ${PLACE_MAX} fit.`;
  if (textLength(cue) > CUE_MAX) return `The cue is ${textLength(cue)} characters; ${CUE_MAX} fit.`;
  const clash = lists.find((list) => list.id !== listId && list.place === place && list.audience === draft.audience);
  if (clash !== undefined) return `There is already a list ${clash.title}.`;
  return null;
}

/** The tips on a list that a list of `audience` refuses: the server takes them off when it changes. */
export function refusedBy(items: readonly ListItem[], audience: string, meta: Meta): ListItem[] {
  return items.filter((item) => !compatible(item.bucket, item.audience, audience, meta));
}

/** `ids` with the entry at `index` moved one place up (`-1`) or down (`1`). */
export function moved<T>(ids: readonly T[], index: number, delta: -1 | 1): T[] {
  const target = index + delta;
  const next = [...ids];
  const here = next[index];
  const there = next[target];
  if (here === undefined || there === undefined) return next;
  next[index] = there;
  next[target] = here;
  return next;
}

/**
 * The `PATCH /api/lists/{id}` bodies that store `order` as the page order: each list gets
 * `sort_order` = its place in `order` (1 = first); lists already there are left alone.
 */
export function orderPatches(order: readonly ListInfo[]): { id: number; sort_order: number }[] {
  return order.flatMap((list, index) => (list.sort_order === index + 1 ? [] : [{ id: list.id, sort_order: index + 1 }]));
}

/**
 * The daily tips that may be added to `list`: those it accepts (a kids list takes only kids+ tips)
 * that are not on it yet, split into tips on no list and tips on other lists, each by id.
 */
export function addChoices(
  list: ListInfo,
  items: readonly ListItem[],
  daily: readonly Tip[],
  meta: Meta,
): { unplaced: Tip[]; elsewhere: Tip[] } {
  const onList = new Set(items.map((item) => item.tip_id));
  const fits = daily.filter((tip) => !onList.has(tip.id) && compatible(tip.bucket, tip.audience, list.audience, meta));
  return {
    unplaced: fits.filter((tip) => tip.list_ids.length === 0),
    elsewhere: fits.filter((tip) => tip.list_ids.length > 0),
  };
}

/** Why a kids list refuses some tips: "a kids list takes only kids+ tips". */
export function kidsRule(meta: Meta): string {
  return `a ${meta.limits.AUDIENCE_KIDS} list takes only ${meta.limits.AUDIENCE_KIDS_PLUS} tips`;
}

/** Tip names as a sentence lists them: “A”, “B”. */
function quoted(names: readonly string[]): string {
  return names.map((name) => `“${name}”`).join(", ");
}

/** "1 tip is too long … : A." / "2 tips are too long … : A, B." */
export function overlongMessage(names: readonly string[]): string {
  const count = names.length;
  const one = count === 1;
  return `${one ? "1 tip is" : `${count} tips are`} too long for one printed line, so ${one ? "its" : "their"} end would be cut off: ${names.join(", ")}.`;
}

async function loadIndex(dailyBucket: number): Promise<ListsData | null> {
  const [lists, unplaced, daily] = await Promise.all([
    apiGet<ListInfo[]>("/lists"),
    apiGet<Tip[]>("/tips?unplaced=true"),
    apiGet<Tip[]>(`/tips?bucket=${dailyBucket}`),
  ]);
  if (!Array.isArray(lists) || !Array.isArray(unplaced) || !Array.isArray(daily)) return null;
  const items = await Promise.all(lists.map((list) => apiGet<ListItem[]>(`/lists/${list.id}/items`)));
  if (!items.every((entry) => Array.isArray(entry))) return null;
  return {
    lists,
    items: new Map(lists.map((list, index) => [list.id, items[index] ?? []])),
    unplaced,
    daily,
  };
}

const UNEXPECTED = "the server sent an unexpected reply";

export function ListsPage({ meta }: { meta: Meta | null }) {
  const [state, setState] = useState<IndexState>({ status: "loading" });
  const [announcement, setAnnouncement] = useState("");
  const [pageError, setPageError] = useState<string | null>(null);
  const [ordering, setOrdering] = useState(false);
  const loadToken = useRef(0);
  const dailyBucket = meta?.limits.BUCKET_DAILY ?? null;

  /** Load everything again; a newer reload wins, and a failed one keeps the data already shown. */
  const reload = useCallback(async () => {
    if (dailyBucket === null) return;
    const token = ++loadToken.current;
    try {
      const data = await loadIndex(dailyBucket);
      if (token !== loadToken.current) return;
      if (data === null) {
        setState((current) => (current.status === "ready" ? current : { status: "error", message: UNEXPECTED }));
        setPageError(`Cannot reload the lists: ${UNEXPECTED}`);
        return;
      }
      setState({ status: "ready", data });
    } catch (error) {
      if (token !== loadToken.current) return;
      const message = errorText(error);
      setState((current) => (current.status === "ready" ? current : { status: "error", message }));
      setPageError(`Cannot reload the lists: ${message}`);
    }
  }, [dailyBucket]);

  useEffect(() => {
    void reload();
    return () => {
      // A reload that lands after the page is gone is dropped.
      loadToken.current += 1;
    };
  }, [reload]);

  /** After a change: reload, then say what changed. */
  const changed = useCallback(
    async (message: string) => {
      setPageError(null);
      await reload();
      setAnnouncement(message);
    },
    [reload],
  );

  const moveList = async (lists: readonly ListInfo[], index: number, delta: -1 | 1) => {
    const list = lists[index];
    if (list === undefined) return;
    setOrdering(true);
    setPageError(null);
    try {
      for (const { id, sort_order } of orderPatches(moved(lists, index, delta))) {
        await apiSend<ListPatchResult>("PATCH", `/lists/${id}`, { sort_order });
      }
      await changed(`Moved ${list.title} ${delta < 0 ? "up" : "down"}.`);
    } catch (error) {
      await reload();
      setPageError(`Could not move ${list.title}: ${errorText(error)}`);
    } finally {
      setOrdering(false);
    }
  };

  return (
    <section className="lists">
      <h1>Lists</h1>
      <p className="lede">
        The checklist for each room and audience, ready to print. Only approved tips print. Each list's cue says when you
        are there; the skill reads it to place new daily tips.
      </p>
      <p className="lists-activities">
        <a href={PRINT_ACTIVITIES_HREF}>Print the activities sheet</a>: the approved occasional activities, for
        Kids+ and Adults.
      </p>
      <p role="status" className="review-announcement">
        {announcement}
      </p>
      {meta === null && <p>Loading the app settings…</p>}
      {meta !== null && state.status === "loading" && <p>Loading the lists…</p>}
      {state.status === "error" && (
        <p role="alert" className="app-alert">
          Cannot load the lists: {state.message}
        </p>
      )}
      {pageError !== null && state.status === "ready" && (
        <p role="alert" className="app-alert">
          {pageError}
        </p>
      )}
      {meta !== null && state.status === "ready" && (
        <>
          <NewListPanel lists={state.data.lists} meta={meta} onChanged={changed} />
          <ul className="list-cards" aria-label="Lists">
            {state.data.lists.map((list, index, lists) => (
              <ListCard
                key={list.id}
                list={list}
                items={state.data.items.get(list.id) ?? NO_ITEMS}
                lists={lists}
                daily={state.data.daily}
                meta={meta}
                first={index === 0}
                last={index === lists.length - 1}
                ordering={ordering}
                onMove={(delta) => moveList(lists, index, delta)}
                onChanged={changed}
              />
            ))}
          </ul>
          <UnplacedPanel tips={state.data.unplaced} meta={meta} />
        </>
      )}
    </section>
  );
}

interface ListFieldsProps {
  idPrefix: string;
  draft: ListDraft;
  onChange: (draft: ListDraft) => void;
  meta: Meta;
  disabled: boolean;
}

/** Place, audience and cue, each with its limit from /api/meta. */
function ListFields({ idPrefix, draft, onChange, meta, disabled }: ListFieldsProps) {
  const { PLACE_MAX, CUE_MAX, AUDIENCES_LIST } = meta.limits;
  const field = (name: string) => `${idPrefix}-${name}`;
  const set = (change: Partial<ListDraft>) => onChange({ ...draft, ...change });
  return (
    <div className="list-fields">
      <div className="field">
        <label htmlFor={field("place")}>Place</label>
        <input
          id={field("place")}
          value={draft.place}
          maxLength={PLACE_MAX}
          onChange={(event) => set({ place: event.target.value })}
          disabled={disabled}
        />
        <span className="field-count">
          {textLength(draft.place)} / {PLACE_MAX}
        </span>
      </div>
      <div className="field">
        <label htmlFor={field("audience")}>Audience</label>
        <select
          id={field("audience")}
          value={draft.audience}
          onChange={(event) => set({ audience: event.target.value })}
          disabled={disabled}
        >
          {AUDIENCES_LIST.map((audience) => (
            <option key={audience} value={audience}>
              {audienceLabel(audience)}
            </option>
          ))}
        </select>
      </div>
      <div className="field">
        <label htmlFor={field("cue")}>Cue</label>
        <input
          id={field("cue")}
          value={draft.cue}
          maxLength={CUE_MAX}
          placeholder="When are you here? For example: leaving the house or coming home"
          onChange={(event) => set({ cue: event.target.value })}
          disabled={disabled}
        />
        <span className="field-count">
          {textLength(draft.cue)} / {CUE_MAX}
        </span>
      </div>
      <p className="hint">
        A daily tip goes on the list whose cue matches where you are when it is due. A kids list takes only kids+
        tips.
      </p>
    </div>
  );
}

interface NewListPanelProps {
  lists: readonly ListInfo[];
  meta: Meta;
  onChanged: (message: string) => Promise<void>;
}

/** "New list": a place, an audience and a cue; the list goes after every other one. */
function NewListPanel({ lists, meta, onChanged }: NewListPanelProps) {
  const [draft, setDraft] = useState<ListDraft | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const problem = draft === null ? null : listDraftProblem(draft, lists, meta, null);

  if (draft === null) {
    return (
      <div className="add-tip-closed">
        <button
          type="button"
          className="primary"
          onClick={() => {
            setDraft(blankListDraft(meta));
            setError(null);
          }}
        >
          New list
        </button>
      </div>
    );
  }

  const create = async () => {
    if (problem !== null) return;
    setBusy(true);
    setError(null);
    try {
      const list = await apiSend<ListInfo>("POST", "/lists", newListOf(draft));
      setDraft(null);
      await onChanged(
        `Created ${list.title}. Add daily tips to it below; the skill reads its cue on the next ingest.`,
      );
    } catch (caught) {
      setError(`Could not create the list: ${errorText(caught)}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="add-tip new-list" aria-labelledby="new-list-heading">
      <h2 id="new-list-heading">New list</h2>
      <ListFields idPrefix="new-list" draft={draft} onChange={setDraft} meta={meta} disabled={busy} />
      {problem !== null && <p className="hint">{problem}</p>}
      {error !== null && (
        <p role="alert" className="app-alert">
          {error}
        </p>
      )}
      <div className="tip-actions">
        <button type="button" className="primary" onClick={create} disabled={busy || problem !== null}>
          Create list
        </button>
        <button type="button" onClick={() => setDraft(null)} disabled={busy}>
          Cancel
        </button>
      </div>
    </section>
  );
}

interface ListCardProps {
  list: ListInfo;
  items: ListItem[];
  lists: readonly ListInfo[];
  daily: readonly Tip[];
  meta: Meta;
  first: boolean;
  last: boolean;
  ordering: boolean;
  onMove: (delta: -1 | 1) => Promise<void>;
  onChanged: (message: string) => Promise<void>;
}

function ListCard({ list, items, lists, daily, meta, first, last, ordering, onMove, onChanged }: ListCardProps) {
  const [draft, setDraft] = useState<ListDraft | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [choice, setChoice] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [startsOpen] = useState(() => items.length <= OPEN_UP_TO);
  const measureRef = useRef<HTMLElement>(null);
  const { overflowing, measured } = useRowOverflow(measureRef, items);
  const { limits } = meta;
  const sep = limits.LINE_SEPARATOR;
  const printed = printedItems(items, meta);
  const waiting = items.length - printed.length;
  const overlong = items.filter((_, row) => overflowing.has(row));
  const ids = items.map((item) => item.tip_id);
  const choices = addChoices(list, items, daily, meta);
  const noChoice = choices.unplaced.length === 0 && choices.elsewhere.length === 0;
  const field = (name: string) => `list-${list.id}-${name}`;
  const locked = busy || ordering;
  const problem = draft === null ? null : listDraftProblem(draft, lists, meta, list.id);
  const patch = draft === null ? {} : listPatchOf(list, draft);
  const refused = draft === null || draft.audience === list.audience ? [] : refusedBy(items, draft.audience, meta);

  /** Run `work` with the card busy; a failure is shown on the card. True when it succeeded. */
  const run = async (failure: string, work: () => Promise<void>): Promise<boolean> => {
    setBusy(true);
    setError(null);
    try {
      await work();
      return true;
    } catch (caught) {
      setError(`${failure}: ${errorText(caught)}`);
      return false;
    } finally {
      setBusy(false);
    }
  };

  const putItems = (tipIds: number[], failure: string, message: string) =>
    run(failure, async () => {
      await apiSend<ListItem[]>("PUT", `/lists/${list.id}/items`, { tip_ids: tipIds });
      await onChanged(message);
    });

  const add = async () => {
    const tip = daily.find((item) => String(item.id) === choice);
    if (tip === undefined) return;
    if (await putItems([...ids, tip.id], "Could not add the tip", `Added “${tip.name}” to ${list.title}.`)) {
      setChoice("");
    }
  };

  const remove = (item: ListItem) => {
    const tip = daily.find((entry) => entry.id === item.tip_id);
    const nowhere =
      tip !== undefined && tip.list_ids.length === 1 && tip.status === limits.TIP_STATUS_APPROVED
        ? " It is now under “Not on any list yet”."
        : "";
    return putItems(
      ids.filter((id) => id !== item.tip_id),
      "Could not take the tip off",
      `Took “${item.name}” off ${list.title}.${nowhere}`,
    );
  };

  const save = () =>
    run("Could not save the list", async () => {
      if (draft === null || problem !== null) return;
      if (Object.keys(patch).length > 0) {
        const result = await apiSend<ListPatchResult>("PATCH", `/lists/${list.id}`, patch);
        const names = result.removed_tip_ids.map(
          (id) => items.find((item) => item.tip_id === id)?.name ?? `#${id}`,
        );
        const removed =
          names.length > 0
            ? ` Took ${plural(names.length, "tip", "tips")} off it, since ${kidsRule(meta)}: ${quoted(names)}.`
            : "";
        const cue = patch.cue !== undefined ? " The skill reads the new cue on its next ingest." : "";
        await onChanged(`Saved ${result.list.title}.${removed}${cue}`);
      }
      setDraft(null);
    });

  const destroy = () =>
    run("Could not delete the list", async () => {
      await apiSend<undefined>("DELETE", `/lists/${list.id}`);
      await onChanged(`Deleted the list ${list.title}. Its tips are still in the Library.`);
    });

  return (
    <li className="list-card" aria-label={list.title} data-measured={measured ? "true" : "false"}>
      <div className="list-card-header">
        <h2>{list.title}</h2>
        <a className="list-print" href={printListHref(list.id)} aria-label={`Print ${list.title}`}>
          Print sheet
        </a>
      </div>
      {list.cue !== "" ? (
        <p className="list-cue">When: {list.cue}</p>
      ) : (
        <p className="list-cue list-cue-missing">No cue yet: edit the list to say when you are here.</p>
      )}
      <p className="list-count">
        {`${plural(printed.length, "tip prints", "tips print")}${waiting > 0 ? `; ${waiting} more waiting for review` : ""}.`}
      </p>
      {list.over_capacity && (
        <p className="list-warning" role="note" data-warning="capacity">
          More than {meta.print_capacity} tips: the sheet spills onto a second page.
        </p>
      )}
      {overlong.length > 0 && (
        <p className="list-warning" role="note" data-warning="overflow">
          {overlongMessage(overlong.map((item) => item.name))} Shorten {overlong.length === 1 ? "it" : "them"} in the{" "}
          <a href={href("library")}>Library</a> before printing.
        </p>
      )}
      {error !== null && (
        <p role="alert" className="app-alert">
          {error}
        </p>
      )}

      {draft !== null ? (
        <div className="list-edit" role="group" aria-label={`Edit ${list.title}`}>
          <ListFields idPrefix={field("edit")} draft={draft} onChange={setDraft} meta={meta} disabled={busy} />
          {refused.length > 0 && (
            <p className="list-warning" role="note" data-warning="audience">
              {`Saving takes ${refused.length === 1 ? "this tip" : `these ${refused.length} tips`} off the list, since ${kidsRule(meta)}: ${quoted(refused.map((item) => item.name))}.`}
            </p>
          )}
          {problem !== null && <p className="hint">{problem}</p>}
          <div className="tip-actions">
            <button
              type="button"
              className="primary"
              onClick={save}
              disabled={busy || problem !== null || Object.keys(patch).length === 0}
            >
              Save
            </button>
            <button type="button" onClick={() => setDraft(null)} disabled={busy}>
              Cancel
            </button>
          </div>
        </div>
      ) : confirming ? (
        <div className="confirm-delete" role="group" aria-label={`Delete ${list.title}`}>
          <p>
            Delete the list {list.title} for good? Its tips stay in the Library; any that are on no other list show
            under “Not on any list yet”.
          </p>
          <div className="tip-actions">
            <button type="button" className="danger" onClick={destroy} disabled={busy}>
              Delete for good
            </button>
            <button type="button" onClick={() => setConfirming(false)} disabled={busy}>
              Keep it
            </button>
          </div>
        </div>
      ) : (
        <div className="tip-actions list-actions">
          <button type="button" onClick={() => void onMove(-1)} disabled={locked || first}>
            Move list up
          </button>
          <button type="button" onClick={() => void onMove(1)} disabled={locked || last}>
            Move list down
          </button>
          <button
            type="button"
            onClick={() => {
              setDraft(listDraftOf(list));
              setError(null);
            }}
            disabled={locked}
          >
            Edit list
          </button>
          <button type="button" className="danger" onClick={() => setConfirming(true)} disabled={locked}>
            Delete list
          </button>
        </div>
      )}

      <details className="list-items" open={startsOpen}>
        <summary>{items.length === 0 ? "No tips yet: add some" : `Show the ${plural(items.length, "tip", "tips")}`}</summary>
        {items.length > 0 && (
          <ol className="list-item-rows" aria-label={`Tips on ${list.title}`}>
            {items.map((item, row) => {
              const cut = overflowing.has(row);
              return (
                <li key={item.tip_id} className="list-item" data-overflow={cut ? "true" : "false"}>
                  <span className="list-item-text">
                    <b>{item.name}</b>
                    {sep}
                    {item.action}
                  </span>
                  {item.status !== limits.TIP_STATUS_APPROVED && (
                    <span className="badge list-pending">waiting for review</span>
                  )}
                  {cut && <span className="badge list-overlong">too long for one line</span>}
                  <span className="list-item-controls">
                    <button
                      type="button"
                      aria-label={`Move “${item.name}” up`}
                      onClick={() =>
                        putItems(moved(ids, row, -1), "Could not move the tip", `Moved “${item.name}” up on ${list.title}.`)
                      }
                      disabled={locked || row === 0}
                    >
                      ↑
                    </button>
                    <button
                      type="button"
                      aria-label={`Move “${item.name}” down`}
                      onClick={() =>
                        putItems(
                          moved(ids, row, 1),
                          "Could not move the tip",
                          `Moved “${item.name}” down on ${list.title}.`,
                        )
                      }
                      disabled={locked || row === items.length - 1}
                    >
                      ↓
                    </button>
                    <button type="button" aria-label={`Remove “${item.name}”`} onClick={() => remove(item)} disabled={locked}>
                      Remove
                    </button>
                  </span>
                </li>
              );
            })}
          </ol>
        )}
        <div className="list-add">
          <label htmlFor={field("add")}>Add a tip</label>
          <select id={field("add")} value={choice} onChange={(event) => setChoice(event.target.value)} disabled={locked || noChoice}>
            <option value="">{noChoice ? "No other daily tip fits this list" : "Choose a daily tip…"}</option>
            {choices.unplaced.length > 0 && (
              <optgroup label="Not on any list yet">
                {choices.unplaced.map((tip) => (
                  <TipOption key={tip.id} tip={tip} meta={meta} />
                ))}
              </optgroup>
            )}
            {choices.elsewhere.length > 0 && (
              <optgroup label="On other lists">
                {choices.elsewhere.map((tip) => (
                  <TipOption key={tip.id} tip={tip} meta={meta} />
                ))}
              </optgroup>
            )}
          </select>
          <button type="button" onClick={add} disabled={locked || choice === ""}>
            Add to list
          </button>
        </div>
      </details>

      {/* The rows as the A4 sheet lays them out (same markup and print.css), off screen, so
          useRowOverflow can measure which ones would be cut. Every tip on the list is
          measured: a pending one prints once it is approved. */}
      <div className="list-measure screen-only" aria-hidden="true">
        <article ref={measureRef} className="print-sheet">
          <ul className="print-rows">
            {items.map((item, row) => (
              <PrintRow key={item.tip_id} item={item} row={row} separator={sep} checkbox overflowing={false} />
            ))}
          </ul>
        </article>
      </div>
    </li>
  );
}

function TipOption({ tip, meta }: { tip: Tip; meta: Meta }) {
  const pending = tip.status === meta.limits.TIP_STATUS_APPROVED ? "" : " (waiting for review)";
  return (
    <option value={tip.id} title={tip.action}>
      {`#${tip.id} ${tip.name}${pending}`}
    </option>
  );
}

function UnplacedPanel({ tips, meta }: { tips: readonly Tip[]; meta: Meta }) {
  const sep = meta.limits.LINE_SEPARATOR;
  return (
    <section className="unplaced" aria-labelledby="unplaced-heading">
      <h2 id="unplaced-heading">Not on any list yet</h2>
      <p className="lede">
        Approved daily tips print only once they are on a list. Put one on a list with “Add a tip” on that list.
      </p>
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
