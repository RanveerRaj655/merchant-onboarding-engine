# Lazy exports — avoid importing engine at package import time during Alembic metadata load
__all__ = ["SessionLocal", "engine", "get_db"]


def __getattr__(name: str):
    if name in __all__:
        from apps.api.db import session as _session

        return getattr(_session, name)
    raise AttributeError(name)
