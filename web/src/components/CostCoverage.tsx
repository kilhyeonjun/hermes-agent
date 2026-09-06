import type { CostCoverage as Coverage } from "@/lib/api";

export function CostCoverage({ coverage }: { coverage?: Coverage }) {
  const amount = (value: number | null | undefined) =>
    value == null ? "n/a" : `$${value.toFixed(4)}`;
  return (
    <div className="mt-4 text-xs text-text-tertiary leading-relaxed" aria-label="Cost coverage">
      <p>Actual reported: {amount(coverage?.actual_reported_usd)} · Estimated reference: {amount(coverage?.estimated_reference_usd)}</p>
      {coverage ? (
        <>
          <p>Recorded requests: {coverage.route_api_calls} · Actual: {coverage.requests.actual} · Estimated: {coverage.requests.estimated} · Subscription included: {coverage.requests.included} · Unknown: {coverage.requests.unknown}</p>
          {(coverage.main_api_call_discrepancy !== 0 || coverage.provenance_count_discrepancy !== 0) && (
            <p>Request count discrepancy: session vs main route {coverage.main_api_call_discrepancy}; provenance excess {coverage.provenance_count_discrepancy}. Unattributed requests are outside coverage.</p>
          )}
        </>
      ) : <p>Request cost provenance is unavailable.</p>}
      <p>Session-start period. Reported amounts and reference estimates are separate; neither verifies your provider invoice. Unobserved retries are excluded.</p>
    </div>
  );
}
