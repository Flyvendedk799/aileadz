# Debug a conversation from its chat ID

Employee chat, HR chat (page and panel), the vendor assistant and the embedded
widget expose **Kopiér chat-id**. Copy it and include it with the failure report.
This is the server's `session_id`, not the numeric sidebar history row ID or the
HTTP request ID. It stays the same across turns and when reopening a conversation;
a new conversation gets a new ID. The control is disabled until an ID is known.

## Look up and export

As a **platform admin**, open `/app1/adminlog`, paste the ID into **Find chat-id**,
and open the matching session. **Download samtale og logs** exports JSON, and
**Kopier som JSON** copies the same diagnostic bundle once the session is loaded.
The direct authenticated endpoint is:

```
GET /app1/adminlog/session/<chat_id>
GET /app1/adminlog/session/<chat_id>?download=1
```

The report includes:

- `turns`: full captured user messages and assistant text, rendered event payloads,
  outcome (`completed`, `interrupted`, `error`, `fallback`, or
  `unfinished_or_running`), request IDs and turn IDs. A network interruption keeps
  the partial response captured by the server; it does not prove the browser
  received every captured chunk.
- `conversations`: available stored conversation history, with its original
  messages and UI artifacts. Legacy histories can have been pruned for model
  context, so prefer captured `turns` when diagnosing recent failures.
- `runs` and `tool_runs`: existing AI runtime records matching the exact session
  ID, including model, usage, status, latency and tool arguments as already
  redacted by the runtime.
- `logs`: all retained debug entries, including correlated `chat_error` records
  with sanitized exception details; `warnings` identifies missing sources.

For server logs, search for `chat=<chat_id>` in the default log format, or the
`chat_id` field with `LOG_FORMAT=json`. The initial SSE heartbeat and response
headers `X-Chat-ID` / `X-Chat-Scope` expose the same reference before an answer
finishes. `X-Request-ID` links individual HTTP requests; `turn_id` links captured
start/end/error entries within the chat.

If the debugging environment does not have authorized access to the deployed
application or its database, attach the downloaded JSON to the issue/chat.
Knowing the ID does not grant access: the export is platform-admin-only and uses
`Cache-Control: no-store`. Employee and tenant-HR permissions do not expose other
users' logs.

## Coverage and retention

Full per-turn capture starts with this feature's deployment, including anonymous
and widget streams. It uses the existing `ai_debug_logs` store (MySQL in production,
SQLite only for configured local development). No new database table is required.
Debug capture retains **seven days**, matching the store's existing cleanup;
export promptly for longer investigations. Previously truncated, deleted or
expired messages cannot be recovered. A worker terminated before its finalizer
runs can leave a start entry without an end; the report marks that explicitly.

Captured transcripts are not shortened to the old 500-character response preview.
Existing diagnostic redaction, runtime sampling, GDPR deletion and retention
continue to apply. Storage failures must not break chat and emit a correlated
warning in application logs; the report does not claim unavailable sources exist.
