# BEFORE MODIFYING THIS FILE: Read RULES.md in the project root and follow all rules. No exceptions.
"""
Module: config.py
Purpose: Single source of truth for all application configuration, loading secrets, and defining constants.
Layer Architecture: Accessed globally by all layers.
Contracts Served: Contract 1, 2, and 3.
Dependencies: os, dotenv.
"""
import os
from dotenv import load_dotenv

# Enforces R15: No module reads os.environ directly — all config via config.py
load_dotenv()  # loads .env into os.environ — called once here only

_required = ["GSK_OAUTH_URL", "GSK_CLIENT_ID", "GSK_CLIENT_SECRET", "GSK_GEMINI_BASE_URL"]
for _var in _required:
    if not os.environ.get(_var):
        # Enforces R16: config.py raises EnvironmentError on missing required secrets at import
        raise EnvironmentError(f"[config] Required environment variable '{_var}' is missing. "
                               f"Check your .env file.")

# ── Secrets (from .env) ──────────────────────────────────────────────────────
OAUTH_URL     = os.environ["GSK_OAUTH_URL"]
CLIENT_ID     = os.environ["GSK_CLIENT_ID"]
CLIENT_SECRET = os.environ["GSK_CLIENT_SECRET"]
GSK_BASE_URL  = os.environ["GSK_GEMINI_BASE_URL"].rstrip("/")

# ── Tunables (version-controlled constants — change here, not in .env) ───────
ALLOWED_MODEL            = "gemini-3.1-pro-preview"  # GSK gateway allowed model
PROXY_PORT               = 5000                       # Flask listen port
TOKEN_EXPIRY_BUFFER_SECS = 60      # Refresh token this many seconds before expiry
SESSION_TTL_SECS         = 1800    # Evict session after 30 min inactivity
SESSION_CLEANUP_INTERVAL = 300     # Run TTL cleanup every 5 min
UPSTREAM_CONNECT_TIMEOUT = 30      # Seconds to wait for upstream TCP connection
UPSTREAM_READ_TIMEOUT    = 300     # Seconds to wait for upstream response body
LOG_BODY_CHARS           = 300     # How many chars of body to log on each request