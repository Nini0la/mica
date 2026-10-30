from app.security import resolve_login_username


def test_production_identity_cannot_be_selected_by_client():
    assert resolve_login_username("attacker-selected", "production") == "owner"
    assert resolve_login_username("alice", "test") == "alice"
