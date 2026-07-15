from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


SENSITIVE_QUERY_KEYS = {
    "api_key",
    "apikey",
    "auth",
    "auth_key",
    "authorization",
    "expires",
    "key",
    "ossaccesskeyid",
    "signature",
    "token",
}


def _is_sensitive_query_key(key: str) -> bool:
    normalized = key.lower()
    return (
        normalized in SENSITIVE_QUERY_KEYS
        or normalized.startswith("x-amz-")
        or normalized.startswith("x-oss-")
    )


def strip_sensitive_query(url: str) -> str:
    parsed = urlparse(url)
    params = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not _is_sensitive_query_key(key)
    ]
    return urlunparse(parsed._replace(query=urlencode(params)))
