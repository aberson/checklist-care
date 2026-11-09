// Hash routing without a router dependency (plan §2): pages live at #/review, #/library,
// #/lists and #/buckets, and the A4 print sheets at #/print/list/<id> and #/print/activities,
// so the server needs no fallback for page paths.
import { useEffect, useState } from "react";

export type PageName = "review" | "library" | "lists" | "buckets";

export type Route =
  | { kind: "page"; page: PageName }
  | { kind: "print-list"; listId: number }
  | { kind: "print-activities" }
  | { kind: "not-found"; path: string };

export const NAV: readonly { page: PageName; label: string }[] = [
  { page: "review", label: "Review" },
  { page: "library", label: "Library" },
  { page: "lists", label: "Lists" },
  { page: "buckets", label: "Buckets" },
];

/** The page an empty hash (`/`, `#`, `#/`) shows. */
export const DEFAULT_PAGE: PageName = "review";

export function href(page: PageName): string {
  return `#/${page}`;
}

/** The location sheet of list `listId`: `#/print/list/7`. */
export function printListHref(listId: number): string {
  return `#/print/list/${listId}`;
}

/** The activities sheet. */
export const PRINT_ACTIVITIES_HREF = "#/print/activities";

const PRINT_LIST_PATH = /^print\/list\/([1-9][0-9]*)$/;
const PRINT_ACTIVITIES_PATH = "print/activities";

/** The route of a `location.hash` such as `#/lists`; slashes at either end are ignored. */
export function parseRoute(hash: string): Route {
  const path = hash.replace(/^#/, "").replace(/^\/+|\/+$/g, "");
  if (path === "") return { kind: "page", page: DEFAULT_PAGE };
  const item = NAV.find((entry) => entry.page === path);
  if (item !== undefined) return { kind: "page", page: item.page };
  if (path === PRINT_ACTIVITIES_PATH) return { kind: "print-activities" };
  const listId = Number(PRINT_LIST_PATH.exec(path)?.[1]);
  if (Number.isSafeInteger(listId)) return { kind: "print-list", listId };
  return { kind: "not-found", path };
}

/** The current route, updated on every `hashchange`. */
export function useHashRoute(): Route {
  const [route, setRoute] = useState(() => parseRoute(window.location.hash));
  useEffect(() => {
    const update = () => setRoute(parseRoute(window.location.hash));
    window.addEventListener("hashchange", update);
    update();
    return () => window.removeEventListener("hashchange", update);
  }, []);
  return route;
}
