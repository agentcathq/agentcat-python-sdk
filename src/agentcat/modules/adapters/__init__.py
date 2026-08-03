"""Era-specific wiring between a customer's MCP server and the v2 engine.

One adapter per server generation. Each knows only the mechanics of its era —
which field holds the dispatched handler, how results are wrapped, whether the
model fields are camelCase or snake_case — and delegates every orchestration
decision (resolve / strip / decorate / publish) to
`agentcat.modules.callpath`.

Adapters import `mcp` / `fastmcp` symbols inside their install function, never
at module scope, so `import agentcat` succeeds under either SDK major.
"""
