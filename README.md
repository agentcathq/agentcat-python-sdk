<div align="center">
  <img alt="AgentCat — see exactly how agents experience your product" src="docs/static/og-image.png" width="80%">
</div>
<h3 align="center">
    <a href="#getting-started">Getting Started</a>
    <span> · </span>
    <a href="#why-use-agentcat-">Features</a>
    <span> · </span>
    <a href="https://docs.agentcat.com">Docs</a>
    <span> · </span>
    <a href="https://agentcat.com">Website</a>
    <span> · </span>
    <a href="#free-for-open-source">Open Source</a>
    <span> · </span>
    <a href="https://meet.agentcat.com/meet">Schedule a Demo</a>
</h3>
<p align="center">
  <a href="https://badge.fury.io/py/agentcat"><img src="https://badge.fury.io/py/agentcat.svg" alt="PyPI version"></a>
  <a href="https://pypi.org/project/agentcat/"><img src="https://img.shields.io/pypi/dm/agentcat.svg" alt="PyPI downloads"></a>
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.10+-blue.svg" alt="Python"></a>
  <a href="https://github.com/agentcathq/agentcat-python-sdk/issues"><img src="https://img.shields.io/github/issues/agentcathq/agentcat-python-sdk.svg" alt="GitHub issues"></a>
  <a href="https://github.com/agentcathq/agentcat-python-sdk/actions"><img src="https://github.com/agentcathq/agentcat-python-sdk/workflows/MCP%20Version%20Compatibility%20Testing/badge.svg" alt="CI"></a>
</p>

