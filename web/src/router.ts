// Hash routing without a router dependency (plan §2): pages live at #/review, #/library,
// #/lists and #/buckets, so the server needs no fallback for page paths.
import { useEffect, useState } from "react";

export type PageName = "review" | "library" | "lists" | "buckets";

export type Route = { kind: "page"; page: PageName } | { kind: "not-found"; path: string };

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

/** The route of a `location.hash` such as `#/lists`; slashes at either end are ignored. */
export function parseRoute(hash: string): Route {
  const path = hash.replace(/^#/, "").replace(/^\/+|\/+$/g, "");
  if (path === "") return { kind: "page", page: DEFAULT_PAGE };
  const item = NAV.find((entry) => entry.page === path);
  return item === undefined ? { kind: "not-found", path } : { kind: "page", page: item.page };
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
