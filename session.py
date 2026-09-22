# BEFORE MODIFYING THIS FILE: Read RULES.md in the project root and follow all rules. No exceptions.
"""
Module: session.py
Purpose: In-memory session store for tracking client states, thoughtSignatures, and tool metadata.
Layer Architecture: Layer 3 (Session R/W).
Contracts Served: Contract 1, 2, and 3.
Dependencies: time, threading, dataclasses, config.
"""
import time
import threading
from dataclasses import dataclass, field
import config

@dataclass
class ToolCallMeta:
    """
    BEFORE MODIFYING THIS CLASS: Read RULES.md in the project root and follow all rules. No exceptions.
    Purpose: Holds metadata for a single tool call to preserve state across turns.
    Thread-safety: Not thread-safe on its own; managed safely by SessionEntry.
    Lifecycle: Created on upstream response, read on downstream request, tied to SessionEntry lifecycle.
    """
    name: str                       # functionCall.name from Gemini response
    tool_type: str | None           # tool_type field if present in part
    thought_signature: str | None   # thoughtSignature value (camelCase, REST wire format)
    part_position: int              # index in parts[] where functionCall appeared
    step: int                       # sequential step number within current turn

@dataclass
class SessionEntry:
    """
    BEFORE MODIFYING THIS CLASS: Read RULES.md in the project root and follow all rules. No exceptions.
    Purpose: Holds all state for a single proxy session.
    Thread-safety: Thread-safe; contains its own threading.Lock.
    Lifecycle: Created dynamically per X-Session-ID, destroyed by TTL cleanup.
    """
    # Enforces R10: threading.Lock per SessionEntry, not global
    lock: threading.Lock = field(default_factory=threading.Lock)
    # Keyed by functionCall.id (== OpenAI tool_call_id)
    tool_call_meta: dict[str, ToolCallMeta] = field(default_factory=dict)
    # Full part objects for built-in tool invocations (Google Search, Code Execution etc.)
    # Must be re-injected verbatim into model turn on next request
    server_side_tool_parts: list = field(default_factory=list)
    # Increments with each sequential function call step within the current turn
    current_turn_step: int = 0
    # Maintains part offset across streaming chunks for the current turn
    stream_part_offset: int = 0
    last_active: float = field(default_factory=time.time)

