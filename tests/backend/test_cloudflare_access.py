from __future__ import annotations

import time
from unittest.mock import Mock

import pytest
from flask import Flask, jsonify, session
from google.auth import crypt, jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

from src.backend.security import cloudflare_access as access
from src.backend.server.routes import auth_routes

HOST = "https://von.example.test"
TEAM = "team.cloudflareaccess.com"


@pytest.fixture
def signing_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return crypt.RSASigner.from_string(pem, key_id="key1"), public


def token(signer, **overrides):
    now = int(time.time())
    claims = dict(iss=f"https://{TEAM}", aud=["aud1"], type="app", sub="subject1", email="user@example.test", iat=now, exp=now + 3600)
    claims.update(overrides)
    return jwt.encode(signer, claims).decode()


@pytest.fixture
def app(monkeypatch, signing_key):
    from src.backend.services import login_organisation_preference_service as preferences
    monkeypatch.setattr(preferences, "read_preferences", lambda actor: {})
    monkeypatch.setenv("VON_CLOUDFLARE_ACCESS_ENABLED", "true")
    monkeypatch.setenv("VON_CLOUDFLARE_ACCESS_HOSTNAME", "von.example.test")
    monkeypatch.setenv("VON_CLOUDFLARE_ACCESS_TEAM_DOMAIN", TEAM)
    monkeypatch.setenv("VON_CLOUDFLARE_ACCESS_AUDIENCE", "aud1")
    app = Flask(__name__)
    app.secret_key = "test-only-secret-not-for-deployment"
    app.testing = True
    access.install_cloudflare_access(app)
    app.extensions["cloudflare_access_verifier"]._keys = {"key1": signing_key[1]}
    app.extensions["cloudflare_access_verifier"]._fetched_at = time.monotonic()
    monkeypatch.setattr(access, "find_user_concept_by_login_email", lambda email: {"concept_id": "#V#user", "name": "User"})
    app.register_blueprint(auth_routes.auth_bp, url_prefix="/von")

    @app.get("/private")
    def private():
        if not session.get("user_concept_id"):
            return jsonify(error="no actor"), 401
        return jsonify(actor=session.get("user_concept_id"), org=session.get("organisation_concept_id"), provider=session.get("auth_provider"))

    return app


def test_normal_issuance_binding_and_scope_retention(app, signing_key):
    client = app.test_client()
    headers = {"Cf-Access-Jwt-Assertion": token(signing_key[0])}
    first = client.get("/von/api/auth/status", base_url=HOST, headers=headers)
    assert first.json["authenticated"] is True
    assert first.json["user_concept_id"] == "#V#user"
    assert first.json["auth_provider"] == "cloudflare_access"
    with client.session_transaction(base_url=HOST) as s:
        s["organisation_concept_id"] = "#V#permitted_org"
    assert client.get("/private", base_url=HOST, headers=headers).json["org"] == "#V#permitted_org"
    assert client.get("/von/api/auth/google/login", base_url=HOST, headers=headers).location == "/von/"


def test_same_person_other_email_gets_new_login_context(app, signing_key, monkeypatch):
    from src.backend.services import login_organisation_preference_service as preferences
    monkeypatch.setattr(preferences, "read_preferences", lambda actor: {
        "user@example.test": "#V#primary", "home@example.test": "#V#household"})
    monkeypatch.setattr(preferences, "resolve_user_organisation_membership",
                        lambda actor, org: {"role": "member"})
    client = app.test_client()
    first = client.get("/von/api/auth/status", base_url=HOST,
        headers={"Cf-Access-Jwt-Assertion": token(signing_key[0])}).json
    with client.session_transaction(base_url=HOST) as s:
        s["session_id"] = "work-chat"
    second = client.get("/von/api/auth/status", base_url=HOST,
        headers={"Cf-Access-Jwt-Assertion": token(signing_key[0], email="home@example.test")}).json
    assert first["user_concept_id"] == second["user_concept_id"]
    assert first["login_context"]["organisation_concept_id"] == "#V#primary"
    assert second["login_context"]["organisation_concept_id"] == "#V#household"
    assert first["login_context"]["generation"] != second["login_context"]["generation"]
    with client.session_transaction(base_url=HOST) as s:
        assert "session_id" not in s


@pytest.mark.parametrize("overrides", [
    {"iss": "https://evil.cloudflareaccess.com"}, {"aud": ["other"]},
    {"aud": None}, {"aud": "aud1"}, {"exp": int(time.time()) - 1},
    {"iat": int(time.time()) + 200}, {"nbf": int(time.time()) + 200},
    {"email": None}, {"email": "invalid"}, {"sub": ""}, {"type": "service"},
    {"exp": "tomorrow"},
])
def test_invalid_claims_fail_closed(app, signing_key, overrides):
    response = app.test_client().get("/private", base_url=HOST, headers={"Cf-Access-Jwt-Assertion": token(signing_key[0], **overrides)})
    assert response.status_code == 403
    assert "user@example.test" not in response.get_data(as_text=True)


@pytest.mark.parametrize("raw", ["", "garbage", "a.b.c"])
def test_missing_and_forged_headers_cannot_reuse_session(app, signing_key, raw):
    client = app.test_client()
    assert client.get("/private", base_url=HOST, headers={"Cf-Access-Jwt-Assertion": token(signing_key[0])}).status_code == 200
    denied = client.get("/private", base_url=HOST, headers={"Cf-Access-Jwt-Assertion": raw, "Cf-Access-Authenticated-User-Email": "user@example.test", "X-User-Concept-ID": "#V#user"})
    assert denied.status_code == 403
    with client.session_transaction(base_url=HOST) as s:
        assert not s.get("user_concept_id")


