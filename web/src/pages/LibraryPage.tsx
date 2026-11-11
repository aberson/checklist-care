// The Library (plan §4.1, §5, Step 9): every stored tip, filtered by bucket, status, audience and
// source. A tip can be edited inline (TipEditor), moved to another bucket, or deleted for good
// (the only hard delete; the tips that repeat it keep a note naming it). "Add my own tip" stores
// a tip of your own, approved at once under the manual source and on the lists you tick, so a
// basic like "Brush your teeth" prints straight away. Bucket 3 is browsed only here; bucket 4
// (Left out) shows only under its own bucket filter. Data: GET /api/tips (the filters go to the
// server as query parameters), /api/sources and /api/lists; changes: POST /api/tips,
// PATCH /api/tips/{id} and DELETE /api/tips/{id}.
import { useCallback, useEffect, useState } from "react";
import { apiGet, apiSend, errorText } from "../api";
import { draftOf, draftProblem, keptListIds, patchNotice, patchOf, TipEditor } from "../components/TipEditor";
import type { TipDraft } from "../components/TipEditor";
import type { Meta } from "../meta";
import type { ListInfo, NewTip, PatchResult, SourceInfo, Tip, TipBrief } from "../model";
import { bucketTitle, clockTime } from "../model";
import { href } from "../router";

/** The Library's filters; null means "any". With no bucket, Left out tips stay hidden. */
export interface LibraryFilters {
  bucket: number | null;
  status: string | null;
  audience: string | null;
  sourceId: number | null;
}

export const NO_FILTERS: LibraryFilters = { bucket: null, status: null, audience: null, sourceId: null };

/** The `GET /api/tips` path for `filters`, without the `/api` prefix: `/tips?status=approved&bucket=1`. */
export function tipsPath(filters: LibraryFilters): string {
  const params = new URLSearchParams();
  if (filters.status !== null) params.set("status", filters.status);
  if (filters.bucket !== null) params.set("bucket", String(filters.bucket));
  if (filters.audience !== null) params.set("audience", filters.audience);
  if (filters.sourceId !== null) params.set("source_id", String(filters.sourceId));
  const query = params.toString();
  return query === "" ? "/tips" : `/tips?${query}`;
}

/**
 * Whether `tip` shows under `filters`: the server's filters, applied again so a tip that an
 * edit or a move takes out of the current view leaves it. Without a bucket filter, Left out
 * tips are hidden (plan §4.1: bucket 4 appears only under the Library's Left out filter).
 */
export function shownUnder(tip: Tip, filters: LibraryFilters, meta: Meta): boolean {
  if (filters.bucket === null ? tip.bucket === meta.limits.BUCKET_LEFT_OUT : tip.bucket !== filters.bucket) {
    return false;
  }
  if (filters.status !== null && tip.status !== filters.status) return false;
  if (filters.audience !== null && tip.audience !== filters.audience) return false;
  return filters.sourceId === null || tip.source_id === filters.sourceId;
}

/** A tip status as the page says it. */
export function statusLabel(status: string, meta: Meta): string {
  return status === meta.limits.TIP_STATUS_APPROVED ? "Approved" : "Waiting for review";
}

/** The empty "Add my own tip" form: a daily tip for adults (the safe audience, §4.1), on no list. */
export function blankDraft(meta: Meta): TipDraft {
  return {
    name: "",
    action: "",
    reason: "",
    bucket: meta.limits.BUCKET_DAILY,
    audience: meta.limits.AUDIENCE_ADULTS,
    listIds: [],
  };
}

/** The `POST /api/tips` body for `draft`; an empty reason is left out, and so is a list the tip may not go on. */
export function newTipOf(draft: TipDraft, lists: readonly ListInfo[], meta: Meta): NewTip {
  const reason = draft.reason.trim();
  return {
    name: draft.name.trim(),
    action: draft.action.trim(),
    ...(reason === "" ? {} : { reason }),
    bucket: draft.bucket,
    audience: draft.audience,
    list_ids: keptListIds(draft, lists, meta),
  };
}

