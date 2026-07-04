> [!IMPORTANT]
> **MCPcat is now AgentCat** 🐱 — same team, same product, new name. This package has been renamed to [`agentcat`](https://pypi.org/project/agentcat/) (`pip install agentcat`, starts at v1.0.0). `mcpcat` keeps working forever, but new features land only in `agentcat`. Upgrading takes a few minutes — see the [migration guide](./MIGRATION.md).

<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/static/logo-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="docs/static/logo-light.svg">
    <img alt="MCPcat Logo" src="docs/static/logo-light.svg" width="80%">
  </picture>
</div>
<h3 align="center">
    <a href="#getting-started">Getting Started</a>
    <span> · </span>
    <a href="#why-use-mcpcat-">Features</a>
    <span> · </span>
    <a href="https://docs.mcpcat.io">Docs</a>
    <span> · </span>
    <a href="https://mcpcat.io">Website</a>
    <span> · </span>
    <a href="#free-for-open-source">Open Source</a>
    <span> · </span>
    <a href="https://meet.mcpcat.io/meet">Schedule a Demo</a>
</h3>
<p align="center">
  <a href="https://badge.fury.io/py/mcpcat"><img src="https://badge.fury.io/py/mcpcat.svg" alt="PyPI version"></a>
  <a href="https://pypi.org/project/mcpcat/"><img src="https://img.shields.io/pypi/dm/mcpcat.svg" alt="PyPI downloads"></a>
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.10+-blue.svg" alt="Python"></a>
  <a href="https://github.com/MCPCat/mcpcat-python-sdk/issues"><img src="https://img.shields.io/github/issues/MCPCat/mcpcat-python-sdk.svg" alt="GitHub issues"></a>
  <a href="https://github.com/MCPCat/mcpcat-python-sdk/actions"><img src="https://github.com/MCPCat/mcpcat-python-sdk/workflows/MCP%20Version%20Compatibility%20Testing/badge.svg" alt="CI"></a>
</p>

> [!NOTE]
> Looking for the TypeScript SDK? Check it out here [mcpcat-typescript](https://github.com/mcpcat/mcpcat-typescript-sdk).

MCPcat is an analytics platform for MCP server owners 🐱. It captures user intentions and behavior patterns to help you understand what AI users actually need from your tools — eliminating guesswork and accelerating product development all with one-line of code.

This SDK also provides a free and simple way to forward telemetry like logs, traces, and errors to any Open Telemetry collector or popular tools like Datadog and Sentry. 

```bash
# Basic installation (includes official MCP SDK)
pip install mcpcat

# With Jlowin's/Prefect's FastMCP support
pip install "mcpcat[community]"
```

To learn more about us, check us out [here](https://mcpcat.io)

## Why use MCPcat? 🤔

MCPcat helps developers and product owners build, improve, and monitor their MCP servers by capturing user analytics and tracing tool calls.

Use MCPcat for:

- **User session replay** 🎬. Follow alongside your users to understand why they're using your MCP servers, what functionality you're missing, and what clients they're coming from.
- **Trace debugging** 🔍. See where your users are getting stuck, track and find when LLMs get confused by your API, and debug sessions across all deployments of your MCP server.
- **Existing platform support** 📊. Get logging and tracing out of the box for your existing observability platforms (OpenTelemetry, Datadog, Sentry) — eliminating the tedious work of implementing telemetry yourself.

<img width="1274" height="770" alt="mcpcat-diagram" src="https://github.com/user-attachments/assets/36615b3c-7267-4b01-a055-856105c432cb" />

## Getting Started

To get started with MCPcat, first create an account and obtain your project ID by signing up at [mcpcat.io](https://mcpcat.io). For detailed setup instructions visit our [documentation](https://docs.mcpcat.io).

Once you have your project ID, integrate MCPcat into your MCP server:

```python
import mcpcat
from mcp.server import FastMCP

server = FastMCP(name="echo-mcp", version="1.0.0")

mcpcat.track(server, "proj_0000000")
```

### Identifying users

You can identify your user sessions with a simple callback MCPcat exposes, called `identify`.

```python
def identify_user(request, extra):
    user = myapi.get_user(request.params.arguments.token)
    return UserIdentity(
            user_id=user.id,
            user_name=user.name,
            user_data={
                "favorite_color": user.favorite_color,
            },
    )

mcpcat.track(server, "proj_0000000", MCPCatOptions(identify=identify_user))
```

### Redacting sensitive data

MCPcat redacts all data sent to its servers and encrypts at rest, but for additional security, it offers a hook to do your own redaction on all text data returned back to our servers.

```python
# Sync version
def redact_sync(text):
    return custom_redact(text)

mcpcat.track(server, "proj_0000000", redact_sensitive_information=redact_sync)
```

### Forwarding data to existing observability platforms

MCPcat seamlessly integrates with your existing observability stack, providing automatic logging and tracing without the tedious setup typically required. Export telemetry data to multiple platforms simultaneously:

```python
from mcpcat import MCPCatOptions, ExporterConfig

mcpcat.track(
    server, 
    "proj_0000000", # Or None if you just want to use the SDK to forward telemetry
    MCPCatOptions(
        exporters={
            # OpenTelemetry - works with Jaeger, Tempo, New Relic, etc.
            "otlp": ExporterConfig(
                type="otlp",
                endpoint="http://localhost:4318/v1/traces"
            ),
            # Datadog
            "datadog": ExporterConfig(
                type="datadog",
                api_key=os.getenv("DD_API_KEY"),
                site="datadoghq.com",
                service="my-mcp-server"
            ),
            # Sentry
            "sentry": ExporterConfig(
                type="sentry",
                dsn=os.getenv("SENTRY_DSN"),
                environment="production"
            )
        }
    )
)
```

Learn more about our free and open source [telemetry integrations](https://docs.mcpcat.io/telemetry/integrations).

### Internal diagnostics

To help us catch and fix broken installs, the SDK sends MCPcat a small, anonymized
signal when setup or runtime errors occur — never your tool calls, your responses,
or anything about your users. Records carry only operational metadata, such as your
project ID (or an anonymous install ID when none is set). Your local `~/mcpcat.log`
is unchanged.

Diagnostics are on by default and can be turned off completely with either:

- `track(server, project_id, MCPCatOptions(disable_diagnostics=True))`, or
- the `DISABLE_DIAGNOSTICS` environment variable.

## Free for open source

MCPcat is free for qualified open source projects. We believe in supporting the ecosystem that makes MCP possible. If you maintain an open source MCP server, you can access our full analytics platform at no cost.

**How to apply**: Email hi@mcpcat.io with your repository link

_Already using MCPcat? We'll upgrade your account immediately._

## Community Cats 🐱

Meet the cats behind MCPcat! Add your cat to our community by submitting a PR with your cat's photo in the `docs/cats/` directory.

<div align="left">
  <img src="docs/cats/bibi.png" alt="bibi" width="80" height="80">
  <img src="docs/cats/zelda.jpg" alt="zelda" width="80" height="80">
</div>

_Want to add your cat? Create a PR adding your cat's photo to `docs/cats/` and update this section!_
