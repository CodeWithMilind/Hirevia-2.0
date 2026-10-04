import sqlite3
import os
from concurrent.futures import ThreadPoolExecutor

from click.testing import CliRunner
from dotenv import dotenv_values, set_key
import pytest
from fastapi.testclient import TestClient

from hirevia.core.models import Job
from hirevia.cli import cli
from hirevia.config import Config
from hirevia.storage.state import JobRepository
from hirevia.web import state as web_state
from hirevia.web.auth import (
    create_initial_admin,
    ensure_session_secret,
    hash_password,
    verify_password,
)
from hirevia.web.main import app


@pytest.fixture
def auth_client(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HIREVIA_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("HIREVIA_ADMIN_PASSWORD_HASH", hash_password("test-password-strong"))
    monkeypatch.setenv("HIREVIA_SESSION_SECRET", "test-session-secret-with-more-than-32-bytes")
    monkeypatch.setenv("HIREVIA_AUTH_DB_PATH", str(tmp_path / "auth.db"))
    monkeypatch.setenv("HIREVIA_COOKIE_SECURE", "false")
    monkeypatch.delenv("HIREVIA_ENV", raising=False)
    web_state._web_state = None
    web_state.WebUIState._instance = None
    try:
        with TestClient(app, follow_redirects=False) as client:
            yield client
    finally:
        web_state._web_state = None
        web_state.WebUIState._instance = None


def login(client, password="test-password-strong", email="admin@example.com"):
    return client.post("/auth/login", json={"email": email, "password": password})


def test_login_protects_dashboard_and_api_and_sets_http_only_cookie(auth_client):
    client = auth_client
    assert client.get("/").status_code == 303
    assert client.get("/api/config").status_code == 401
    assert client.get("/static/index.html").status_code == 404

    rejected = login(client, "incorrect-password")
    assert rejected.status_code == 401
    assert rejected.json() == {"detail": "Invalid email or password"}

    accepted = login(client)
    assert accepted.status_code == 200
    assert accepted.json() == {"authenticated": True, "expires_at": accepted.json()["expires_at"]}
    cookie = accepted.headers["set-cookie"].casefold()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "test-password-strong" not in accepted.text
    with sqlite3.connect(os.environ["HIREVIA_AUTH_DB_PATH"]) as connection:
        password_hash = connection.execute(
            "SELECT password_hash FROM admins WHERE id = 1"
        ).fetchone()[0]
    assert password_hash.startswith("$argon2id$")
    assert client.get("/").status_code == 200
    assert client.get("/api/config").status_code == 200


def test_production_auth_requires_secure_session_cookie(auth_client, monkeypatch):
    monkeypatch.setenv("HIREVIA_ENV", "production")
    monkeypatch.setenv("HIREVIA_COOKIE_SECURE", "false")
    assert login(auth_client).status_code == 503

    monkeypatch.setenv("HIREVIA_COOKIE_SECURE", "true")
    response = login(auth_client)
    assert response.status_code == 200
    assert "secure" in response.headers["set-cookie"].casefold()


def test_authenticated_startup_preserves_existing_jobs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HIREVIA_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("HIREVIA_ADMIN_PASSWORD_HASH", hash_password("test-password-strong"))
    monkeypatch.setenv("HIREVIA_SESSION_SECRET", "test-session-secret-with-more-than-32-bytes")
    monkeypatch.setenv("HIREVIA_AUTH_DB_PATH", str(tmp_path / "auth.db"))
    monkeypatch.setenv("HIREVIA_COOKIE_SECURE", "false")
    monkeypatch.setenv("HIREVIA_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.delenv("HIREVIA_ENV", raising=False)
    web_state._web_state = None
    web_state.WebUIState._instance = None

    repo = JobRepository(tmp_path / "jobs.db")
    job = Job(
        source="test",
        source_job_id="preserved-job",
        title="Existing role",
        company="Example",
        location="Remote",
        description="Existing record",
        url="https://example.com/jobs/preserved-job",
    )
    repo.upsert_job(job)
    web_state.initialize_web_state(Config(), repo)

    try:
        with TestClient(app, follow_redirects=False) as client:
            assert login(client).status_code == 200
            assert client.get("/api/jobs").status_code == 200
        stored_job = repo.get_job(job.id)
        assert stored_job is not None
        assert stored_job.title == "Existing role"
        assert (tmp_path / "auth.db") != (tmp_path / "jobs.db")
    finally:
        web_state._web_state = None
        web_state.WebUIState._instance = None


def test_logout_revokes_session_and_expired_sessions_are_rejected(auth_client, monkeypatch):
    client = auth_client
    assert login(client).status_code == 200
    assert client.get("/api/config").status_code == 200
    assert client.post("/auth/logout").status_code == 200
    assert client.get("/api/config").status_code == 401

    assert login(client).status_code == 200
    with sqlite3.connect(os.environ["HIREVIA_AUTH_DB_PATH"]) as connection:
        connection.execute("UPDATE sessions SET expires_at = 0")
    assert client.get("/api/config").status_code == 401


def test_login_lockout_and_auth_database_never_contains_plaintext(auth_client, monkeypatch):
    client = auth_client
    for _ in range(5):
        assert login(client, "wrong-password").status_code == 401
    assert login(client).status_code == 429

    database_path = os.environ["HIREVIA_AUTH_DB_PATH"]
    with sqlite3.connect(database_path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        columns = {
            table: {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            for table in tables
        }
    assert "password" not in {column for table_columns in columns.values() for column in table_columns}
    assert b"test-password-strong" not in open(database_path, "rb").read()


def test_unknown_email_and_wrong_password_have_same_error(auth_client):
    wrong_password = login(auth_client, "wrong-password")
    unknown_email = login(auth_client, "test-password-strong", "unknown@example.com")
    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json()


def test_admin_setup_accepts_short_password_without_storing_plaintext(tmp_path):
    env_file = tmp_path / ".env"
    database_path = tmp_path / "auth.db"
    result = CliRunner().invoke(
        cli,
        ["auth", "create-admin", "--env-file", str(env_file)],
        input="admin@example.com\nx\nx\n",
        env={"HIREVIA_AUTH_DB_PATH": str(database_path)},
    )

    assert result.exit_code == 0, result.output
    settings = dotenv_values(env_file)
    assert settings["HIREVIA_ADMIN_EMAIL"] == "admin@example.com"
    assert "HIREVIA_ADMIN_PASSWORD_HASH" not in settings
    assert settings["HIREVIA_SESSION_SECRET"]
    assert "HIREVIA_ADMIN_PASSWORD=" not in env_file.read_text(encoding="utf-8")
    with sqlite3.connect(database_path) as connection:
        row = connection.execute("SELECT email, password_hash FROM admins").fetchone()
    assert row[0] == "admin@example.com"
    assert verify_password(row[1], "x")

    duplicate = CliRunner().invoke(
        cli,
        ["auth", "create-admin", "--env-file", str(env_file)],
        input="other@example.com\nnew-password\nnew-password\n",
        env={"HIREVIA_AUTH_DB_PATH": str(database_path)},
    )
    assert duplicate.exit_code != 0
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT email FROM admins").fetchone()[0] == "admin@example.com"


def test_startup_bootstraps_admin_once_and_consumes_plaintext_env(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    database_path = tmp_path / "auth.db"
    set_key(str(env_file), "HIREVIA_ADMIN_EMAIL", "admin@example.com")
    set_key(str(env_file), "HIREVIA_ADMIN_PASSWORD", "bootstrap-password")
    set_key(str(env_file), "HIREVIA_SESSION_SECRET", "persisted-session-secret-with-more-than-32-bytes")
    set_key(str(env_file), "HIREVIA_AUTH_DB_PATH", str(database_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HIREVIA_ENV_FILE", str(env_file))
    monkeypatch.setenv("HIREVIA_COOKIE_SECURE", "false")
    for name in (
        "HIREVIA_ADMIN_EMAIL",
        "HIREVIA_ADMIN_PASSWORD",
        "HIREVIA_ADMIN_PASSWORD_HASH",
        "HIREVIA_SESSION_SECRET",
        "HIREVIA_AUTH_DB_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    web_state._web_state = None
    web_state.WebUIState._instance = None

    try:
        with TestClient(app, follow_redirects=False) as client:
            first_login = login(client, "bootstrap-password")
        assert first_login.status_code == 200
        assert "HIREVIA_ADMIN_PASSWORD" not in os.environ
        assert "HIREVIA_ADMIN_PASSWORD" not in dotenv_values(env_file)
        assert "bootstrap-password" not in env_file.read_text(encoding="utf-8")
        with sqlite3.connect(database_path) as connection:
            email, password_hash = connection.execute(
                "SELECT email, password_hash FROM admins WHERE id = 1"
            ).fetchone()
        assert email == "admin@example.com"
        assert password_hash.startswith("$argon2id$")
        assert verify_password(password_hash, "bootstrap-password")

        set_key(str(env_file), "HIREVIA_ADMIN_EMAIL", "changed@example.com")
        set_key(str(env_file), "HIREVIA_ADMIN_PASSWORD", "changed-password")
        monkeypatch.delenv("HIREVIA_ADMIN_EMAIL", raising=False)
        monkeypatch.setenv("HIREVIA_ADMIN_PASSWORD", "changed-password")
        web_state._web_state = None
        web_state.WebUIState._instance = None
        with TestClient(app, follow_redirects=False) as client:
            assert login(client, "changed-password", "changed@example.com").status_code == 401
            assert login(client, "bootstrap-password", "admin@example.com").status_code == 200
        with sqlite3.connect(database_path) as connection:
            assert connection.execute(
                "SELECT email, password_hash FROM admins WHERE id = 1"
            ).fetchone() == ("admin@example.com", password_hash)
        assert "HIREVIA_ADMIN_PASSWORD" not in dotenv_values(env_file)
    finally:
        web_state._web_state = None
        web_state.WebUIState._instance = None


def test_simultaneous_admin_initialization_creates_one_record(tmp_path):
    database_path = tmp_path / "concurrent-auth.db"
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(
            lambda password: create_initial_admin(
                "admin@example.com", password=password, database_path=database_path
            ),
            ("first-password", "second-password"),
        ))

    assert sorted(results) == [False, True]
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM admins").fetchone()[0] == 1


def test_simultaneous_session_secret_generation_is_persistent(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    monkeypatch.setenv("HIREVIA_ENV_FILE", str(env_file))
    monkeypatch.setenv("HIREVIA_ADMIN_PASSWORD", "bootstrap-password")
    monkeypatch.delenv("HIREVIA_SESSION_SECRET", raising=False)

    def ensure_and_read_secret(_):
        ensure_session_secret(env_file)
        import os

        return os.environ["HIREVIA_SESSION_SECRET"]

    with ThreadPoolExecutor(max_workers=4) as executor:
        values = list(executor.map(ensure_and_read_secret, range(4)))

    assert len(set(values)) == 1
    assert dotenv_values(env_file)["HIREVIA_SESSION_SECRET"] == values[0]
    monkeypatch.delenv("HIREVIA_SESSION_SECRET", raising=False)
    ensure_session_secret(env_file)
    import os

    assert os.environ["HIREVIA_SESSION_SECRET"] == values[0]