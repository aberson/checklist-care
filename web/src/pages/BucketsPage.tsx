// Placeholder: shows the bucket titles from /api/meta; rubric editing replaces it.
import type { Meta } from "../meta";

export function BucketsPage({ meta }: { meta: Meta | null }) {
  return (
    <section>
      <h1>Buckets</h1>
      <p>Every tip sorts into one of these buckets.</p>
      {meta === null ? (
        <p>Loading…</p>
      ) : (
        <ol aria-label="Buckets">
          {meta.buckets.map((bucket) => (
            <li key={bucket.id}>{bucket.title}</li>
          ))}
        </ol>
      )}
    </section>
  );
}
