// The review inbox (plan §5, Step 6): pending tips grouped by source. Each tip can be edited
// inline (TipEditor), approved, or marked Left out (bucket 4 and approved, nothing deleted);
// a source's tips can be approved together. Duplicate badges show and jump to the original,
// and YouTube tips link to their moment in the video. Data: GET /api/tips?status=pending,
// /api/sources and /api/lists; changes: PATCH /api/tips/{id}, POST /api/review/approve and
// POST /api/review/left-out.
import { useCallback, useEffect, useId, useState } from "react";
import { apiGet, apiSend, errorText } from "../api";
import { draftOf, draftProblem, patchNotice, patchOf, TipEditor } from "../components/TipEditor";
import type { TipDraft } from "../components/TipEditor";
import type { Meta } from "../meta";
import type { ListInfo, PatchResult, SourceInfo, Tip, TipBrief } from "../model";
import { bucketTitle, clockTime, sourceKindLabel } from "../model";

type InboxState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; tips: Tip[]; sources: SourceInfo[]; lists: ListInfo[] };

interface SourceGroup {
  source: SourceInfo;
  tips: Tip[];
}

/** The pending tips by source, in source order; a source with no pending tip is left out. */
export function groupBySource(tips: readonly Tip[], sources: readonly SourceInfo[]): SourceGroup[] {
  const groups = sources
    .map((source) => ({ source, tips: tips.filter((tip) => tip.source_id === source.id) }))
    .filter((group) => group.tips.length > 0);
  // A tip whose source is missing from /api/sources still shows, under its ref.
  const known = new Set(sources.map((source) => source.id));
  for (const tip of tips) {
    if (known.has(tip.source_id)) continue;
    known.add(tip.source_id);
    groups.push({
      source: {
        id: tip.source_id,
        kind: tip.source_ref.split(":")[0] ?? "",
        ref: tip.source_ref,
        title: null,
        url: null,
        mode: "",
        reviewed_at: null,
        tip_counts: {},
      },
      tips: tips.filter((item) => item.source_id === tip.source_id),
    });
  }
  return groups;
}

