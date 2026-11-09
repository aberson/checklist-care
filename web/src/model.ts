// The API's tip, source and list shapes (plan §6) and the small rules the UI needs to show
// them. Limits and enum members always come from /api/meta (limits.py); the server stays the
// authority, so a rule here only decides what the form offers, never what is stored.
import type { Meta } from "./meta";

/** The short view of a tip that another tip names as its (possible) duplicate. */
export interface TipBrief {
  id: number;
  name: string;
  action: string;
  bucket: number;
  status: string;
  source_id: number;
}

/** A tip as `GET /api/tips` returns it. */
export interface Tip {
  id: number;
  name: string;
  action: string;
  reason: string | null;
  bucket: number;
  audience: string;
  status: string;
  source_id: number;
  source_ref: string;
  start_s: number | null;
  /** The YouTube link that starts playing at `start_s`; null for other sources. */
  start_url: string | null;
  locator: string | null;
  evidence_url: string | null;
  duplicate_of: number | null;
  duplicate_of_tip: TipBrief | null;
  possible_duplicate_of: number | null;
  possible_duplicate_of_tip: TipBrief | null;
  note: string | null;
  list_ids: number[];
  created_at: string;
  updated_at: string;
}

/** A source as `GET /api/sources` returns it. */
export interface SourceInfo {
  id: number;
  kind: string;
  ref: string;
  title: string | null;
  url: string | null;
  mode: string;
  reviewed_at: string | null;
  tip_counts: Record<string, number>;
}

/** A list as `GET /api/lists` returns it. */
export interface ListInfo {
  id: number;
  place: string;
  audience: string;
  title: string;
  cue: string;
  sort_order: number;
  item_count: number;
  over_capacity: boolean;
}

/** A tip on a list, as `GET /api/lists/{id}/items` returns it (pending or approved). */
export interface ListItem {
  tip_id: number;
  position: number;
  name: string;
  action: string;
  status: string;
  bucket: number;
  audience: string;
}

/** One printed row: the bold name, then the action. */
export interface PrintItem {
  name: string;
  action: string;
}

/** A location sheet, as `GET /api/print/lists/{id}` returns it: approved daily tips only. */
export interface LocationSheet {
  title: string;
  audience: string;
  generated_at: string;
  items: PrintItem[];
  over_capacity: boolean;
}

/** One audience's group on the activities sheet. */
export interface ActivityGroup {
  audience: string;
  items: PrintItem[];
}

/** The activities sheet, as `GET /api/print/activities` returns it: approved bucket-2 tips. */
export interface ActivitiesSheet {
  generated_at: string;
  groups: ActivityGroup[];
}

/** The fields `PATCH /api/tips/{id}` takes; a field left out keeps its value. */
export interface TipPatch {
  name?: string;
  action?: string;
  reason?: string | null;
  bucket?: number;
  audience?: string;
  list_ids?: number[];
}

/** What `PATCH /api/tips/{id}` returns. */
export interface PatchResult {
  tip: Tip;
  removed_placements: number[];
  cleared_duplicate_of: number | null;
}

/** Characters as Python counts them (code points), so the counter matches the server. */
export function textLength(text: string): number {
  return [...text].length;
}

/** Length of the printed `name — action` line, compared with LINE_MAX. */
export function lineLength(name: string, action: string, meta: Meta): number {
  return textLength(name) + textLength(meta.limits.LINE_SEPARATOR) + textLength(action);
}

/**
 * Whether a tip with `bucket` and `audience` may go on a list of `listAudience` (placement.py):
 * only daily tips go on lists, and a kids list takes only kids+ tips.
 */
export function compatible(bucket: number, audience: string, listAudience: string, meta: Meta): boolean {
  if (bucket !== meta.limits.BUCKET_DAILY) return false;
  return listAudience !== meta.limits.AUDIENCE_KIDS || audience === meta.limits.AUDIENCE_KIDS_PLUS;
}

/** `start_s` as the video player shows it: `0:30`, `12:05`, `1:02:03`. */
export function clockTime(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const secs = String(whole % 60).padStart(2, "0");
  return hours > 0 ? `${hours}:${String(minutes).padStart(2, "0")}:${secs}` : `${minutes}:${secs}`;
}

/** The title of bucket `id` from /api/meta, or `Bucket <id>` before it loads. */
export function bucketTitle(id: number, meta: Meta | null): string {
  return meta?.buckets.find((bucket) => bucket.id === id)?.title ?? `Bucket ${id}`;
}

/** A source kind as a reader says it; the kind itself before /api/meta loads. */
export function sourceKindLabel(kind: string, meta: Meta | null): string {
  if (meta === null) return kind;
  const labels: Record<string, string> = {
    [meta.limits.SOURCE_KIND_YOUTUBE]: "YouTube video",
    [meta.limits.SOURCE_KIND_WIKI]: "Wikipedia book page",
    [meta.limits.SOURCE_KIND_TEXT]: "Pasted text",
    [meta.limits.SOURCE_KIND_MANUAL]: "My own tips",
  };
  return labels[kind] ?? kind;
}

/** An audience as a heading shows it: `kids+` is `Kids+`, `adults` is `Adults`. */
export function audienceLabel(audience: string): string {
  return audience.charAt(0).toUpperCase() + audience.slice(1);
}
