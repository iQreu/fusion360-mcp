"""Entity token registry.

Fusion API objects cannot cross the socket, so we hand the MCP client opaque
string tokens (e.g. "edg7", "fac3", "prf1") and resolve them back to live
objects on subsequent calls. Tokens are deduplicated by Fusion's persistent
`entityToken` where available, so the same edge always maps to the same token.

Token strings are NEVER reused: the per-kind counters are monotonic for the
life of the process and survive reset(). Reusing a string would let a stale
client token silently resolve to a different entity (wrong-body edits); instead
a stale token raises the helpful KeyError below so the client re-queries.
"""


class Registry:
    def __init__(self):
        self._by_token = {}      # token -> live API object
        self._token_by_et = {}   # entityToken -> token
        self._et_by_token = {}   # token -> entityToken (reverse, for remove)
        self._counters = {}      # kind -> int (monotonic; never reset)

    def reset(self):
        # Drop the resolution maps so old tokens go dead, but keep the counters
        # so freshly minted tokens never collide with strings the client may
        # still be holding.
        self._by_token.clear()
        self._token_by_et.clear()
        self._et_by_token.clear()

    @staticmethod
    def _entity_token(obj):
        try:
            return obj.entityToken
        except Exception:
            return None

    def add(self, kind, obj):
        et = self._entity_token(obj)
        if et is not None and et in self._token_by_et:
            token = self._token_by_et[et]
            self._by_token[token] = obj  # refresh stale reference
            return token
        self._counters[kind] = self._counters.get(kind, 0) + 1
        token = '%s%d' % (kind, self._counters[kind])
        self._by_token[token] = obj
        if et is not None:
            self._token_by_et[et] = token
            self._et_by_token[token] = et
        return token

    def get(self, token):
        if token is None:
            raise KeyError('Expected an entity token, got null')
        if token not in self._by_token:
            raise KeyError('Unknown entity token: %r (call get_state/query_entities '
                           'to obtain fresh tokens)' % token)
        return self._by_token[token]

    def get_opt(self, token):
        return self._by_token.get(token)

    def remove(self, token):
        """Forget a token (its entity was deleted). A later get() then raises
        the helpful KeyError instead of returning a dead API wrapper."""
        self._by_token.pop(token, None)
        et = self._et_by_token.pop(token, None)
        if et is not None:
            self._token_by_et.pop(et, None)