function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`;
}

export function tipCardId(tipId: number): string {
  return `review-tip-${tipId}`;
}

export function ReviewPage({ meta }: { meta: Meta | null }) {
  const [state, setState] = useState<InboxState>({ status: "loading" });
  const [announcement, setAnnouncement] = useState("");
  const [editing, setEditing] = useState<ReadonlySet<number>>(new Set());

  useEffect(() => {
    let active = true;
    Promise.all([
      apiGet<Tip[]>("/tips?status=pending"),
      apiGet<SourceInfo[]>("/sources"),
      apiGet<ListInfo[]>("/lists"),
    ]).then(
      ([tips, sources, lists]) => {
        if (!active) return;
        if (!Array.isArray(tips) || !Array.isArray(sources) || !Array.isArray(lists)) {
          setState({ status: "error", message: "the server sent an unexpected reply" });
          return;
        }
        setState({ status: "ready", tips, sources, lists });
      },
      (error: unknown) => {
        if (active) setState({ status: "error", message: errorText(error) });
      },
    );
    return () => {
      active = false;
    };
  }, []);

  const replaceTip = useCallback((tip: Tip) => {
    setState((current) =>
      current.status === "ready"
        ? { ...current, tips: current.tips.map((item) => (item.id === tip.id ? tip : item)) }
        : current,
    );
  }, []);

  const removeTips = useCallback((ids: readonly number[], message: string) => {
    setState((current) =>
      current.status === "ready" ? { ...current, tips: current.tips.filter((tip) => !ids.includes(tip.id)) } : current,
    );
    setEditing((current) => new Set([...current].filter((id) => !ids.includes(id))));
    setAnnouncement(message);
  }, []);

  const setTipEditing = useCallback((tipId: number, on: boolean) => {
    setEditing((current) => {
      const next = new Set(current);
      if (on) next.add(tipId);
      else next.delete(tipId);
      return next;
    });
  }, []);

  return (
    <section className="review">
      <h1>Review inbox</h1>
      <p className="lede">Proposed tips wait here until you approve them or leave them out. Only approved tips print.</p>
      <p role="status" className="review-announcement">
        {announcement}
      </p>
      {state.status === "loading" && <p>Loading the inbox…</p>}
      {state.status === "error" && (
        <p role="alert" className="app-alert">
          Cannot load the review inbox: {state.message}
        </p>
      )}
      {state.status === "ready" && (
        <Inbox
          state={state}
          meta={meta}
          editing={editing}
          onReplace={replaceTip}
          onRemove={removeTips}
          onEditing={setTipEditing}
        />
      )}
    </section>
  );
}

interface InboxProps {
  state: Extract<InboxState, { status: "ready" }>;
  meta: Meta | null;
  editing: ReadonlySet<number>;
  onReplace: (tip: Tip) => void;
  onRemove: (ids: readonly number[], message: string) => void;
  onEditing: (tipId: number, on: boolean) => void;
}

function Inbox({ state, meta, editing, onReplace, onRemove, onEditing }: InboxProps) {
  const groups = groupBySource(state.tips, state.sources);
  if (groups.length === 0) {
    return (
      <p className="review-empty">
        Nothing to review. Run the checklist-ingest skill on a source, then reload this page.
      </p>
    );
  }
  const inbox = new Set(state.tips.map((tip) => tip.id));
  return (
    <>
      <p className="review-summary">
        {plural(state.tips.length, "tip", "tips")} waiting from {plural(groups.length, "source", "sources")}.
      </p>
      {groups.map((group) => (
        <SourceSection
          key={group.source.id}
          group={group}
          lists={state.lists}
          sources={state.sources}
          meta={meta}
          inbox={inbox}
          editing={editing}
          onReplace={onReplace}
          onRemove={onRemove}
          onEditing={onEditing}
        />
      ))}
    </>
  );
}

interface SourceSectionProps extends Omit<InboxProps, "state"> {
  group: SourceGroup;
  lists: readonly ListInfo[];
  sources: readonly SourceInfo[];
  inbox: ReadonlySet<number>;
}

function SourceSection({ group, lists, sources, meta, inbox, editing, onReplace, onRemove, onEditing }: SourceSectionProps) {
  const { source, tips } = group;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const title = source.title ?? source.ref;
  const headingId = `review-source-${source.id}`;
  const editingHere = tips.some((tip) => editing.has(tip.id));

  const approveAll = async () => {
    setBusy(true);
    setError(null);
    const ids = tips.map((tip) => tip.id);
    try {
      await apiSend<{ approved: number }>("POST", "/review/approve", { tip_ids: ids });
      onRemove(ids, `Approved ${plural(ids.length, "tip", "tips")} from ${title}.`);
    } catch (caught) {
      setError(errorText(caught));
      setBusy(false);
    }
  };

  return (
    <section className="source-group" aria-labelledby={headingId}>
      <header className="source-header">
        <div>
          <h2 id={headingId}>{title}</h2>
          <p className="source-meta">
            {sourceKindLabel(source.kind, meta)} · {plural(tips.length, "tip", "tips")} to review
            {source.url !== null && (
              <>
                {" · "}
                <a href={source.url} target="_blank" rel="noopener noreferrer">
                  Open the source
                </a>
              </>
            )}
          </p>
        </div>
        <div className="source-actions">
          <button type="button" onClick={approveAll} disabled={busy || editingHere}>
            Approve all for this source
          </button>
          {editingHere && <span className="hint">Save or cancel the open edit first.</span>}
        </div>
      </header>
      {error !== null && (
        <p role="alert" className="app-alert">
          Could not approve these tips: {error}
        </p>
      )}
      <ol className="tip-cards">
        {tips.map((tip) => (
          <TipCard
            key={tip.id}
            tip={tip}
            lists={lists}
            sources={sources}
            meta={meta}
            inbox={inbox}
            onReplace={onReplace}
            onRemove={onRemove}
            onEditing={onEditing}
          />
        ))}
      </ol>
    </section>
  );
}

interface TipCardProps {
  tip: Tip;
  lists: readonly ListInfo[];
  sources: readonly SourceInfo[];
  meta: Meta | null;
  inbox: ReadonlySet<number>;
  onReplace: (tip: Tip) => void;
  onRemove: (ids: readonly number[], message: string) => void;
  onEditing: (tipId: number, on: boolean) => void;
}

function TipCard({ tip, lists, sources, meta, inbox, onReplace, onRemove, onEditing }: TipCardProps) {
  const [draft, setDraft] = useState<TipDraft | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const separator = meta?.limits.LINE_SEPARATOR ?? " — ";
  const listTitles = tip.list_ids.map((id) => lists.find((list) => list.id === id)?.title ?? `list ${id}`);
  const editingNow = draft !== null;
  const problem = draft !== null && meta !== null ? draftProblem(draft, meta) : null;

  const startEdit = () => {
    setDraft(draftOf(tip));
    setError(null);
    onEditing(tip.id, true);
  };
  const stopEdit = () => {
    setDraft(null);
    onEditing(tip.id, false);
  };

  /** Save the open draft; true when there was nothing to save or the save worked. */
  const saveDraft = async (): Promise<boolean> => {
    if (draft === null || meta === null) return true;
    const blocked = draftProblem(draft, meta);
    if (blocked !== null) {
      setError(blocked);
      return false;
    }
    const patch = patchOf(tip, draft, lists, meta);
    if (Object.keys(patch).length === 0) {
      stopEdit();
      return true;
    }
    try {
      const result = await apiSend<PatchResult>("PATCH", `/tips/${tip.id}`, patch);
      onReplace(result.tip);
      setNotice(patchNotice(result, lists));
      stopEdit();
      return true;
    } catch (caught) {
      setError(`Could not save: ${errorText(caught)}`);
      return false;
    }
  };

  const run = async (work: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await work();
    } finally {
      setBusy(false);
    }
  };

  const save = () => run(async () => void (await saveDraft()));

  const review = (path: "/review/approve" | "/review/left-out", verb: string) =>
    run(async () => {
      if (!(await saveDraft())) return;
      try {
        await apiSend("POST", path, { tip_ids: [tip.id] });
        onRemove([tip.id], `${verb} “${draft?.name.trim() || tip.name}”.`);
      } catch (caught) {
        setError(`Could not update the tip: ${errorText(caught)}`);
      }
    });

  return (
    <li id={tipCardId(tip.id)} className="tip-card" tabIndex={-1} aria-label={`Tip #${tip.id} ${tip.name}`}>
      {editingNow && meta !== null ? (
        <TipEditor idPrefix={`tip-${tip.id}`} draft={draft} onChange={setDraft} lists={lists} meta={meta} />
      ) : (
        <div className="tip-view">
          <p className="tip-line">
            <span className="tip-id">#{tip.id}</span> <strong className="tip-name">{tip.name}</strong>
            <span className="tip-separator">{separator}</span>
            <span className="tip-action">{tip.action}</span>
          </p>
          {tip.reason !== null && <p className="tip-reason">{tip.reason}</p>}
        </div>
      )}
      <div className="tip-badges">
        <span className={`badge badge-bucket badge-bucket-${tip.bucket}`}>{bucketTitle(tip.bucket, meta)}</span>
        <span className="badge badge-audience">{tip.audience}</span>
        {tip.bucket === meta?.limits.BUCKET_DAILY && (
          <span className="badge badge-lists">{listTitles.length > 0 ? `On ${listTitles.join(", ")}` : "Not on any list yet"}</span>
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
          <OriginalBadge
            kind="duplicate"
            originalId={tip.duplicate_of}
            original={tip.duplicate_of_tip}
            sources={sources}
            meta={meta}
            inbox={inbox}
          />
        )}
        {tip.possible_duplicate_of !== null && (
          <OriginalBadge
            kind="possible"
            originalId={tip.possible_duplicate_of}
            original={tip.possible_duplicate_of_tip}
            sources={sources}
            meta={meta}
            inbox={inbox}
          />
        )}
      </div>
      {tip.note !== null && !editingNow && <p className="tip-note">Note: {tip.note}</p>}
      {notice !== null && <p className="tip-notice">{notice}</p>}
      {problem !== null && <p className="hint">{problem}</p>}
      {error !== null && (
        <p role="alert" className="app-alert">
          {error}
        </p>
      )}
      <div className="tip-actions">
        {editingNow ? (
          <>
            <button type="button" onClick={save} disabled={busy || problem !== null}>
              Save
            </button>
            <button type="button" onClick={stopEdit} disabled={busy}>
              Cancel
            </button>
          </>
        ) : (
          <button type="button" onClick={startEdit} disabled={busy || meta === null}>
            Edit
          </button>
        )}
        <button type="button" className="primary" onClick={() => review("/review/approve", "Approved")} disabled={busy || problem !== null}>
          Approve
        </button>
        <button type="button" onClick={() => review("/review/left-out", "Left out")} disabled={busy || problem !== null}>
          Left out
        </button>
      </div>
    </li>
  );
}

