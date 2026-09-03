## General intent

Outcome: execute a `$hi` or `/hi` request that has no deterministic Atelier
intent match through the active runtime's existing semantic routing.
Done when: the primary request is handled by the best matching skill, app,
agent, or normal tool path.
Evidence: the chosen capability and its ordinary completion evidence.
Output: one brief semantic-routing announcement, then the requested result.

Treat `intents.general` as a handoff, not as a user intent. Infer the primary
request from the full message and use an existing capability when one fits;
otherwise answer or act with the runtime's normal tools. Do not start daily
reflection or load reflection profiles solely because no row's description
fit. Clarify only when the request is materially ambiguous or unsafe to
default.
