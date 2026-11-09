// The shared tip form (plan §5): name, action (with a live LINE_MAX counter), reason, bucket,
// audience and the lists the tip may go on. It is controlled: the caller holds the draft, so
// a button outside the form (Approve, Left out) can save it. Every limit comes from /api/meta.
import type { Meta } from "../meta";
import type { ListInfo, Tip, TipPatch } from "../model";
import { compatible, lineLength, textLength } from "../model";

/** The editable fields of a tip, as the form holds them. */
export interface TipDraft {
  name: string;
  action: string;
  reason: string;
  bucket: number;
  audience: string;
  listIds: number[];
}

export function draftOf(tip: Tip): TipDraft {
  return {
    name: tip.name,
    action: tip.action,
    reason: tip.reason ?? "",
    bucket: tip.bucket,
    audience: tip.audience,
    listIds: [...tip.list_ids],
  };
}

function sameIds(left: readonly number[], right: readonly number[]): boolean {
  const a = [...left].sort((x, y) => x - y);
  const b = [...right].sort((x, y) => x - y);
  return a.length === b.length && a.every((value, index) => value === b[index]);
}

/** The lists among `listIds` the draft may stay on; the others are dropped when it is saved. */
export function keptListIds(draft: TipDraft, lists: readonly ListInfo[], meta: Meta): number[] {
  return draft.listIds.filter((id) => {
    const list = lists.find((item) => item.id === id);
    return list !== undefined && compatible(draft.bucket, draft.audience, list.audience, meta);
  });
}

/**
 * The PATCH body that turns `tip` into `draft`: only the changed fields. `list_ids` is sent
 * only when the checkboxes changed, so a bucket or audience change alone lets the server drop
 * the placements it invalidates and report them (`removed_placements`).
 */
export function patchOf(tip: Tip, draft: TipDraft, lists: readonly ListInfo[], meta: Meta): TipPatch {
  const patch: TipPatch = {};
  const name = draft.name.trim();
  const action = draft.action.trim();
  const reason = draft.reason.trim();
  if (name !== tip.name) patch.name = name;
  if (action !== tip.action) patch.action = action;
  if (reason !== (tip.reason ?? "")) patch.reason = reason === "" ? null : reason;
  if (draft.bucket !== tip.bucket) patch.bucket = draft.bucket;
  if (draft.audience !== tip.audience) patch.audience = draft.audience;
  if (!sameIds(draft.listIds, tip.list_ids)) patch.list_ids = keptListIds(draft, lists, meta);
  return patch;
}

/** Why the draft cannot be saved yet, or null when it can. */
export function draftProblem(draft: TipDraft, meta: Meta): string | null {
  if (draft.name.trim() === "") return "The name is empty.";
  if (draft.action.trim() === "") return "The action is empty.";
  const length = lineLength(draft.name.trim(), draft.action.trim(), meta);
  if (length > meta.limits.LINE_MAX) {
    return `The printed line is ${length} characters; ${meta.limits.LINE_MAX} fit on one line.`;
  }
  return null;
}

interface TipEditorProps {
  /** Prefixes the field ids, so several editors can share a page. */
  idPrefix: string;
  draft: TipDraft;
  onChange: (draft: TipDraft) => void;
  lists: readonly ListInfo[];
  meta: Meta;
}

export function TipEditor({ idPrefix, draft, onChange, lists, meta }: TipEditorProps) {
  const { limits } = meta;
  const length = lineLength(draft.name.trim(), draft.action.trim(), meta);
  const tooLong = length > limits.LINE_MAX;
  const offered = lists.filter((list) => compatible(draft.bucket, draft.audience, list.audience, meta));
  const kept = keptListIds(draft, lists, meta);
  const dropped = lists.filter((list) => draft.listIds.includes(list.id) && !kept.includes(list.id));
  const set = (change: Partial<TipDraft>) => onChange({ ...draft, ...change });
  const toggle = (listId: number, checked: boolean) =>
    set({ listIds: checked ? [...draft.listIds, listId] : draft.listIds.filter((id) => id !== listId) });
  const field = (name: string) => `${idPrefix}-${name}`;

  return (
    <div className="tip-editor">
      <div className="field">
        <label htmlFor={field("name")}>Name</label>
        <input
          id={field("name")}
          value={draft.name}
          maxLength={limits.NAME_MAX}
          onChange={(event) => set({ name: event.target.value })}
        />
        <span className="field-count">
          {textLength(draft.name)} / {limits.NAME_MAX}
        </span>
      </div>
      <div className="field">
        <label htmlFor={field("action")}>What to do</label>
        <input
          id={field("action")}
          value={draft.action}
          maxLength={limits.ACTION_MAX}
          onChange={(event) => set({ action: event.target.value })}
        />
        <span className="field-count">
          {textLength(draft.action)} / {limits.ACTION_MAX}
        </span>
      </div>
      <p className={tooLong ? "line-counter line-counter-over" : "line-counter"} aria-live="polite">
        Printed line: {length} / {limits.LINE_MAX} characters
        {tooLong ? " — too long for one line; shorten the name or the action" : ""}
      </p>
      <div className="field">
        <label htmlFor={field("reason")}>Why</label>
        <input
          id={field("reason")}
          value={draft.reason}
          maxLength={limits.REASON_MAX}
          onChange={(event) => set({ reason: event.target.value })}
        />
        <span className="field-count">
          {textLength(draft.reason)} / {limits.REASON_MAX}
        </span>
      </div>
      <div className="field-row">
        <div className="field">
          <label htmlFor={field("bucket")}>Bucket</label>
          <select
            id={field("bucket")}
            value={draft.bucket}
            onChange={(event) => set({ bucket: Number(event.target.value) })}
          >
            {meta.buckets.map((bucket) => (
              <option key={bucket.id} value={bucket.id}>
                {bucket.id}. {bucket.title}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor={field("audience")}>Audience</label>
          <select id={field("audience")} value={draft.audience} onChange={(event) => set({ audience: event.target.value })}>
            {limits.AUDIENCES_TIP.map((audience) => (
              <option key={audience} value={audience}>
                {audience}
              </option>
            ))}
          </select>
        </div>
      </div>
      <fieldset className="field-lists">
        <legend>Lists</legend>
        {offered.length === 0 ? (
          <p className="hint">
            {draft.bucket === limits.BUCKET_DAILY
              ? "No list fits this tip."
              : "Only daily-checklist tips go on lists."}
          </p>
        ) : (
          offered.map((list) => (
            <label key={list.id} className="check">
              <input
                type="checkbox"
                checked={draft.listIds.includes(list.id)}
                onChange={(event) => toggle(list.id, event.target.checked)}
              />
              {list.title}
            </label>
          ))
        )}
        {dropped.length > 0 && (
          <p className="hint">Saving takes it off: {dropped.map((list) => list.title).join(", ")}</p>
        )}
      </fieldset>
    </div>
  );
}
