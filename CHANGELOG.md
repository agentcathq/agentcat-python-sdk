# Changelog

This file lists the notable changes of each release of `agentcat` on PyPI.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the SDK follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security

- The SDK now masks sensitive values in every event before the event leaves your server. It tests
  each key in `parameters`, `response` and `error`, at any depth. When the key names a secret, the
  SDK keeps the key and replaces the value with `[redacted]`. A key names a secret when it is a
  known credential header (`authorization`, `cookie`, `x-api-key` and others) or has a word such
  as `token`, `secret`, `password`, `signature` or `api-key`, in any spelling (`X-Api-Key`,
  `HTTP_X_API_KEY`, `xApiKey`). Before, the SDK sent the HTTP headers of each MCP request as
  received, in `parameters.extra.requestInfo.headers`. Tool arguments and results under such key
  names lose their values too, for example `pageToken` and `auth_type`. `null`, `true` and
  `false` stay. No option turns the mask off (#62).

## [2.2.0] - 2026-11-17

### Added

- Each `mcp:tools/call` event has `input_tokens` and `output_tokens`: estimates of the tokens of
  the tool arguments and of the tool result, as the model sees them (`ceil(utf8_bytes / 3.5)`).
  The SDK counts them before your `redact_sensitive_information` and `redact_event` hooks and
  before truncation. To keep these sizes on your server, drop the two fields in `redact_event`.
  The TypeScript and Go SDKs count the same way. Expect numbers about 10 percent lower than the
  estimates that the AgentCat server made before. An error in the estimate never changes the tool
  result (#54).
- The SDK now sends AgentCat a description of your MCP server: its name, version, title and
  instructions, and the lists of its tools, resources, resource templates and prompts. About
  once an hour per process and server name, after your server answers a request, the SDK calls
  your server's own list handlers and sends the result in the background. In a runtime that
  keeps no memory between requests, it sends only on a `tools/list` request or a
  `get_more_tools` call. `redact_sensitive_information` runs on every text; item names and URIs
  stay as they are. No option changes: the send is on when tracking is on, and a failed send
  never reaches your server (#58).

### Changed

- `agentcat` now needs `agentcat-api` 1.0.5, which adds the two token fields (#54) and the server
  info request (#58).

### Fixed

- The tags of the `event_tags` hook and of `publish_custom_event` no longer keep a key that ends
  in a newline, such as `"env\n"`. The SDK drops it, as the TypeScript and Go SDKs do (#55).

## [2.1.0] - 2026-08-31

The `track()` API does not change. The changes are in what your server sends to the agent.

### Changed

- The text block that issues the session ID is now the first content block of the response, not
  the last, so it stays when a client cuts a long result. Published events do not change.
- `session_id` and `agent_id` are in the `required` array of each tool. `session_id` has the
  pattern `^(start|ses_[0-9A-Za-z]{27})$`: the agent sends `start` for a new task, or the issued
  ID. A call without `session_id` works as `start`.
- `mcp_session` replaces `_mcp_instructions` in `structuredContent`. It has `session_id`,
  `agent_id` and a `status` (`issued`, `active` or `unrecognized`) in place of `instructions`.
- An unknown `session_id` gets `mcp_session.status: "unrecognized"` and a first text block that
  starts with `[session_id unrecognized`. The response issues no new ID.
- The descriptions of `session_id` and `agent_id` are shorter, and the two `agent_id`
  descriptions are now one. The text is the same in all AgentCat SDKs.

These changes break only code that reads the response or the schemas, such as tests, proxies, or
clients that read `structuredContent`.

## [2.0.2] - 2026-08-08

### Added

- The `redact_event` option: a hook that gets the full event before the
  `redact_sensitive_information` hook. Change the event and return it, or return `None` to drop
  the event. The hook's return value replaces all fields of the event.

### Changed

- `redact_sensitive_information` no longer changes `server_name`, `server_version`,
  `client_name` and `client_version`.

### Security

- After `redact_event`, the SDK restores the fields that it sets itself (`id`, `session_id`,
  `project_id`, `event_type`, `timestamp`, the user identity, `tags`, `properties`, and the
  client and server identity) from a deep copy. A hook cannot forge or erase them.

## [2.0.1] - 2026-08-06

### Fixed

- Servers built on FastMCP Code Mode, and other servers that call themselves during a request,
  work again. A `tools/list` inside a tracked call no longer replaces the tool lists of the agent.
  Before this fix, calls after the first `execute` failed with `Unexpected keyword argument`.
- A `tools/call` inside a tracked call joins the session of the outer call, gets the tag
  `agentcat_nested: "true"`, and its result is never changed.

## [2.0.0] - 2026-08-05

AgentCat 2 correlates tool calls with explicit handles, because the MCP 2026-07-28 specification
removed protocol sessions. It also supports MCP Python SDK 2.x. `MIGRATION.md` has the upgrade
steps.

### Added

- `track()` adds a `session_id` parameter to each tool and issues the ID to the agent, which sends
  it back on each call. This works over stateless HTTP, behind load balancers and with a server
  built per request. Your tool handler never sees the added parameters.
- The `resolve_session_id` hook, to derive the session from your own IDs instead.
- `agent_id`, off by default: turn it on with `enable_agent_tracking=True`.
- Support for MCP Python SDK 2.x: `MCPServer` and the low-level `Server`, beside `FastMCP` and
  the low-level `Server` of 1.x, and community FastMCP 3.x and 4.x.
- Full support of the MCP 2026-07-28 protocol.

### Changed

- A failure inside AgentCat never changes your tool's response. Hooks run with a 5-second limit
  and can never stop your request. The SDK installs no signal handlers and never calls
  `os._exit`. `import agentcat` has no side effects.
- `identify`, `event_tags`, `event_properties` and `resolve_session_id` get the request params of
  the tool call, not the enclosing request.
- `publish_custom_event` takes session IDs as they are, and `CustomEventData.session_id`
  replaces the session references.
- Tool errors keep the full Python exception: type, stack frames and chained causes.

### Removed

- The old session ID machinery, the identity caches and the identify events.
- Support for community FastMCP 2.x. Pin `agentcat<2` if you need it.

## Earlier versions

The [GitHub releases](https://github.com/agentcathq/agentcat-python-sdk/releases) have the notes
of 1.0.0 and of the `mcpcat` releases before it.

[Unreleased]: https://github.com/agentcathq/agentcat-python-sdk/compare/v2.2.0...HEAD
[2.2.0]: https://github.com/agentcathq/agentcat-python-sdk/compare/v2.1.0...v2.2.0
[2.1.0]: https://github.com/agentcathq/agentcat-python-sdk/compare/v2.0.2...v2.1.0
[2.0.2]: https://github.com/agentcathq/agentcat-python-sdk/compare/v2.0.1...v2.0.2
[2.0.1]: https://github.com/agentcathq/agentcat-python-sdk/compare/v2.0.0...v2.0.1
[2.0.0]: https://github.com/agentcathq/agentcat-python-sdk/compare/v1.0.0...v2.0.0
