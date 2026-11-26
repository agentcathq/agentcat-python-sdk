"""Masks the values of keys that name a secret, such as `authorization` or `x-api-key`.

A port of the shared AgentCat reference implementation. Every SDK passes the same cases
in `tests/sensitive_keys_vectors.json`.
"""

from typing import Any

MASK = "[redacted]"

# A key matches a name when its canonical form equals the name or ends with "-" + name.
EXACT = [
    "accesskey", "cb-access-sign", "cf-access-client-id", "dpop", "fastly-key",
    "jenkins-crumb", "paypal-transmission-sig", "plaid-verification",
    "sec-websocket-protocol", "x-airtable-content-mac", "x-amzn-ava-user-context",
    "x-amzn-oidc-data", "x-firebase-appcheck", "x-forge-oauth-system",
    "x-forge-oauth-user", "x-honeycomb-team", "x-insert-key", "x-jfrog-art-api",
    "x-nf-sign", "x-registry-config", "x-requestdigest", "x-sfdc-session",
    "x-trello-webhook", "x-vault-mfa", "x-vercel-protection-bypass", "x-wp-nonce",
]  # fmt: skip
# A key matches when its canonical form contains the word.
CONTAINS = [
    "authorization", "secret", "password", "passwd", "passphrase", "cookie",
    "signature", "credential", "jwt", "assertion", "bearer", "csrf", "xsrf", "hmac",
    "saml",
    "api-key", "apikey", "access-key", "private-key", "privatekey", "subscription-key",
    "encryption-key", "signing-key", "shared-key", "sharedkey", "master-key",
    "account-key", "app-key", "appkey", "application-key", "functions-key", "sas-key",
    "license-key", "consumer-key", "client-key", "customer-key",
]  # fmt: skip
# A key matches when one hyphen-separated part of its canonical form equals the word.
SEGMENT = ["auth", "token", "otp"]
# A key matches when its canonical form ends with the word.
SUFFIX = ["token"]

# The pair rule: a map that names a key in one entry and holds its value in another.
PAIR_NAME_KEYS = ["name", "key", "header"]
PAIR_VALUE_KEYS = ["value", "values"]
# The children of a map stored under this exact key are JSON Schema property names.
SCHEMA_PROPERTIES_KEY = "properties"


def camel_split(text: str) -> str:
    out = []
    for i, ch in enumerate(text):
        if i > 0 and "A" <= ch <= "Z":
            prev = text[i - 1]
            nxt = text[i + 1] if i + 1 < len(text) else ""
            if ("a" <= prev <= "z") or ("0" <= prev <= "9"):
                out.append("-")
            elif ("A" <= prev <= "Z") and ("a" <= nxt <= "z"):
                out.append("-")
        out.append(ch)
    return "".join(out)


def canonical_parts(text: str) -> list[str]:
    out = []
    for ch in text:
        if "A" <= ch <= "Z":
            out.append(chr(ord(ch) + 32))
        elif ("a" <= ch <= "z") or ("0" <= ch <= "9"):
            out.append(ch)
        else:
            out.append("-")
    parts = [p for p in "".join(out).split("-") if p]
    while parts and all("0" <= c <= "9" for c in parts[-1]):
        parts.pop()
    return parts


def parts_match(parts: list[str]) -> bool:
    if not parts:
        return False
    joined = "-".join(parts)
    for name in EXACT:
        if joined == name or joined.endswith("-" + name):
            return True
    for word in CONTAINS:
        if word in joined:
            return True
    for part in parts:
        if part in SEGMENT:
            return True
    for word in SUFFIX:
        if joined.endswith(word):
            return True
    return False


def is_sensitive_key(key: str) -> bool:
    return parts_match(canonical_parts(key)) or parts_match(
        canonical_parts(camel_split(key))
    )


def canonical(key: str) -> str:
    return "-".join(canonical_parts(key))


def mask_value(value: Any, under_properties: bool) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, dict) and under_properties:
        return mask_sensitive_keys(value, False)
    return MASK


def mask_sensitive_keys(value: Any, under_properties: bool = False) -> Any:
    """A masked copy of `value`. `under_properties` is true when `value` is the map
    stored under the key "properties"."""
    if isinstance(value, list):
        return [mask_sensitive_keys(item, False) for item in value]
    if not isinstance(value, dict):
        return value
    out = {}
    for key, child in value.items():
        if isinstance(key, str) and is_sensitive_key(key):
            out[key] = mask_value(child, under_properties)
        else:
            out[key] = mask_sensitive_keys(child, key == SCHEMA_PROPERTIES_KEY)
    names_a_sensitive_key = False
    for key, child in value.items():
        if isinstance(key, str) and canonical(key) in PAIR_NAME_KEYS:
            if isinstance(child, str) and is_sensitive_key(child):
                names_a_sensitive_key = True
    if names_a_sensitive_key:
        for key, child in value.items():
            if isinstance(key, str) and canonical(key) in PAIR_VALUE_KEYS:
                out[key] = mask_value(child, False)
    return out
