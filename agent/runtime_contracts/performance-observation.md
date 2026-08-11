<rule_contract id="PERFORMANCE_OBSERVATION_CONTRACT_V1">
  <required_action>Retain available elapsed metadata for commands, tool calls, agent tasks, builds, tests, deployments, browser work, and network operations; inspect available stage or bottleneck signals. Report required timing as unavailable instead of inferring it.</required_action>
  <responsibility>Apply repository or tool budgets first. Without one, flag a candidate when two comparable measurements confirm the same operation and environment regressed against the same baseline by more than 20% and at least 1 second, one stage takes at least 20% of total and at least 10 seconds, or one bounded operation repeatedly takes more than 30 seconds.</responsibility>
  <responsibility>Report material expected external waits, but propose optimization only when the delay is actionable.</responsibility>
  <required_action>If requested work newly reveals a material bottleneck outside the user-authorized optimization scope, continue the requested correctness/work, then present evidence, root cause, options, exactly one fundamental recommendation with its reason, and obtain approval before optimizing.</required_action>
  <required_action>Completion reports surface material candidates, unavailable required evidence, and skipped inspection; they need not enumerate every non-material duration.</required_action>
</rule_contract>