class SessionStore:
    """
    BEFORE MODIFYING THIS CLASS: Read RULES.md in the project root and follow all rules. No exceptions.
    Purpose: Manages a collection of SessionEntries with automatic TTL eviction.
    Thread-safety: Fully thread-safe.
    Lifecycle: Instantiated once globally at proxy startup.
    """
    def __init__(self):
        """
        BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
        Initializes the session store with a global store lock and an empty dictionary.
        Side effects: Starts the background TTL cleanup daemon thread.
        """
        self._store_lock = threading.Lock()
        self._sessions: dict[str, SessionEntry] = {}
        self._cleanup_timer = None
        self._start_cleanup_timer()
        # WORK-08/09 / TODO-03 spec section 4.10 Option A: in-memory cache for
        # OpenAI Responses API GET/DELETE. Lost on proxy restart -- documented
        # limitation. Separate lock since access pattern differs from sessions.
        self._response_cache_lock = threading.Lock()
        self._response_cache: dict[str, dict] = {}
        # Responses API previous_response_id chaining: stores the full
        # translated messages list (system/user/assistant turns) behind a
        # response_id so the next turn in the chain can prepend prior
        # history. Deliberately a SEPARATE store from _response_cache —
        # that cache is returned verbatim to the client on GET
        # /v1/responses/{id}, so internal message history must never be
        # merged into it or it would leak into a client-facing response.
        self._response_messages_lock = threading.Lock()
        self._response_messages: dict[str, list] = {}

    def _start_cleanup_timer(self):
        """
        BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
        Starts the background timer daemon for TTL cleanup.
        """
        self._cleanup_timer = threading.Timer(config.SESSION_CLEANUP_INTERVAL, self.cleanup_expired)
        self._cleanup_timer.daemon = True
        self._cleanup_timer.start()

    def get_or_create(self, session_id: str) -> SessionEntry:
        """
        BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
        Returns existing entry or creates new one.
        
        Parameters: session_id (str): The unique session identifier.
        Returns: SessionEntry
        Side effects: Logs 🆕 on creation. Updates last_active timestamp.
        """
        with self._store_lock:
            if session_id not in self._sessions:
                print(f"[{time.strftime('%H:%M:%S')}] 🆕 Session created: {session_id}")
                self._sessions[session_id] = SessionEntry()
            
            entry = self._sessions[session_id]
            
        with entry.lock:
            entry.last_active = time.time()
            
        return entry

    def store_tool_call_meta(self, session_id: str, call_id: str, meta: ToolCallMeta) -> None:
        """
        BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
        Stores tool metadata under call_id.
        
        Parameters: session_id (str), call_id (str), meta (ToolCallMeta)
        Returns: None
        Side effects: Updates the session entry state and last_active timestamp.
        """
        entry = self.get_or_create(session_id)
        with entry.lock:
            entry.tool_call_meta[call_id] = meta
            entry.last_active = time.time()

    def get_tool_call_meta(self, session_id: str, call_id: str) -> ToolCallMeta | None:
        """
        BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
        Returns stored metadata for call_id, or None if not found.
        
        Parameters: session_id (str), call_id (str)
        Returns: ToolCallMeta | None
        """
        entry = self.get_or_create(session_id)
        with entry.lock:
            entry.last_active = time.time()
            return entry.tool_call_meta.get(call_id)

    def store_server_side_parts(self, session_id: str, parts: list) -> None:
        """
        BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
        Replaces server_side_tool_parts for this session.
        
        Parameters: session_id (str), parts (list)
        Returns: None
        """
        entry = self.get_or_create(session_id)
        with entry.lock:
            entry.server_side_tool_parts = parts
            entry.last_active = time.time()

    def get_server_side_parts(self, session_id: str) -> list:
        """
        BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
        Returns a copy of stored built-in tool parts, or [].
        
        Parameters: session_id (str)
        Returns: list
        """
        entry = self.get_or_create(session_id)
        with entry.lock:
            entry.last_active = time.time()
            return entry.server_side_tool_parts.copy()

    def get_current_step(self, session_id: str) -> int:
        """
        BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
        Returns the current_turn_step under lock. Enforces R10 by preventing direct field access from callers.

        Parameters: session_id (str)
        Returns: int (current step value)
        """
        entry = self.get_or_create(session_id)
        with entry.lock:
            entry.last_active = time.time()
            return entry.current_turn_step

    def get_and_increment_stream_offset(self, session_id: str, delta: int) -> int:
        """
        BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
        Atomically reads stream_part_offset, increments it by delta, and returns the value before increment.
        Enforces R10 by making the read-modify-write a single locked operation, preventing races between
        concurrent threads processing chunks for the same session.

        Parameters: session_id (str), delta (int): number of parts in the current chunk
        Returns: int (stream_part_offset value before increment)
        """
        entry = self.get_or_create(session_id)
        with entry.lock:
            offset = entry.stream_part_offset
            entry.stream_part_offset += delta
            entry.last_active = time.time()
            return offset

    def increment_step(self, session_id: str) -> int:
        """
        BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
        Increments and returns current_turn_step. Called after each functionCall step.
        
        Parameters: session_id (str)
        Returns: int (the updated step count)
        """
        entry = self.get_or_create(session_id)
        with entry.lock:
            entry.current_turn_step += 1
            entry.last_active = time.time()
            return entry.current_turn_step

    def clear_turn(self, session_id: str) -> None:
        """
        BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
        Resets current_turn_step and stream_part_offset to 0, and clears server_side_tool_parts.
        Called when a new user text message is detected (start of new turn).
        Does NOT clear tool_call_meta — signatures from earlier turns kept for
        history reconstruction even though Gemini only validates current turn.
        
        Parameters: session_id (str)
        Returns: None
        Side effects: Modifies session state, logs 🔄 if cleared.
        """
        entry = self.get_or_create(session_id)
        with entry.lock:
            if entry.current_turn_step > 0 or entry.server_side_tool_parts or entry.stream_part_offset > 0:
                print(f"[{time.strftime('%H:%M:%S')}] 🔄 New turn detected — session turn state cleared")
                entry.current_turn_step = 0
                entry.stream_part_offset = 0
                entry.server_side_tool_parts = []
            entry.last_active = time.time()

    def cleanup_expired(self, ttl_seconds: int = None) -> None:
        """
        BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
        Removes sessions inactive beyond ttl_seconds.
        Avoids lock-inversion deadlock by sampling outside the global lock.
        
        Parameters: ttl_seconds (int): Optional override for config.SESSION_TTL_SECS.
        Returns: None
        Side effects: Deletes keys from internal dictionary, schedules next timer execution.
        """
        if ttl_seconds is None:
            ttl_seconds = config.SESSION_TTL_SECS
            
        now = time.time()
        
        with self._store_lock:
            candidates = list(self._sessions.items())
            
        expired_keys = []
        for session_id, entry in candidates:
            with entry.lock:
                if now - entry.last_active > ttl_seconds:
                    expired_keys.append(session_id)
                    
        with self._store_lock:
            for session_id in expired_keys:
                if session_id in self._sessions:
                    del self._sessions[session_id]
                
        self._start_cleanup_timer()


    def store_response_cache(self, response_id: str, response_obj: dict) -> None:
        """
        BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
        Stores a completed Responses API response object for later retrieval
        via GET /v1/responses/{id}.

        Parameters: response_id (str): Synthesized response ID (resp_{uuid}).
                    response_obj (dict): The Responses API shaped response object.
        Returns: None
        Side effects: Writes to self._response_cache under self._response_cache_lock.

        SPEC-CODE SYNC: proxy_v2_spec.md v3.0 section 4.10 Option A (TODO-03)
        """
        with self._response_cache_lock:
            self._response_cache[response_id] = response_obj

    def get_response_cache(self, response_id: str) -> dict | None:
        """
        BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
        Retrieves a cached Responses API response object.

        Parameters: response_id (str): The response ID to look up.
        Returns: dict | None — the cached response object, or None if not found.

        SPEC-CODE SYNC: proxy_v2_spec.md v3.0 section 4.10 Option A (TODO-03)
        """
        with self._response_cache_lock:
            return self._response_cache.get(response_id)

    def delete_response_cache(self, response_id: str) -> bool:
        """
        BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
        Removes a cached Responses API response object.

        Parameters: response_id (str): The response ID to delete.
        Returns: bool — True if the entry existed and was removed, False otherwise.

        SPEC-CODE SYNC: proxy_v2_spec.md v3.0 section 4.10 Option A (TODO-04)
        """
        with self._response_cache_lock:
            if response_id in self._response_cache:
                del self._response_cache[response_id]
                return True
            return False

    def store_response_messages(self, response_id: str, messages: list) -> None:
        """
        BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
        Stores the full translated messages list (all turns) behind a
        response_id, for previous_response_id chaining on a future
        request. Deliberately separate from store_response_cache — see
        __init__ note. Lost on proxy restart, same as _response_cache.

        Parameters: response_id (str): The response ID this turn produced.
                    messages (list): Full OpenAI-shaped messages list
                                     (system/user/assistant turns) up to
                                     and including this turn's assistant reply.
        Returns: None
        Side effects: Writes to self._response_messages under its own lock.
        """
        with self._response_messages_lock:
            self._response_messages[response_id] = messages

    def get_response_messages(self, response_id: str) -> list | None:
        """
        BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
        Retrieves the messages list stored behind a response_id, for
        previous_response_id chaining.

        Parameters: response_id (str): The prior response ID being chained from.
        Returns: list | None — the stored messages list, or None if not found
                 (e.g. expired via proxy restart — same limitation as _response_cache).
        """
        with self._response_messages_lock:
            return self._response_messages.get(response_id)

