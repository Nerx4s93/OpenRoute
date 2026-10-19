"""OpenRoute gateway package (data plane).

OpenAI-compatible API gateway that load-balances client requests across a pool
of HTTP(S) proxies (round-robin) and forwards them to opencode.ai's free-tier
inference. See ``DESIGN.md`` for the frozen interface contract.
"""

__version__ = "0.1.0"
