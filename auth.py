# BEFORE MODIFYING THIS FILE: Read RULES.md in the project root and follow all rules. No exceptions.
"""
Module: auth.py
Purpose: Thread-safe OAuth2 token lifecycle management.
Layer Architecture: Layer 2 (Token fetch).
Contracts Served: Contract 1, 2, and 3.
Dependencies: time, threading, requests, config.
"""
import time
import threading
import requests
import config

class TokenManager:
    """
    BEFORE MODIFYING THIS CLASS: Read RULES.md in the project root and follow all rules. No exceptions.
    Purpose: Fetches, caches, and proactively refreshes GSK OAuth2 tokens.
    Thread-safety: Fully thread-safe using an internal threading.Lock.
    Lifecycle: Instantiated once globally at proxy startup.
    """
    def __init__(self):
        """
        BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
        Initializes the TokenManager with an internal thread lock, 
        an empty token cache, and a zeroed expiration time.
        """
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expiry: float = 0

    def get_token(self) -> str:
        """
        BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
        Returns a valid Bearer token.
        Refreshes if within TOKEN_EXPIRY_BUFFER_SECS of expiry.
        Must be the FIRST call in every request handler.
        Never cache the return value — always call get_token() per request.
        
        Parameters: None
        Returns: str (Bearer token)
        Raises: Exception if token refresh fails.
        Side effects: May trigger an outbound HTTP request to the OAuth provider and update internal cache.
        """
        with self._lock:
            if not self._token or time.time() > self._expiry - config.TOKEN_EXPIRY_BUFFER_SECS:
                self._do_refresh()
        return self._token

    def warm_up(self):
        """
        BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
        Called once at startup. Raises immediately if credentials are wrong.
        
        Parameters: None
        Returns: None
        Raises: Exception if credentials fail.
        Side effects: Logs success or failure.
        """
        self.get_token()

    def _do_refresh(self):
        """
        BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
        Internal method to execute the OAuth2 client_credentials grant.
        Caller must hold self._lock.
        
        Parameters: None
        Returns: None
        Raises: requests.exceptions.RequestException on network failure or HTTP errors.
        Side effects: Updates self._token and self._expiry, logs status.
        """
        try:
            # BEFORE MODIFYING THIS BLOCK: Read RULES.md in the project root and follow all rules. No exceptions.
            res = requests.post(config.OAUTH_URL, data={
                "client_id":     config.CLIENT_ID,
                "client_secret": config.CLIENT_SECRET,
                "grant_type":    "client_credentials"
            }, timeout=15)
            res.raise_for_status()
            data = res.json()
            self._token  = data["access_token"]
            self._expiry = time.time() + data.get("expires_in", 900)
            print(f"[{time.strftime('%H:%M:%S')}] ✅ Token refreshed, expires in {data.get('expires_in', 900)}s")
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] ❌ Auth Error: {e}")
            raise