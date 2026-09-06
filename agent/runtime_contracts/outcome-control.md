# Outcome Control Contract

<rule_contract id="OUTCOME_CONTROL_V1">
  <required_action name="task_supersession">TASK_SUPERSESSION: Explicit replacement supersedes work; steering preserves it. Mention/review never reactivates old work. Continue follows latest selection after compaction too; ask if ambiguous.</required_action>
  <required_action name="scope_snapshot">SCOPE_SNAPSHOT: Before review freeze acceptance, approved additions, contracts, exclusions, baseline/ref and artifact; scope changes need explicit approval.</required_action>
  <required_action name="blocks_current_user_outcome">REVIEW_SCOPE_ATTRIBUTION: Block/auto-fix only evidenced ACCEPTANCE failures or DIFF_REGRESSION blocking outcome/verification. NONE is non-blocking; expansion requires approval.</required_action>
  <responsibility>Elapsed time, repository count, or dispatch count alone never require renewed approval. Continue approved work; ask before scope/authority expansion.</responsibility>
  <responsibility>SMALL: bounded/reversible, deterministic checks, no high risk—act/verify. NORMAL: review if uncertain. HIGH: security, data loss, architecture, public contracts or weak verification—independent design and implementation review. Keep safety/delivery gates; re-review material fixes/new risk only.</responsibility>
  <responsibility>REVIEW_POLL_RUNNING: Running poll expiry is non-terminal; progress within 60s, wait without interruption or retry charge. REVIEW_RUNTIME_RETRY: Only declared retryable terminal transport/execution failure permits one fresh dispatch; findings are not failures. REVIEW_UNAVAILABLE: Repeated failure/unavailability: report attempts/unverified work. Timeout never approves.</responsibility>
  <required_action name="REMOTE_DELIVERY_GATE">After required remote delivery, fetch; local HEAD = remote SHA + delivered paths in that commit before claim.</required_action>
</rule_contract>
