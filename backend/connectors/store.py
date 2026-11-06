"""Stub connector store (no connectors in this build)."""

_SECRET_SLOTS: set[str] = set()


def list_connectors() -> list[dict]:
    return []


def get_connector(name: str) -> dict | None:
    return None