function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`;
}

function listTitles(ids: readonly number[], lists: readonly ListInfo[]): string[] {
  return ids.map((id) => lists.find((list) => list.id === id)?.title ?? `list ${id}`);
}

function sourceName(sourceId: number, sources: readonly SourceInfo[], fallback: string): string {
  const source = sources.find((item) => item.id === sourceId);
  return source === undefined ? fallback : (source.title ?? source.ref);
}

const HIDDEN = " The current filters hide it.";

type RefsState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; sources: SourceInfo[]; lists: ListInfo[] };

export function LibraryPage({ meta }: { meta: Meta | null }) {
  const [refs, setRefs] = useState<RefsState>({ status: "loading" });
  const [filters, setFilters] = useState<LibraryFilters>(NO_FILTERS);
  const [reloads, setReloads] = useState(0);
  const [tips, setTips] = useState<Tip[] | null>(null);
  const [tipsError, setTipsError] = useState<string | null>(null);
  const [fetching, setFetching] = useState(true);
  const [announcement, setAnnouncement] = useState("");
  const path = tipsPath(filters);

  useEffect(() => {
    let active = true;
    Promise.all([apiGet<SourceInfo[]>("/sources"), apiGet<ListInfo[]>("/lists")]).then(
      ([sources, lists]) => {
        if (!active) return;
        if (!Array.isArray(sources) || !Array.isArray(lists)) {
          setRefs({ status: "error", message: "the server sent an unexpected reply" });
          return;
        }
        setRefs({ status: "ready", sources, lists });
      },
      (error: unknown) => {
        if (active) setRefs({ status: "error", message: errorText(error) });
      },
    );
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    let active = true;
    setFetching(true);
    apiGet<Tip[]>(path).then(
      (loaded) => {
        if (!active) return;
        if (Array.isArray(loaded)) {
          setTips(loaded);
          setTipsError(null);
        } else {
          setTipsError("the server sent an unexpected reply");
        }
        setFetching(false);
      },
      (error: unknown) => {
        if (!active) return;
        setTipsError(errorText(error));
        setFetching(false);
      },
    );
    return () => {
      active = false;
    };
  }, [path, reloads]);

  const hiddenNote = useCallback(
    (tip: Tip) => (meta !== null && !shownUnder(tip, filters, meta) ? HIDDEN : ""),
    [filters, meta],
  );

  const added = useCallback(
    (tip: Tip, lists: readonly ListInfo[]) => {
      setTips((current) => (current === null ? [tip] : [...current.filter((item) => item.id !== tip.id), tip]));
      const titles = listTitles(tip.list_ids, lists);
      const where =
        titles.length > 0
          ? ` It prints on ${titles.join(", ")}.`
          : tip.bucket === meta?.limits.BUCKET_DAILY
            ? " It is not on any list yet."
            : "";
      setAnnouncement(`Added “${tip.name}” to ${bucketTitle(tip.bucket, meta)}, approved.${where}${hiddenNote(tip)}`);
    },
    [meta, hiddenNote],
  );

  const saved = useCallback(
    (result: PatchResult, message: string, lists: readonly ListInfo[]) => {
      setTips((current) => (current ?? []).map((item) => (item.id === result.tip.id ? result.tip : item)));
      const extra = patchNotice(result, lists);
      setAnnouncement(`${message}${extra === null ? "" : ` ${extra}`}${hiddenNote(result.tip)}`);
    },
    [hiddenNote],
  );

  const deleted = useCallback((tip: Tip) => {
    setTips((current) => (current ?? []).filter((item) => item.id !== tip.id));
    setAnnouncement(`Deleted #${tip.id} “${tip.name}”.`);
    // The tips that repeated it now carry a note naming it: show the server's copy.
    setReloads((count) => count + 1);
  }, []);

  return (
    <section className="library">
      <h1>Library</h1>
      <p className="lede">
        Every stored tip, from every source and your own. Edit a tip, move it to another bucket, or delete it for good.
        Optional tips live only here; Left out tips show under the Left out bucket.
      </p>
      <p role="status" className="review-announcement">
        {announcement}
      </p>
      {meta === null && <p>Loading the app settings…</p>}
      {refs.status === "error" && (
        <p role="alert" className="app-alert">
          Cannot load the sources and lists: {refs.message}
        </p>
      )}
      {meta !== null && refs.status === "ready" && (
        <>
          <AddTipPanel lists={refs.lists} meta={meta} onAdded={(tip) => added(tip, refs.lists)} />
          <FilterBar filters={filters} onChange={setFilters} sources={refs.sources} meta={meta} />
          {tipsError !== null && (
            <p role="alert" className="app-alert">
              Cannot load the tips: {tipsError}
            </p>
          )}
          {tips === null ? (
            tipsError === null && <p>Loading the tips…</p>
          ) : (
            <TipList
              tips={tips.filter((tip) => shownUnder(tip, filters, meta))}
              busy={fetching}
              filters={filters}
              lists={refs.lists}
              sources={refs.sources}
              meta={meta}
              onSaved={(result, message) => saved(result, message, refs.lists)}
              onDeleted={deleted}
            />
          )}
        </>
      )}
    </section>
  );
}

