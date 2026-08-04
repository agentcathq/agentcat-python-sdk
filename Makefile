# AgentCat Python SDK — example server orchestration.
#
# Mirrors the Go SDK's run-examples / stop-examples targets. Each example is a
# self-contained PEP 723 script: `uv run` resolves it into an isolated, cached
# environment (the first run resolves four distinct dependency sets and can
# take a minute; cached thereafter) and never touches the project's .venv —
# safe to use from a legacy-synced checkout (see CONTRIBUTING.md).
# `--no-project` makes that guarantee explicit.

.PHONY: help run-examples stop-examples smoke-examples

EXAMPLE_PORTS := 8090 8091 8092 8093 8094 8095 8096

# Project ID fallthrough: AGENTCAT_PROJECT_ID > MCPCAT_PROJECT_ID > placeholder.
PROJECT_ID_ENV = AGENTCAT_PROJECT_ID=$${AGENTCAT_PROJECT_ID:-$${MCPCAT_PROJECT_ID:-proj_YOUR_PROJECT_ID}}

help:
	@echo "Available targets:"
	@echo "  make run-examples   - Start all example MCP servers in the background"
	@echo "  make stop-examples  - Stop all example servers (by port)"
	@echo "  make smoke-examples - POST an initialize request to every server"

# Start all example servers in the background.
run-examples:
	@echo "Starting all example servers (first run resolves each script's env; may take a minute)..."
	@$(PROJECT_ID_ENV) uv run --no-project examples/officialsdk/factory/main.py & echo "  officialsdk-factory  (pid $$!) → http://localhost:8090/mcp"
	@$(PROJECT_ID_ENV) uv run --no-project examples/officialsdk/basic/main.py & echo "  officialsdk-basic    (pid $$!) → http://localhost:8091/mcp"
	@$(PROJECT_ID_ENV) uv run --no-project examples/officialsdk/advanced/main.py & echo "  officialsdk-advanced (pid $$!) → http://localhost:8092/mcp"
	@$(PROJECT_ID_ENV) uv run --no-project examples/officialsdk/legacy/main.py & echo "  officialsdk-legacy   (pid $$!) → http://localhost:8093/mcp"
	@$(PROJECT_ID_ENV) uv run --no-project examples/fastmcp/basic/main.py & echo "  fastmcp-basic        (pid $$!) → http://localhost:8094/mcp"
	@$(PROJECT_ID_ENV) uv run --no-project examples/fastmcp/advanced/main.py & echo "  fastmcp-advanced     (pid $$!) → http://localhost:8095/mcp"
	@$(PROJECT_ID_ENV) uv run --no-project examples/fastmcp/v3/main.py & echo "  fastmcp-v3           (pid $$!) → http://localhost:8096/mcp"
	@echo "All servers started. Use 'make stop-examples' to stop them."

# Stop all example servers (by the ports they listen on).
stop-examples:
	@echo "Stopping example servers..."
	@for port in $(EXAMPLE_PORTS); do kill $$(lsof -ti:$$port) 2>/dev/null || true; done
	@echo "Done."

# Prove every server answers an MCP initialize over Streamable HTTP.
smoke-examples:
	@fail=0; for port in $(EXAMPLE_PORTS); do \
		code=$$(curl -s -o /dev/null -w "%{http_code}" -m 5 -X POST "http://localhost:$$port/mcp" \
			-H "Content-Type: application/json" \
			-H "Accept: application/json, text/event-stream" \
			-d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"0.0.0"}}}'); \
		if [ "$$code" = "200" ]; then echo "  port $$port OK"; \
		else echo "  port $$port FAIL (HTTP $$code)"; fail=1; fi; \
	done; exit $$fail
