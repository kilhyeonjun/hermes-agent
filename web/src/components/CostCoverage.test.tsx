import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { CostCoverage } from "./CostCoverage";

describe("cost coverage", () => {
  it("distinguishes unavailable cost from measured zero and reports the denominator gap", () => {
    const html = renderToStaticMarkup(<CostCoverage coverage={{
      actual_reported_usd: null, estimated_reference_usd: 0,
      requests: { actual: 0, estimated: 1, included: 0, unknown: 3 },
      route_api_calls: 4, main_api_call_discrepancy: 2, provenance_count_discrepancy: 0,
    }} />);
    expect(html).toContain("Actual reported: n/a");
    expect(html).toContain("Estimated reference: $0.0000");
    expect(html).toContain("Unknown: 3");
    expect(html).toContain("session vs main route 2");
    expect(html).toContain("neither verifies");
    expect(renderToStaticMarkup(<CostCoverage />)).not.toContain("$0");
  });
});
