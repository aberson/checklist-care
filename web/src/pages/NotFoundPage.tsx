import { DEFAULT_PAGE, href } from "../router";

export function NotFoundPage({ path }: { path: string }) {
  return (
    <section>
      <h1>Page not found</h1>
      <p>
        There is no page at <code>#/{path}</code>. <a href={href(DEFAULT_PAGE)}>Go to the review inbox</a>.
      </p>
    </section>
  );
}
