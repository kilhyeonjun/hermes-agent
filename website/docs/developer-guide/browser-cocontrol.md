---
title: "Task-owned browser co-control transport"
description: "Opt-in private Unix transport for a dedicated task browser, with authoritative routing."
---

# Task-owned browser co-control transport

The existing browser registry can opt one dedicated profile/task into an external, local co-control service. The service owns the user's screen/input experience and approved task browser runtime. Vendor-specific authentication and deployment belong in that standalone service, not Hermes core.

```yaml
browser:
  cocontrol:
    enabled: true
    socket: /absolute/private/agent.sock
    task_id: exact-task-id
    session_id: exact-session-id
```

The socket must be owner-only (0600), inside an owned real 0700 directory. Task and session identities must both match the current invocation/session context. The configured lane is authoritative: a missing socket, rejected command, human takeover or identity mismatch never falls back to a different browser backend. Default profiles remain unchanged when the opt-in is absent.

The newline JSON protocol carries only `task_id`, `session_id`, `action` and `args`; the response is one bounded JSON object. The service must enforce exclusive control, queued-command invalidation, ownership/approval, deadlines, content privacy and revocation. It must not expose its socket or raw debugging endpoint publicly. No endpoint URL or credential is sent through this transport.

Initial allowed actions are `browser_navigate`, `browser_snapshot`, `browser_click`, `browser_type`, `browser_scroll`, `browser_back`, `browser_press`, `browser_tabs` and `browser_tab_activate`. Other actions—including `browser_exec`, raw CDP, vision, console, images and automated dialog response—fail closed. Dedicated sessions should retain the ordinary browser toolset; arbitrary browser code is not supported on this lane. Existing extension-control routing remains available outside the opt-in.

Use only a new approved task browser/profile. Do not configure a production profile to adopt another task's authenticated browser. Resume after takeover is an explicit user action in the external service, never a retry/fallback from Hermes. See `tools/browser_cocontrol.py`, `tools/browser_extension_router.py` and `tests/tools/test_browser_cocontrol.py` for the transport contract.