def test_wrong_signing_key(app):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = other.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    signer = crypt.RSASigner.from_string(pem, key_id="key1")
    assert app.test_client().get("/private", base_url=HOST, headers={"Cf-Access-Jwt-Assertion": token(signer)}).status_code == 403


@pytest.mark.parametrize("ambiguous", [False, True])
def test_unbound_or_ambiguous_identity_never_inherits_actor(app, signing_key, monkeypatch, ambiguous):
    def resolve(email):
        if ambiguous:
            raise auth_routes.MultipleUsersForEmailError(email, ["#V#a", "#V#b"])
        return None
    monkeypatch.setattr(access, "find_user_concept_by_login_email", resolve)
    client = app.test_client()
    with client.session_transaction(base_url=HOST) as s:
        s.update(user_concept_id="#V#previous", organisation_concept_id="#V#private_org")
    assert client.get("/private", base_url=HOST, headers={"Cf-Access-Jwt-Assertion": token(signing_key[0])}).status_code == 403


def test_identity_change_clears_org_and_chat_scope(app, signing_key, monkeypatch):
    client = app.test_client()
    headers = {"Cf-Access-Jwt-Assertion": token(signing_key[0])}
    client.get("/private", base_url=HOST, headers=headers)
    with client.session_transaction(base_url=HOST) as s:
        s.update(organisation_concept_id="#V#old_org", namespace="old", session_id="old-chat")
    monkeypatch.setattr(access, "find_user_concept_by_login_email", lambda email: {"concept_id": "#V#other"})
    response = client.get("/private", base_url=HOST, headers={"Cf-Access-Jwt-Assertion": token(signing_key[0], email="other@example.test", sub="other")})
    assert response.json == {"actor": "#V#other", "org": None, "provider": "cloudflare_access"}
    with client.session_transaction(base_url=HOST) as s:
        assert not s.get("session_id") and not s.get("namespace")


def test_sso_cookie_cannot_authenticate_direct_host(app, signing_key):
    client = app.test_client()
    client.get("/private", base_url=HOST, headers={"Cf-Access-Jwt-Assertion": token(signing_key[0])})
    cookie = client.get_cookie("session", domain="von.example.test")
    direct = app.test_client()
    direct.set_cookie("session", cookie.value, domain="localhost")
    assert direct.get("/private").status_code == 401


def test_sso_cookie_requires_feature_and_assertion_after_disable(app, signing_key, monkeypatch):
    client = app.test_client()
    client.get("/private", base_url=HOST, headers={"Cf-Access-Jwt-Assertion": token(signing_key[0])})
    cookie = client.get_cookie("session", domain="von.example.test")
    monkeypatch.setenv("VON_CLOUDFLARE_ACCESS_ENABLED", "false")
    disabled = Flask("disabled")
    disabled.secret_key = app.secret_key
    access.install_cloudflare_access(disabled)
    disabled.register_blueprint(auth_routes.auth_bp, url_prefix="/von")
    other = disabled.test_client()
    other.set_cookie("session", cookie.value, domain="von.example.test")
    assert other.get("/von/api/auth/status", base_url=HOST).json["authenticated"] is False


def test_public_auth_routes_cannot_replace_verified_identity(app, signing_key):
    headers = {"Cf-Access-Jwt-Assertion": token(signing_key[0])}
    client = app.test_client()
    assert client.post("/von/api/auth/exchange-token", base_url=HOST, headers=headers, json={"token": "other"}).status_code == 403
    assert client.post("/von/api/auth/browser-test-login", base_url=HOST, headers=headers).status_code == 403


def test_logout_redirects_to_access_logout(app, signing_key, monkeypatch):
    from src.backend.services import window_session_context_service
    monkeypatch.setattr(window_session_context_service, "delete_window_context_if_owned", lambda *args: None)
    response = app.test_client().post("/von/api/auth/logout", base_url=HOST, headers={"Cf-Access-Jwt-Assertion": token(signing_key[0])})
    assert response.json["redirect_url"] == "/cdn-cgi/access/logout"


def test_key_rotation_and_fixed_fetch_destination(signing_key, monkeypatch):
    response = Mock(status_code=200)
    response.json.return_value = {"public_certs": [{"kid": "key1", "cert": signing_key[1]}]}
    fetch = Mock(return_value=response)
    monkeypatch.setattr(access.requests, "get", fetch)
    verifier = access.AccessVerifier(TEAM, "aud1")
    assert verifier.verify(token(signing_key[0]))["email"] == "user@example.test"
    verifier.verify(token(signing_key[0]))
    assert fetch.call_count == 1
    assert fetch.call_args.args[0] == f"https://{TEAM}/cdn-cgi/access/certs"
    assert fetch.call_args.kwargs["allow_redirects"] is False


def test_incomplete_config_fails_startup(app, monkeypatch):
    monkeypatch.setenv("VON_CLOUDFLARE_ACCESS_TEAM_DOMAIN", "https://evil.test/path")
    with pytest.raises(RuntimeError):
        access.install_cloudflare_access(Flask("bad"))


@pytest.mark.parametrize("secret", [None, "short", "von-dev-secret-key-change-in-production"])
def test_unsafe_session_secret_fails_startup(app, secret):
    candidate = Flask("unsafe-secret")
    candidate.secret_key = secret
    with pytest.raises(RuntimeError, match="non-default Flask secret"):
        access.install_cloudflare_access(candidate)


def test_key_fetch_failure_fails_closed(app, signing_key, monkeypatch):
    verifier = app.extensions["cloudflare_access_verifier"]
    verifier._keys = {}
    monkeypatch.setattr(access.requests, "get", Mock(side_effect=access.requests.ConnectionError()))
    response = app.test_client().get("/private", base_url=HOST, headers={"Cf-Access-Jwt-Assertion": token(signing_key[0])})
    assert response.status_code == 403
