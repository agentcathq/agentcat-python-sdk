INACTIVITY_TIMEOUT_IN_MINUTES = 30
LOG_PATH = "agentcat.log"  # Default log file path
SESSION_ID_PREFIX = "ses"
EVENT_ID_PREFIX = "evt"
AGENTCAT_API_URL = "https://api.agentcat.com"  # Default API URL for AgentCat events
AGENTCAT_SOURCE = "agentcat"  # Source attribution for telemetry exporters
AGENTCAT_CUSTOM_EVENT_TYPE = "agentcat:custom"  # Event type for publish_custom_event
# Event types defined by this SDK (all "agentcat:"-prefixed) that the generated
# API client's enum doesn't know about; types.Event lets these bypass the
# generated enum check. Add new SDK-defined event types here.
SDK_EVENT_TYPES = frozenset({AGENTCAT_CUSTOM_EVENT_TYPE})
DEFAULT_CONTEXT_DESCRIPTION = "Explain why you are calling this tool and how it fits into the user's overall goal. This parameter is used for analytics and user intent tracking. YOU MUST provide 15-25 words (count carefully). NEVER use first person ('I', 'we', 'you') - maintain third-person perspective. NEVER include sensitive information such as credentials, passwords, or personal data. Example (20 words): \"Searching across the organization's repositories to find all open issues related to performance complaints and latency issues for team prioritization.\""

# Maximum number of exceptions to capture in a cause chain
MAX_EXCEPTION_CHAIN_DEPTH = 10

# Maximum number of stack frames to capture per exception
MAX_STACK_FRAMES = 50

# Internal SDK diagnostics (privacy-first, metadata-only OTLP logs)
DIAGNOSTICS_SCOPE_NAME = "agentcat-diagnostics"
DEFAULT_DIAGNOSTICS_ENDPOINT = "https://otel.agentcat.com"
# Public shared ingestion key — NOT a secret; ships in the package to deter
# drive-by traffic, paired with a server-side rate limit. Override with the
# DIAGNOSTICS_TOKEN env var. Must match the collector's bearer token (same
# literal as the TypeScript SDK).
DEFAULT_DIAGNOSTICS_TOKEN = "dgk_sdk_diag_3f9a2c7e1b8d4065af2e9c1d7b6a4f80"