interface FilterBarProps {
  filters: LibraryFilters;
  onChange: (filters: LibraryFilters) => void;
  sources: readonly SourceInfo[];
  meta: Meta;
}

function FilterBar({ filters, onChange, sources, meta }: FilterBarProps) {
  const { limits } = meta;
  const set = (change: Partial<LibraryFilters>) => onChange({ ...filters, ...change });
  const chosen = (value: string): string | null => (value === "" ? null : value);
  const number = (value: string): number | null => (value === "" ? null : Number(value));
  const any =
    filters.bucket !== null || filters.status !== null || filters.audience !== null || filters.sourceId !== null;
  return (
    <div className="library-filters" role="group" aria-label="Filters">
      <div className="filter">
        <label htmlFor="library-filter-bucket">Bucket</label>
        <select
          id="library-filter-bucket"
          value={filters.bucket ?? ""}
          onChange={(event) => set({ bucket: number(event.target.value) })}
        >
          <option value="">All but Left out</option>
          {meta.buckets.map((bucket) => (
            <option key={bucket.id} value={bucket.id}>
              {bucket.id}. {bucket.title}
            </option>
          ))}
        </select>
      </div>
      <div className="filter">
        <label htmlFor="library-filter-status">Status</label>
        <select
          id="library-filter-status"
          value={filters.status ?? ""}
          onChange={(event) => set({ status: chosen(event.target.value) })}
        >
          <option value="">Any status</option>
          {limits.TIP_STATUSES.map((status) => (
            <option key={status} value={status}>
              {statusLabel(status, meta)}
            </option>
          ))}
        </select>
      </div>
      <div className="filter">
        <label htmlFor="library-filter-audience">Audience</label>
        <select
          id="library-filter-audience"
          value={filters.audience ?? ""}
          onChange={(event) => set({ audience: chosen(event.target.value) })}
        >
          <option value="">Any audience</option>
          {limits.AUDIENCES_TIP.map((audience) => (
            <option key={audience} value={audience}>
              {audience}
            </option>
          ))}
        </select>
      </div>
      <div className="filter">
        <label htmlFor="library-filter-source">Source</label>
        <select
          id="library-filter-source"
          value={filters.sourceId ?? ""}
          onChange={(event) => set({ sourceId: number(event.target.value) })}
        >
          <option value="">Any source</option>
          {sources.map((source) => (
            <option key={source.id} value={source.id}>
              {source.title ?? source.ref}
            </option>
          ))}
        </select>
      </div>
      <button type="button" className="filter-clear" onClick={() => onChange(NO_FILTERS)} disabled={!any}>
        Clear filters
      </button>
    </div>
  );
}

interface AddTipPanelProps {
  lists: readonly ListInfo[];
  meta: Meta;
  onAdded: (tip: Tip) => void;
}

