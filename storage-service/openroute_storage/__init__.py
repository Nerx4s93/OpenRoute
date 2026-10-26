"""OpenRoute storage-service: the only component with PostgreSQL access.

The gateway (and every other service) talks to this private FastAPI service
over its domain HTTP API. The database schema is the frozen, idempotent DDL in
``schema.py``; no migration or schema change is introduced here.
"""

__version__ = "1.0.0"
