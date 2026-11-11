// GET /api/meta, loaded once per page load: every limits.py value the UI uses (form
// limits, enums, print capacity) and the current bucket titles. The values are never
// restated here; tests/integration/test_server_shell.py checks that every key named in
// MetaLimits is a public limits.py name. Renaming a bucket (the Buckets page) calls
// refreshMeta(), so every page shows the new title without a reload.
import { useEffect, useState } from "react";
import { apiGet } from "./api";

export interface MetaLimits {
  NAME_MAX: number;
  ACTION_MAX: number;
  LINE_MAX: number;
  LINE_SEPARATOR: string;
  REASON_MAX: number;
  BUCKET_TITLE_MAX: number;
  RUBRIC_MAX: number;
  PLACE_MAX: number;
  CUE_MAX: number;
  PRINT_CAPACITY: number;
  AUDIENCES_TIP: readonly string[];
  AUDIENCES_LIST: readonly string[];
  AUDIENCE_KIDS: string;
  AUDIENCE_KIDS_PLUS: string;
  AUDIENCE_ADULTS: string;
  BUCKET_IDS: readonly number[];
  BUCKET_DAILY: number;
  BUCKET_LEFT_OUT: number;
  TIP_STATUSES: readonly string[];
  TIP_STATUS_APPROVED: string;
  TIP_STATUS_PENDING: string;
  SOURCE_KIND_YOUTUBE: string;
  SOURCE_KIND_WIKI: string;
  SOURCE_KIND_TEXT: string;
  SOURCE_KIND_MANUAL: string;
}

export interface BucketTitle {
  id: number;
  title: string;
}

export interface Meta {
  version: string;
  limits: MetaLimits;
  buckets: readonly BucketTitle[];
  print_capacity: number;
}

export type MetaState =
  | { status: "loading" }
  | { status: "ready"; meta: Meta }
  | { status: "error"; message: string };

let pending: Promise<Meta> | null = null;

/** The meta payload, fetched on the first call and shared after; a failed load is retried next call. */
export function loadMeta(): Promise<Meta> {
  if (pending === null) {
    pending = apiGet<Meta>("/meta").catch((error: unknown) => {
      pending = null;
      throw error;
    });
  }
  return pending;
}

/** Drop the loaded payload so the next {@link loadMeta} fetches again. */
export function forgetMeta(): void {
  pending = null;
}

const reloaders = new Set<() => void>();

/**
 * Fetch /api/meta again and give every mounted {@link useMeta} the new payload: called after a
 * bucket is renamed, so the bucket selects and badges on every page show the stored title.
 */
export function refreshMeta(): void {
  forgetMeta();
  for (const reload of reloaders) reload();
}

/**
 * The meta payload, loaded once and again after each {@link refreshMeta}. A failed first load is
 * an error; a failed refresh keeps the payload already shown (only bucket titles can change).
 */
export function useMeta(): MetaState {
  const [state, setState] = useState<MetaState>({ status: "loading" });
  const [loads, setLoads] = useState(0);

  useEffect(() => {
    const reload = () => setLoads((count) => count + 1);
    reloaders.add(reload);
    return () => {
      reloaders.delete(reload);
    };
  }, []);

  useEffect(() => {
    let active = true;
    loadMeta().then(
      (meta) => {
        if (active) setState({ status: "ready", meta });
      },
      (error: unknown) => {
        if (!active) return;
        const message = error instanceof Error ? error.message : String(error);
        setState((current) => (current.status === "ready" ? current : { status: "error", message }));
      },
    );
    return () => {
      active = false;
    };
  }, [loads]);
  return state;
}
