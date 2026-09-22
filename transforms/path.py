# BEFORE MODIFYING THIS FILE: Read RULES.md in the project root and follow all rules. No exceptions.
"""
Module: transforms.path
Purpose: URL path transformations, model normalization, and header hygiene.
Layer Architecture: Layer 5 (Header Hygiene) and Routing Helpers.
Contracts Served: Contract 1, 2, and 3.
Dependencies: re, uuid, config.
"""
import re
import uuid
from config import ALLOWED_MODEL, _BASE_URL

_MODEL_PATTERN = re.compile(r'(?<=models/)[^/:]+')

# Matches Google public API version prefixes at the START of a path only.
# Strips v1/, v1beta/, v1alpha/ etc. before forwarding to  Kong gateway
# which has its own versioned base URL and does not accept these prefixes.
_GOOGLE_API_VERSION_PREFIX = re.compile(r'^v\d+(beta|alpha)?/')

def normalize_model(path: str) -> str:
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Rewrites the model-id segment found in the path to ALLOWED_MODEL, regardless
    of what the client requested (Gemini variant, OpenAI-style name, or anything
    else) —  only accepts ALLOWED_MODEL, so client model choice is never honored.

    Parameters: path (str)
    Returns: str (The modified path)
    Raises: None
    Enforces R12: Model regex targets only {model-id} segment via re.search() + slice replacement
    Side effects: Logs model rewrite if changes occurred.
    """
    match = _MODEL_PATTERN.search(path)
    if match:
        original = match.group(0)
        if original != ALLOWED_MODEL:
            # R12: Slice replacement instead of global replace to prevent URL corruption
            path = path[:match.start()] + ALLOWED_MODEL + path[match.end():]
            print(f"  \U0001f500 Model rewritten: {original} \u2192 {ALLOWED_MODEL}")
    return path

def build_target_url(path: str, query_string: str) -> str:
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Constructs the full upstream target URL.
    Strips leading Google API version prefixes (v1/, v1beta/, v1alpha/) from path before
    appending to _BASE_URL. The  Kong gateway has its own versioned base path and
    does not accept these prefixes. Only strips if the path STARTS WITH the prefix to
    avoid corrupting Hermes-style hostname-embedded paths (e.g. generativelanguage.googleapis.com/...).

    Parameters: path (str), query_string (str)
    Returns: str (Full URL)
    Raises: None
    """
    stripped = _GOOGLE_API_VERSION_PREFIX.sub('', path)
    if stripped != path:
        prefix_end = path.index('/') + 1
        print(f"  \u2702\ufe0f  Stripped API version prefix: {path[:prefix_end]}")
    url = f"{_BASE_URL}/{stripped}"
    if query_string:
        url += f"?{query_string}"
    return url

def build_headers(token: str, request_headers) -> dict:
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Strips forbidden headers and injects authentication/tracking headers.

    Parameters: token (str), request_headers (dict-like)
    Returns: dict (Sanitized and enriched headers)
    Raises: None
    """
    excluded = {'host', 'content-length', 'x-goog-api-key',
                'authorization', 'transfer-encoding'}
    headers = {k: v for k, v in request_headers.items()
               if k.lower() not in excluded}
    headers['Authorization'] = f'Bearer {token}'
    headers['Content-Type']  = 'application/json'
    headers['X-Request-ID']  = str(uuid.uuid4())
    return headers
