// The app shell: the nav (Review, Library, Lists, Buckets) and the page of the current
// hash route, including the A4 print sheets. /api/meta is loaded once here; a failed load
// shows an alert above the page. Printing hides the nav and the alert (print/print.css).
import type { Meta } from "./meta";
import { useMeta } from "./meta";
import { BucketsPage } from "./pages/BucketsPage";
import { LibraryPage } from "./pages/LibraryPage";
import { ListsPage } from "./pages/ListsPage";
import { NotFoundPage } from "./pages/NotFoundPage";
import { ReviewPage } from "./pages/ReviewPage";
import { PrintActivitiesPage } from "./print/PrintActivitiesPage";
import { PrintListPage } from "./print/PrintListPage";
import type { PageName, Route } from "./router";
import { href, NAV, useHashRoute } from "./router";

function Page({ page, meta }: { page: PageName; meta: Meta | null }) {
  switch (page) {
    case "review":
      return <ReviewPage meta={meta} />;
    case "library":
      return <LibraryPage />;
    case "lists":
      return <ListsPage meta={meta} />;
    case "buckets":
      return <BucketsPage meta={meta} />;
  }
}

function RoutePage({ route, meta }: { route: Route; meta: Meta | null }) {
  switch (route.kind) {
    case "page":
      return <Page page={route.page} meta={meta} />;
    case "print-list":
      return <PrintListPage listId={route.listId} meta={meta} />;
    case "print-activities":
      return <PrintActivitiesPage meta={meta} />;
    case "not-found":
      return <NotFoundPage path={route.path} />;
  }
}

export function App() {
  const route = useHashRoute();
  const metaState = useMeta();
  const current = route.kind === "page" ? route.page : null;
  const meta = metaState.status === "ready" ? metaState.meta : null;
  return (
    <div className="app">
      <header className="app-header">
        <span className="app-title">checklist-care</span>
        <nav aria-label="Main">
          <ul>
            {NAV.map((item) => (
              <li key={item.page}>
                <a href={href(item.page)} aria-current={item.page === current ? "page" : undefined}>
                  {item.label}
                </a>
              </li>
            ))}
          </ul>
        </nav>
      </header>
      {metaState.status === "error" && (
        <p role="alert" className="app-alert">
          Cannot load the app settings from the checklist-care server: {metaState.message}
        </p>
      )}
      <main>
        <RoutePage route={route} meta={meta} />
      </main>
    </div>
  );
}