/** "Add my own tip": a TipEditor whose tip is stored approved, on the lists it ticks. */
function AddTipPanel({ lists, meta, onAdded }: AddTipPanelProps) {
  const [draft, setDraft] = useState<TipDraft | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const problem = draft === null ? null : draftProblem(draft, meta);

  if (draft === null) {
    return (
      <div className="add-tip-closed">
        <button
          type="button"
          className="primary"
          onClick={() => {
            setDraft(blankDraft(meta));
            setError(null);
          }}
        >
          Add my own tip
        </button>
      </div>
    );
  }

  const add = async () => {
    if (problem !== null) return;
    setBusy(true);
    setError(null);
    try {
      const tip = await apiSend<Tip>("POST", "/tips", newTipOf(draft, lists, meta));
      setDraft(null);
      onAdded(tip);
    } catch (caught) {
      setError(`Could not add the tip: ${errorText(caught)}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="add-tip" aria-labelledby="add-tip-heading">
      <h2 id="add-tip-heading">Add my own tip</h2>
      <p className="hint">
        Your own tips are approved at once: a daily tip prints on the lists you tick. “Why” is optional.
      </p>
      <TipEditor idPrefix="new-tip" draft={draft} onChange={setDraft} lists={lists} meta={meta} />
      {problem !== null && <p className="hint">{problem}</p>}
      {error !== null && (
        <p role="alert" className="app-alert">
          {error}
        </p>
      )}
      <div className="tip-actions">
        <button type="button" className="primary" onClick={add} disabled={busy || problem !== null}>
          Add tip
        </button>
        <button type="button" onClick={() => setDraft(null)} disabled={busy}>
          Cancel
        </button>
      </div>
    </section>
  );
}

interface TipListProps {
  tips: readonly Tip[];
  busy: boolean;
  filters: LibraryFilters;
  lists: readonly ListInfo[];
  sources: readonly SourceInfo[];
  meta: Meta;
  onSaved: (result: PatchResult, message: string) => void;
  onDeleted: (tip: Tip) => void;
}

function TipList({ tips, busy, filters, lists, sources, meta, onSaved, onDeleted }: TipListProps) {
  const leftOutHint =
    filters.bucket === null ? ` Choose the ${bucketTitle(meta.limits.BUCKET_LEFT_OUT, meta)} bucket to see those tips.` : "";
  return (
    <>
      <p className="library-summary">
        {tips.length === 0 ? "No tips match these filters." : `Showing ${plural(tips.length, "tip", "tips")}.`}
        {leftOutHint}
      </p>
      {tips.length > 0 && (
        <ol className="tip-cards library-tips" aria-label="Tips" aria-busy={busy}>
          {tips.map((tip) => (
            <LibraryCard
              key={tip.id}
              tip={tip}
              lists={lists}
              sources={sources}
              meta={meta}
              onSaved={onSaved}
              onDeleted={onDeleted}
            />
          ))}
        </ol>
      )}
    </>
  );
}

interface LibraryCardProps {
  tip: Tip;
  lists: readonly ListInfo[];
  sources: readonly SourceInfo[];
  meta: Meta;
  onSaved: (result: PatchResult, message: string) => void;
  onDeleted: (tip: Tip) => void;
}

function LibraryCard({ tip, lists, sources, meta, onSaved, onDeleted }: LibraryCardProps) {
  const [draft, setDraft] = useState<TipDraft | null>(null);
  const [moveTo, setMoveTo] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { limits } = meta;
  const problem = draft === null ? null : draftProblem(draft, meta);
  const titles = listTitles(tip.list_ids, lists);
  const field = (name: string) => `library-tip-${tip.id}-${name}`;

  const run = async (failure: string, work: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await work();
    } catch (caught) {
      setError(`${failure}: ${errorText(caught)}`);
    } finally {
      setBusy(false);
    }
  };

  const patch = async (body: object, message: (result: PatchResult) => string) => {
    const result = await apiSend<PatchResult>("PATCH", `/tips/${tip.id}`, body);
    onSaved(result, message(result));
  };

  const save = () =>
    run("Could not save", async () => {
      if (draft === null || problem !== null) return;
      const body = patchOf(tip, draft, lists, meta);
      if (Object.keys(body).length > 0) {
        await patch(body, (result) => `Saved “${result.tip.name}”.`);
      }
      setDraft(null);
    });

  const move = () =>
    run("Could not move the tip", async () => {
      if (moveTo === "") return;
      const bucket = Number(moveTo);
      await patch({ bucket }, (result) => `Moved “${result.tip.name}” to ${bucketTitle(bucket, meta)}.`);
      setMoveTo("");
    });

  const remove = () =>
    run("Could not delete the tip", async () => {
      await apiSend<undefined>("DELETE", `/tips/${tip.id}`);
      onDeleted(tip);
    });

  return (
    <li className="tip-card library-card" aria-label={`Tip #${tip.id} ${tip.name}`}>
      {draft !== null ? (
        <TipEditor idPrefix={`library-tip-${tip.id}`} draft={draft} onChange={setDraft} lists={lists} meta={meta} />
      ) : (
        <div className="tip-view">
          <p className="tip-line">
            <span className="tip-id">#{tip.id}</span> <strong className="tip-name">{tip.name}</strong>
            <span className="tip-separator">{limits.LINE_SEPARATOR}</span>
            <span className="tip-action">{tip.action}</span>
          </p>
          {tip.reason !== null && <p className="tip-reason">{tip.reason}</p>}
        </div>
      )}
      <div className="tip-badges">
        <span className={`badge badge-bucket badge-bucket-${tip.bucket}`}>{bucketTitle(tip.bucket, meta)}</span>
        <span className="badge badge-audience">{tip.audience}</span>
        {tip.status === limits.TIP_STATUS_APPROVED ? (
          <span className="badge badge-status">{statusLabel(tip.status, meta)}</span>
        ) : (
          <a className="badge badge-status badge-pending" href={href("review")}>
            {statusLabel(tip.status, meta)}
          </a>
        )}
        <span className="badge badge-source">{sourceName(tip.source_id, sources, tip.source_ref)}</span>
        {tip.bucket === limits.BUCKET_DAILY && (
          <span className="badge badge-lists">{titles.length > 0 ? `On ${titles.join(", ")}` : "Not on any list yet"}</span>
        )}
        {tip.start_url !== null && tip.start_s !== null && (
          <a
            className="badge badge-time"
            href={tip.start_url}
            target="_blank"
            rel="noopener noreferrer"
            aria-label={`Watch from ${clockTime(tip.start_s)} on YouTube`}
          >
            ▶ {clockTime(tip.start_s)}
          </a>
        )}
        {tip.evidence_url !== null && (
          <a className="badge badge-evidence" href={tip.evidence_url} target="_blank" rel="noopener noreferrer">
            Evidence
          </a>
        )}
        {tip.duplicate_of !== null && (
          <ReferenceBadge kind="duplicate" id={tip.duplicate_of} brief={tip.duplicate_of_tip} />
        )}
        {tip.possible_duplicate_of !== null && (
          <ReferenceBadge kind="possible" id={tip.possible_duplicate_of} brief={tip.possible_duplicate_of_tip} />
        )}
      </div>
      {tip.note !== null && draft === null && <p className="tip-note">Note: {tip.note}</p>}
      {problem !== null && <p className="hint">{problem}</p>}
      {error !== null && (
        <p role="alert" className="app-alert">
          {error}
        </p>
      )}
      {confirming ? (
        <div className="confirm-delete" role="group" aria-label={`Delete #${tip.id}`}>
          <p>
            Delete #{tip.id} “{tip.name}” for good? This cannot be undone; tips that repeat it keep a note naming it.
          </p>
          <div className="tip-actions">
            <button type="button" className="danger" onClick={remove} disabled={busy}>
              Delete for good
            </button>
            <button type="button" onClick={() => setConfirming(false)} disabled={busy}>
              Keep it
            </button>
          </div>
        </div>
      ) : (
        <div className="tip-actions">
          {draft !== null ? (
            <>
              <button type="button" className="primary" onClick={save} disabled={busy || problem !== null}>
                Save
              </button>
              <button type="button" onClick={() => setDraft(null)} disabled={busy}>
                Cancel
              </button>
            </>
          ) : (
            <>
              <button
                type="button"
                onClick={() => {
                  setDraft(draftOf(tip));
                  setError(null);
                }}
                disabled={busy}
              >
                Edit
              </button>
              <span className="move-control">
                <label htmlFor={field("move")}>Move to</label>
                <select id={field("move")} value={moveTo} onChange={(event) => setMoveTo(event.target.value)} disabled={busy}>
                  <option value="">Choose a bucket…</option>
                  {meta.buckets
                    .filter((bucket) => bucket.id !== tip.bucket)
                    .map((bucket) => (
                      <option key={bucket.id} value={bucket.id}>
                        {bucket.id}. {bucket.title}
                      </option>
                    ))}
                </select>
                <button type="button" onClick={move} disabled={busy || moveTo === ""}>
                  Move
                </button>
              </span>
              <button type="button" className="danger" onClick={() => setConfirming(true)} disabled={busy}>
                Delete
              </button>
            </>
          )}
        </div>
      )}
    </li>
  );
}

/** "Duplicate of #3 · name" / "Possible duplicate of #3 · name"; the original may be gone. */
function ReferenceBadge({ kind, id, brief }: { kind: "duplicate" | "possible"; id: number; brief: TipBrief | null }) {
  const label = kind === "duplicate" ? "Duplicate of" : "Possible duplicate of";
  return (
    <span className={`badge badge-${kind}`} title={brief === null ? undefined : `${brief.name}: ${brief.action}`}>
      {label} #{id}
      {brief !== null && <> · {brief.name}</>}
    </span>
  );
}
