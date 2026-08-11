<rule_contract id="OUTCOME_CONTROL_V1">
  <required_action>Focus outcome/verification; classify findings with evidence.</required_action>
  <required_action name="blocks_current_user_outcome">A finding blocks only when the current outcome or required verification cannot proceed; only evidence-backed blockers auto-fix.</required_action>
  <required_action>Non-blocking: evidence + approval before scope expansion.</required_action>
  <responsibility>Pause at 3 derived tasks, 15m, 2 reviewer dispatches, second repo, or second full-suite run.</responsibility>
  <responsibility>One review, one re-review, at most one runtime retry. REVIEW_POLL_RUNNING: A polling expiry while runtime status is running is non-terminal. Progress within 60s; wait; never interrupt; no retry/dispatch charge. REVIEW_RUNTIME_RETRY: Only a runtime-declared retryable terminal transport or execution failure permits one fresh reviewer dispatch. Findings are not runtime failures. REVIEW_UNAVAILABLE: Repeated retryable terminal failure or runtime-declared unavailability: report attempts/remaining verification. A timeout never approves.</responsibility>
  <responsibility>Plans stay within outcome; non-blocking plan/implementation needs approval.</responsibility>
  <required_action name="REMOTE_DELIVERY_GATE">After required remote delivery, fetch; local HEAD = fetched remote branch SHA before claim.</required_action>
</rule_contract>
