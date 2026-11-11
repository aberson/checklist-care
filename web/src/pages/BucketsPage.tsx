// The Buckets page (plan §3.4, §5, Step 10): the four buckets every tip sorts into, each with an
// editable title and rubric. The rubric is what the checklist-ingest skill reads (through
// `checklist-care context`) to sort the tips of its next ingest, so a saved rubric applies from the
// next source on; tips already stored keep their bucket. Data: GET /api/buckets; changes:
// PUT /api/buckets/{id} with only the changed fields. Every limit comes from /api/meta, and a
// renamed bucket refreshes it so the other pages show the new title.
import { useEffect, useState } from "react";
import { apiGet, apiSend, errorText } from "../api";
import type { Meta } from "../meta";
import { refreshMeta } from "../meta";
import type { Bucket, BucketPut } from "../model";
import { textLength } from "../model";

/** The editable fields of a bucket, as the form holds them. */
export interface BucketDraft {
  title: string;
  rubric: string;
}

export function bucketDraftOf(bucket: Bucket): BucketDraft {
  return { title: bucket.title, rubric: bucket.rubric };
}

/**
 * The `PUT /api/buckets/{id}` body that turns `bucket` into `draft`: only the changed fields,
 * trimmed. Line breaks inside the rubric are kept; the skill reads the rubric exactly as stored.
 */
export function bucketPutOf(bucket: Bucket, draft: BucketDraft): BucketPut {
  const put: BucketPut = {};
  const title = draft.title.trim();
  const rubric = draft.rubric.trim();
  if (title !== bucket.title) put.title = title;
  if (rubric !== bucket.rubric) put.rubric = rubric;
  return put;
}

/** Why the draft cannot be saved yet, or null when it can. */
export function bucketDraftProblem(draft: BucketDraft, meta: Meta): string | null {
  const { BUCKET_TITLE_MAX, RUBRIC_MAX } = meta.limits;
  const title = draft.title.trim();
  const rubric = draft.rubric.trim();
  if (title === "") return "The title is empty.";
  if (textLength(title) > BUCKET_TITLE_MAX) {
    return `The title is ${textLength(title)} characters; ${BUCKET_TITLE_MAX} fit.`;
  }
  if (rubric === "") return "The rubric is empty: the skill needs it to know which tips belong here.";
  if (textLength(rubric) > RUBRIC_MAX) {
    return `The rubric is ${textLength(rubric)} characters; ${RUBRIC_MAX} fit.`;
  }
  return null;
}

/** A stored `YYYY-MM-DDTHH:MM:SSZ` timestamp as the page shows it: `2026-11-08 12:00 UTC`. */
export function changedAt(timestamp: string): string {
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/.exec(timestamp);
  return match === null ? timestamp : `${match[1]} ${match[2]} UTC`;
}

type BucketsState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; buckets: Bucket[] };

export function BucketsPage({ meta }: { meta: Meta | null }) {
  const [state, setState] = useState<BucketsState>({ status: "loading" });
  const [announcement, setAnnouncement] = useState("");

  useEffect(() => {
    let active = true;
    apiGet<Bucket[]>("/buckets").then(
      (buckets) => {
        if (!active) return;
        setState(
          Array.isArray(buckets)
            ? { status: "ready", buckets }
            : { status: "error", message: "the server sent an unexpected reply" },
        );
      },
      (error: unknown) => {
        if (active) setState({ status: "error", message: errorText(error) });
      },
    );
    return () => {
      active = false;
    };
  }, []);

  const saved = (before: Bucket, after: Bucket, put: BucketPut) => {
    setState((current) =>
      current.status === "ready"
        ? { status: "ready", buckets: current.buckets.map((item) => (item.id === after.id ? after : item)) }
        : current,
    );
    const renamed = put.title !== undefined ? ` (was “${before.title}”)` : "";
    const next = put.rubric !== undefined ? " The skill sorts its next ingest by the new rubric." : "";
    setAnnouncement(`Saved bucket ${after.id}, “${after.title}”${renamed}.${next}`);
    if (put.title !== undefined) refreshMeta();
  };

  return (
    <section className="buckets">
      <h1>Buckets</h1>
      <p className="lede">
        Every tip sorts into one of these four buckets. The skill reads each rubric when it sorts the tips of its next
        ingest; tips already stored keep their bucket.
      </p>
      <p role="status" className="review-announcement">
        {announcement}
      </p>
      {meta === null && <p>Loading the app settings…</p>}
      {state.status === "loading" && <p>Loading the buckets…</p>}
      {state.status === "error" && (
        <p role="alert" className="app-alert">
          Cannot load the buckets: {state.message}
        </p>
      )}
      {meta !== null && state.status === "ready" && (
        <ol className="bucket-cards" aria-label="Buckets">
          {state.buckets.map((bucket) => (
            <BucketCard key={bucket.id} bucket={bucket} meta={meta} onSaved={saved} />
          ))}
        </ol>
      )}
    </section>
  );
}

interface BucketCardProps {
  bucket: Bucket;
  meta: Meta;
  onSaved: (before: Bucket, after: Bucket, put: BucketPut) => void;
}

function BucketCard({ bucket, meta, onSaved }: BucketCardProps) {
  const [draft, setDraft] = useState<BucketDraft>(() => bucketDraftOf(bucket));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { BUCKET_TITLE_MAX, RUBRIC_MAX } = meta.limits;
  const put = bucketPutOf(bucket, draft);
  const changed = Object.keys(put).length > 0;
  const problem = bucketDraftProblem(draft, meta);
  const field = (name: string) => `bucket-${bucket.id}-${name}`;
  const set = (change: Partial<BucketDraft>) => setDraft((current) => ({ ...current, ...change }));

  const save = async () => {
    if (!changed || problem !== null) return;
    setBusy(true);
    setError(null);
    try {
      const stored = await apiSend<Bucket>("PUT", `/buckets/${bucket.id}`, put);
      setDraft(bucketDraftOf(stored));
      onSaved(bucket, stored, put);
    } catch (caught) {
      setError(`Could not save: ${errorText(caught)}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <li className="bucket-card" aria-labelledby={field("heading")}>
      <h2 id={field("heading")}>
        {bucket.id}. {bucket.title}
      </h2>
      <div className="field">
        <label htmlFor={field("title")}>Title</label>
        <input
          id={field("title")}
          value={draft.title}
          maxLength={BUCKET_TITLE_MAX}
          onChange={(event) => set({ title: event.target.value })}
          disabled={busy}
        />
        <span className="field-count">
          {textLength(draft.title)} / {BUCKET_TITLE_MAX}
        </span>
      </div>
      <div className="field">
        <label htmlFor={field("rubric")}>Rubric</label>
        <textarea
          id={field("rubric")}
          value={draft.rubric}
          maxLength={RUBRIC_MAX}
          rows={4}
          onChange={(event) => set({ rubric: event.target.value })}
          disabled={busy}
        />
        <span className="field-count">
          {textLength(draft.rubric)} / {RUBRIC_MAX}
        </span>
      </div>
      <p className="hint">Last changed {changedAt(bucket.updated_at)}.</p>
      {problem !== null && <p className="hint">{problem}</p>}
      {error !== null && (
        <p role="alert" className="app-alert">
          {error}
        </p>
      )}
      <div className="tip-actions">
        <button type="button" className="primary" onClick={save} disabled={busy || !changed || problem !== null}>
          Save
        </button>
        <button
          type="button"
          onClick={() => {
            setDraft(bucketDraftOf(bucket));
            setError(null);
          }}
          disabled={busy || !changed}
        >
          Undo changes
        </button>
      </div>
    </li>
  );
}