interface OriginalBadgeProps {
  kind: "duplicate" | "possible";
  originalId: number;
  original: TipBrief | null;
  sources: readonly SourceInfo[];
  meta: Meta | null;
  inbox: ReadonlySet<number>;
}

/** "Duplicate of #3 …" / "Possible duplicate of #3 …": opens the original, and jumps to it when it is in the inbox. */
function OriginalBadge({ kind, originalId, original, sources, meta, inbox }: OriginalBadgeProps) {
  const [open, setOpen] = useState(false);
  const label = kind === "duplicate" ? "Duplicate of" : "Possible duplicate of";
  const panelId = useId();
  const source = original === null ? undefined : sources.find((item) => item.id === original.source_id);
  const goTo = () => {
    const card = document.getElementById(tipCardId(originalId));
    card?.scrollIntoView?.({ behavior: "smooth", block: "center" });
    card?.focus();
  };
  return (
    <span className={`original original-${kind}`}>
      <button
        type="button"
        className={`badge badge-${kind}`}
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen(!open)}
      >
        {label} #{originalId}
        {original !== null && <> · {original.name}</>}
      </button>
      {open && (
        <span id={panelId} className="original-panel">
          {original === null ? (
            <>Tip #{originalId} is no longer stored.</>
          ) : (
            <>
              <strong>
                #{original.id} {original.name}
              </strong>
              {meta?.limits.LINE_SEPARATOR ?? " — "}
              {original.action}
              <span className="original-meta">
                {" "}
                ({original.status}, {bucketTitle(original.bucket, meta)}
                {source !== undefined && `, from ${source.title ?? source.ref}`})
              </span>
              {inbox.has(original.id) && (
                <button type="button" className="link-button" onClick={goTo}>
                  Go to #{original.id} in this inbox
                </button>
              )}
            </>
          )}
        </span>
      )}
    </span>
  );
}
