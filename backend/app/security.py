def resolve_login_username(requested: str, environment: str) -> str:
    """Production is deliberately single-user; test/dev can exercise tenancy."""
    return "owner" if environment == "production" else requested
