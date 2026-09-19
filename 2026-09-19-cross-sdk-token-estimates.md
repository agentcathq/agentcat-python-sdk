# AgentCat SDK token estimates — Cross-SDK brief

**Audience:** maintainers of the AgentCat TypeScript, Python and Go SDKs.
**Reference implementation:** `agentcat` (TypeScript) `src/modules/tokenEstimate.ts`.
**Server contract:** `input_tokens` / `output_tokens` on `PublishEventRequest` (agentcat-api 1.0.2), tagged `agentcat:token_source=sdk`.

Every `mcp:tools/call` event carries two integers estimated by the SDK on the
raw payloads, before any redaction hook runs. The rules are pinned
byte-for-byte; do not re-derive them.

## Estimator

`estimate(bytes) = 0` when `bytes == 0`, else `ceil(bytes / 3.5)`, clamped to
`2147483647`. `bytes` is the UTF-8 byte length. The constant is
`TOKEN_ESTIMATE_BYTES_PER_TOKEN` (TypeScript, Python) / `BytesPerToken` (Go).

## Input side: `input_tokens`

The compact JSON of the raw, unstripped `arguments` object, injected
parameters included. Compact separators, no ASCII escaping
(`ensure_ascii=False`), no HTML escaping (`SetEscapeHTML(false)`), no trailing
newline. Not the tool name, `_meta`, `extra`, or the JSON-RPC envelope.
Absent or null arguments → field omitted. `{}` → 1. Adapters that cannot
tell absent arguments from an empty object (Python's three adapters and the
Go official-SDK adapter, which hand the funnel a map either way) count `{}`
→ 1; TypeScript and Go mcp-go omit.

## Output side: `output_tokens`

The summed UTF-8 bytes of `text` blocks and string `resource.text` values in
`content`, divided once. Image, audio, blob and unknown blocks count 0. Not
`structuredContent`, `isError`, `_meta`, the envelope, or the mint-back text.
No `content` list → the compact JSON of the whole recorded response. Absent
response → field omitted. Content with no text-bearing block → 0. The Go
adapters record no `Response` on `isError` results, so Go omits
`output_tokens` there while TypeScript and Python count the error text.

## Serialization corner cases

Non-canonical numbers (`1.0`; integers at or above 1e21, which
`JSON.stringify` writes as `1e+21`) and U+2028/U+2029 (Go always escapes
them to six bytes) can differ by a byte or two between SDKs, all within ±1
token. Lone surrogates count 3 bytes in every SDK.

## Ordering

Both values are set in the tool-call wrapper on the raw objects, before the
queue runs `redact_event` → `redact_sensitive_information` → sanitize →
truncate. Nothing recomputes them afterwards.

## Vectors

| Arguments | Bytes | Tokens |
|---|---|---|
| `{"q":"hello"}` | 13 | 4 |
| `{}` | 2 | 1 |
| `{"name":"café"}` | 16 | 5 |
| `{"html":"<a>&</a>"}` | 19 | 6 |
| `{"t":"你好"}` | 14 | 4 |
| `{"ids":[1,2,3],"opts":{"deep":true,"n":null}}` | 45 | 13 |

| Content | Bytes | Tokens |
|---|---|---|
| text `"hello world"` | 11 | 4 |
| text `"abcd"` + text `"e"` | 5 | 2 |
| text `""` | 0 | 0 |
| image block | 0 | 0 |
| resource with text `"resource body"` | 13 | 4 |
| resource with blob only | 0 | 0 |
| text `"hi"` + 1000-byte `structuredContent` | 2 | 1 |
| `{"result":"ok"}` (no content) | 15 | 5 |
| text of 4096 `x` | 4096 | 1171 |

| Bytes | Tokens |
|---|---|
| 0 | 0 |
| 1 | 1 |
| 7 | 2 |
| 7516192765 | 2147483647 |

## Measurement basis

1,240 production responses (2026-09-12 to 2026-09-19), tokenized with
tiktoken `cl100k_base` and `o200k_base`: inner text 3.71 bytes per token
weighted (p10 3.13, p50 3.75, p90 4.64), the two encodings within 0.2 percent;
JSON 3.72, prose 4.52; the serialized envelope inflates bytes and tokens by
1.19×. Claude-family tokenizers use roughly 15 to 20 percent more tokens
(Anthropic guidance, not yet measured); 3.5 splits the difference.