> [!IMPORTANT]
> **AgentCat 2.0 is here.** MCP 2026-07-28 removed protocol-level sessions, so AgentCat now correlates work with an explicit `session_id` handle that agents echo back as a tool parameter. Handles land in the same `session_id` event field with the same `ses_` prefix, so your dashboards and exporters are unaffected — but tracked tools gain a `session_id` parameter, `AgentCatOptions.stateless` is gone, and FastMCP 2.x is no longer supported. Read the [1.x → 2.0 migration guide](https://github.com/agentcathq/agentcat-python-sdk/blob/main/MIGRATION.md) before upgrading a production server.

> [!IMPORTANT]
> **MCPcat is now AgentCat** 🐱 — same team, same product, new name. This package was previously published as [`mcpcat`](https://pypi.org/project/mcpcat/), which keeps working forever, but new features land here. Upgrading takes a few minutes — see the [migration guide](https://github.com/agentcathq/agentcat-python-sdk/blob/main/MIGRATION.md).

> [!NOTE]
> Looking for the TypeScript SDK? Check it out here [agentcat-typescript](https://github.com/agentcathq/agentcat-typescript-sdk).

AgentCat is an analytics platform for MCP server owners 🐱. It captures user intentions and behavior patterns to help you understand what AI users actually need from your tools — eliminating guesswork and accelerating product development all with one-line of code.

This SDK also provides a free and simple way to forward telemetry like logs, traces, and errors to any Open Telemetry collector or popular tools like Datadog and Sentry.

```bash
# Basic installation (includes official MCP SDK)
pip install agentcat

# With Jlowin's/Prefect's FastMCP support
pip install "agentcat[community]"
```

One `track()` call covers every supported server shape:

| Runtime | Supported |
| --- | --- |
| Official MCP SDK (`mcp`) 1.x — low-level `Server` and `mcp.server.fastmcp.FastMCP` | ✅ |
| Official MCP SDK (`mcp`) 2.x — low-level server and `MCPServer` | ✅ |
| Community FastMCP (`fastmcp`) 3.x and 4.x | ✅ (`agentcat[community]`) |
| Community FastMCP (`fastmcp`) 2.x | ❌ pin `agentcat<2` |

The floors are `mcp>=1.2.0,<3` and `fastmcp>=3.0.0,<5`, and every minor in
both ranges is exercised on each change by the compatibility matrix.

AgentCat runs on the whole range, but the oldest MCP releases lack the SDK
seams some features are built on, so those features go quiet rather than
break:

| On | What is missing upstream | Effect |
| --- | --- | --- |
| `mcp<1.10` | `Server._make_error_result`; declared structured tool output | A bare low-level handler's exception type and stack frames cannot be recovered — the surfaced message is still published. No structured mint-back. |
| `mcp<1.9.2` | `RequestContext.request` | No header or `requestInfo` capture. |
| `mcp<1.8` | Streamable HTTP | stdio only. |
| `mcp<1.3` | Concurrent message handling | The server never has two calls in flight. |
| `fastmcp<3.4` | `ToolResult.is_error` | A proxied upstream error arrives as a raised `ToolError`, so it is published *with* full exception detail rather than as a bare message. |

To learn more about us, check us out [here](https://agentcat.com)

## Why use AgentCat? 🤔

AgentCat helps developers and product owners build, improve, and monitor their MCP servers by capturing user analytics and tracing tool calls.

Use AgentCat for:

- **User session replay** 🎬. Follow alongside your users to understand why they're using your MCP servers, what functionality you're missing, and what clients they're coming from.
- **Trace debugging** 🔍. See where your users are getting stuck, track and find when LLMs get confused by your API, and debug sessions across all deployments of your MCP server.
- **Existing platform support** 📊. Get logging and tracing out of the box for your existing observability platforms (OpenTelemetry, Datadog, Sentry) — eliminating the tedious work of implementing telemetry yourself.

<img alt="AgentCat architecture — the AgentCat SDK inside your MCP server sends analytics to your observability vendors and session replay to the AgentCat dashboard" src="docs/static/architecture.png" />

## Getting Started

To get started with AgentCat, first create an account and obtain your project ID by signing up at [agentcat.com](https://agentcat.com). For detailed setup instructions visit our [documentation](https://docs.agentcat.com).

Once you have your project ID, integrate AgentCat into your MCP server:

```python
import agentcat
from mcp.server.mcpserver import MCPServer   # official MCP SDK 2.x

server = MCPServer("echo-mcp")

agentcat.track(server, "proj_0000000")
```

On the official SDK's 1.x line the server class is `mcp.server.fastmcp.FastMCP` instead; everything after it is identical.

```python
import agentcat
from mcp.server.fastmcp import FastMCP       # official MCP SDK 1.x

server = FastMCP("echo-mcp")

agentcat.track(server, "proj_0000000")
```

> [!NOTE]
> A fresh `pip install` resolves `mcp` **2.x**, where `mcp.server.fastmcp` no longer exists — the 1.x snippet above only runs on a checkout that already pins `mcp<2`. AgentCat supports both generations from one install; which import you use is decided by the `mcp` you have, not by AgentCat.

`track()` classifies whatever object you hand it and installs the matching adapter, so the same call covers a low-level `Server`, either official facade, and community FastMCP. It **never raises** — a shape AgentCat does not support is logged to `~/agentcat.log` and your server comes back untracked rather than failing to start.

### Session handles: how work is correlated

MCP 2026-07-28 removed protocol-level sessions, so AgentCat correlates a series of calls with an explicit **session handle** instead. Tracked tools gain an optional `session_id` string parameter; AgentCat mints one on the first call, tells the agent to echo it back, and strips it out again before your handler runs. Your tool signatures and your handler code are untouched.

AgentCat honors only handles it issued. A `session_id` that is not a `ses_` KSUID this server minted is rejected rather than adopted — the call publishes without a session and the agent is told to re-send the ID it was given, or to omit the parameter and be issued one. That keeps a hallucinated value, or an auth token a client auto-populated into a parameter of the same name, out of the event field your redaction hook cannot reach.

**If one of your own tools already declares a** `session_id` **parameter,** AgentCat leaves it alone: your value reaches your handler untouched and is never read as a handle. Calls to that tool publish **without a session**, so they cannot be correlated, and an `ERROR` naming the tool goes to `~/agentcat.log`. If you already manage sessions, pass `resolve_session_id` — AgentCat then derives its own handle from your identifier and stops injecting `session_id` entirely.

Handles land in the existing `session_id` event field with the same `ses_` prefix, so dashboards, saved filters, and exporters carry over unchanged. See the [migration guide](https://github.com/agentcathq/agentcat-python-sdk/blob/main/MIGRATION.md) for exactly what this adds to your published schemas.

### Track inside your server factory

`track()` mutates the server instance it is given, so it has to run on the *same* instance that serves traffic. If you build servers in a factory — the usual shape for tests, for multi-tenant hosting, or for a per-worker server — call `track()` inside the factory, on the instance you are about to return:

```python
import agentcat
from mcp.server.mcpserver import MCPServer

def create_server() -> MCPServer:
    server = MCPServer("echo-mcp")

    @server.tool()
    def echo(text: str) -> str:
        return text

    agentcat.track(server, "proj_0000000")
    return server
```

Tools registered *after* `track()` are picked up automatically — there is no need to re-track, and calling `track()` twice on one server is safe (the most recent options win).

### Identifying users

You can identify the actor behind a call with a simple callback AgentCat exposes, called `identify`. It runs on **every** tool call and its result is stamped on that call's event, so keep it cheap — add your own caching if it does a database or API lookup.

```python
from agentcat import AgentCatOptions, UserIdentity

def identify_user(request, extra):
    # `request` is the tool call's PARAMS on every server flavor: `.name` and
    # `.arguments`, one hop, not two. `arguments` is a plain dict — index it,
    # don't reach for an attribute.
    user = myapi.get_user(request.arguments["token"])
    return UserIdentity(
            user_id=user.id,
            user_name=user.name,
            user_data={
                "favorite_color": user.favorite_color,
            },
    )

agentcat.track(server, "proj_0000000", AgentCatOptions(identify=identify_user))
```

The hook may be sync or async — `async def identify_user(request, extra)` works the same way, and is the better shape when the lookup is a network call, since a blocking one holds up every other tool call in flight. Returning anything that is not a `UserIdentity`, or raising, publishes the event anonymously rather than failing the call.

### Bringing your own session IDs

If you already have a correlation ID — a trace ID, a job ID, a header your gateway sets — hand it to AgentCat with `resolve_session_id` and no `session_id` parameter is injected into your tools at all:

```python
import agentcat
from agentcat import AgentCatOptions

def session_from_header(request, extra):
    headers = getattr(getattr(extra, "request", None), "headers", {}) or {}
    return headers.get("x-correlation-id")

agentcat.track(server, "proj_0000000", AgentCatOptions(resolve_session_id=session_from_header))
```

The string you return is combined with your project ID into a deterministic `ses_` handle, so the same correlation ID always maps to the same session. The hook may be sync or async, receives the same `(request, extra)` pair as `identify`, and returning `None` (or raising) quietly mints a fresh handle instead.

### Tracking individual agents

Set `enable_agent_tracking=True` to also distinguish *which* agent is working — subagents get their own identity while sharing their parent's session. This adds an `agent_id` parameter to your tools and, unlike `session_id`, marks it **required**, which schema-validating clients will enforce. That is why it is off by default.

```python
import agentcat
from agentcat import AgentCatOptions

agentcat.track(server, "proj_0000000", AgentCatOptions(enable_agent_tracking=True))
```

Agent identity rides on events as the `agentcat_agent_id` tag. A call that omits `agent_id` still succeeds — the event is simply published without it.

### Publishing your own events

Not everything worth seeing on the timeline is a tool call. `publish_custom_event` records background jobs, webhooks, or checkout steps against a session:

```python
import agentcat

agentcat.publish_custom_event(server, "proj_0000000", {
    "session_id": "ses_2cOHEO0LYGADMzRvWTXXVbbgxgm",
    "resource_name": "checkout",
    "message": "order confirmed",
})
```

The first argument may be a tracked server or a bare session-ID string. `session_id` is used **verbatim** — never validated, derived or reformatted, since it is your deliberate server-side call rather than an agent's guess — and an event published without one lands untethered to any session. Events are typed `agentcat:custom`, are fire-and-forget, and never raise. A server tracked with `enable_tracing=False` publishes nothing here either.

### Redacting sensitive data

AgentCat redacts all data sent to its servers and encrypts at rest, but for additional security, it offers a hook to do your own redaction on all text data returned back to our servers.

```python
from agentcat import AgentCatOptions

def redact(text):
    return custom_redact(text)

agentcat.track(server, "proj_0000000", AgentCatOptions(redact_sensitive_information=redact))
```

The hook may be sync or async. It runs on every string in the event except the fields AgentCat needs to attribute it — the session handle, the event, project and resource IDs, the actor your `identify` hook returned, and your own tags and properties. If it raises, the event is dropped rather than published unredacted.

### Forwarding data to existing observability platforms

AgentCat seamlessly integrates with your existing observability stack, providing automatic logging and tracing without the tedious setup typically required. Export telemetry data to multiple platforms simultaneously:

```python
import os

from agentcat import AgentCatOptions

agentcat.track(
    server,
    "proj_0000000", # Or None if you just want to use the SDK to forward telemetry
    AgentCatOptions(
        exporters={
            # OpenTelemetry - works with Jaeger, Tempo, New Relic, etc.
            "otlp": {
                "type": "otlp",
                "endpoint": "http://localhost:4318/v1/traces",
            },
            # Datadog
            "datadog": {
                "type": "datadog",
                "api_key": os.getenv("DD_API_KEY"),
                "site": "datadoghq.com",
                "service": "my-mcp-server",
            },
            # Sentry
            "sentry": {
                "type": "sentry",
                "dsn": os.getenv("SENTRY_DSN"),
                "environment": "production",
            },
        }
    )
)
```

Learn more about our free and open source [telemetry integrations](https://docs.agentcat.com/telemetry/integrations).

### Known limitations

Two behaviors are worth knowing before you read your first dashboard. Both are deliberate in the 2.x line.

- **Multi-round tool calls that mint their own handle land on separate sessions.** When a tool call runs several round trips and the *first* round is what mints the handle, each round is attributed to its own session rather than one shared session. Supplying a `session_id` yourself, or deriving one with `resolve_session_id`, correlates the rounds correctly — those two modes are protocol-enforced. Only the mint-on-first-round case is affected.
- **Errors forwarded from a proxied community tool carry no stack detail.** When a community FastMCP server proxies a tool to an upstream server and that upstream returns an error result, there is no local Python exception to read, so the event records the error message without a stack trace. Errors raised by your own tool code are unaffected and carry full detail.

### Internal diagnostics

To help us catch and fix broken installs, the SDK sends AgentCat a small, anonymized
signal when setup or runtime errors occur — never your tool calls, your responses,
or anything about your users. Records carry only operational metadata, such as your
project ID (or an anonymous install ID when none is set). Your local `~/agentcat.log`
is unchanged.

Diagnostics are on by default and can be turned off completely with either:

- `track(server, project_id, AgentCatOptions(disable_diagnostics=True))`, or
- the `DISABLE_DIAGNOSTICS` environment variable.

## Free for open source

AgentCat is free for qualified open source projects. We believe in supporting the ecosystem that makes MCP possible. If you maintain an open source MCP server, you can access our full analytics platform at no cost.

**How to apply**: Email hi@agentcat.com with your repository link

_Already using AgentCat? We'll upgrade your account immediately._

## Community Cats 🐱

Meet the cats behind AgentCat! Add your cat to our community by submitting a PR with your cat's photo in the `docs/cats/` directory.

<div align="left">
  <img src="docs/cats/bibi.png" alt="bibi" width="80" height="80">
  <img src="docs/cats/zelda.jpg" alt="zelda" width="80" height="80">
</div>

_Want to add your cat? Create a PR adding your cat's photo to `docs/cats/` and update this section!_
