"""Server-side admin authentication for the Hirevia web interface."""

import hashlib
import hmac
import os
import secrets
import sqlite3
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError


password_hasher = PasswordHasher()
SESSION_COOKIE = "hirevia_session"
SESSION_TTL_SECONDS = 8 * 60 * 60
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_LOCKOUT_SECONDS = 15 * 60
MAX_LOGIN_ATTEMPTS = 5


class AuthConfigurationError(RuntimeError):
    """Raised when the admin auth environment is missing or invalid."""


@dataclass(frozen=True)
class AuthConfig:
    admin_email: str
    password_hash: str
    session_secret: bytes
    database_path: Path
    cookie_secure: bool
    session_ttl_seconds: int


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().casefold() not in {"0", "false", "no", "off", ""}


def _database_path() -> Path:
    return Path(os.getenv("HIREVIA_AUTH_DB_PATH", "hirevia_auth.db")).expanduser()


def load_auth_config() -> AuthConfig:
    secret_value = (os.getenv("HIREVIA_SESSION_SECRET") or "").strip()
    if len(secret_value.encode("utf-8")) < 32:
        raise AuthConfigurationError("Admin authentication is not configured")

    production = (os.getenv("HIREVIA_ENV") or "").strip().casefold() == "production"
    secure = _env_bool("HIREVIA_COOKIE_SECURE", production)
    if production and not secure:
        raise AuthConfigurationError("Secure cookies are required in production")

    try:
        ttl = int(os.getenv("HIREVIA_SESSION_TTL_SECONDS", str(SESSION_TTL_SECONDS)))
    except ValueError as error:
        raise AuthConfigurationError("Invalid session lifetime configuration") from error
    if ttl < 60 or ttl > 30 * 24 * 60 * 60:
        raise AuthConfigurationError("Invalid session lifetime configuration")

    database_path = _database_path()
    with _connection(database_path) as connection:
        admin = connection.execute(
            "SELECT email, password_hash FROM admins WHERE id = 1"
        ).fetchone()
    if admin is None:
        raise AuthConfigurationError("Admin authentication is not configured")
    return AuthConfig(
        admin_email=admin[0],
        password_hash=admin[1],
        session_secret=secret_value.encode("utf-8"),
        database_path=database_path,
        cookie_secure=secure,
        session_ttl_seconds=ttl,
    )


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return password_hasher.verify(password_hash, password)
    except (InvalidHashError, VerificationError, VerifyMismatchError):
        return False


def _connect(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path, timeout=30, isolation_level=None)
    connection.execute("PRAGMA busy_timeout = 30000")
    try:
        os.chmod(database_path, 0o600)
    except OSError:
        pass
    connection.execute(
        "CREATE TABLE IF NOT EXISTS admins ("
        "id INTEGER PRIMARY KEY CHECK (id = 1), "
        "email TEXT NOT NULL UNIQUE COLLATE NOCASE, "
        "password_hash TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS sessions ("
        "token_hash TEXT PRIMARY KEY, expires_at INTEGER NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS login_limits ("
        "limit_key TEXT PRIMARY KEY, attempts INTEGER NOT NULL, "
        "window_started INTEGER NOT NULL, locked_until INTEGER NOT NULL)"
    )
    return connection


@contextmanager
def _connection(database_path: Path):
    connection = _connect(database_path)
    try:
        yield connection
    finally:
        connection.close()


def admin_exists(database_path: Optional[Path] = None) -> bool:
    with _connection(database_path or _database_path()) as connection:
        return connection.execute("SELECT 1 FROM admins WHERE id = 1").fetchone() is not None


def create_initial_admin(
    email: str,
    *,
    password: Optional[str] = None,
    password_hash: Optional[str] = None,
    database_path: Optional[Path] = None,
) -> bool:
    """Create the single initial admin without replacing an existing record."""
    with _connection(database_path or _database_path()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT 1 FROM admins WHERE id = 1").fetchone():
            connection.commit()
            return False

        normalized_email = (email or "").strip().casefold()
        if not normalized_email or len(normalized_email) > 254 or "@" not in normalized_email:
            connection.rollback()
            raise AuthConfigurationError("A valid admin email is required")
        if password is not None:
            if not password:
                connection.rollback()
                raise AuthConfigurationError("The initial admin password cannot be empty")
            stored_hash = hash_password(password)
        elif password_hash and password_hash.startswith("$argon2id$"):
            stored_hash = password_hash
        else:
            connection.rollback()
            raise AuthConfigurationError("An Argon2id admin password hash is required")

        connection.execute(
            "INSERT INTO admins (id, email, password_hash) VALUES (1, ?, ?)",
            (normalized_email, stored_hash),
        )
        connection.commit()
        return True


def _remove_bootstrap_password(env_file: Optional[Path]) -> None:
    from dotenv import dotenv_values, unset_key

    path = env_file or Path(os.getenv("HIREVIA_ENV_FILE", Path.cwd() / ".env"))
    if not path.is_file():
        return
    original_mode = stat.S_IMODE(path.stat().st_mode)
    unset_key(str(path), "HIREVIA_ADMIN_PASSWORD")
    if "HIREVIA_ADMIN_PASSWORD" in dotenv_values(path):
        raise AuthConfigurationError(
            "Could not remove the one-time admin password from the environment file"
        )
    if os.name != "nt":
        path.chmod(original_mode or 0o600)


@contextmanager
def _environment_lock(path: Path):
    lock_path = path.with_name(f"{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
        locked = True
        yield
    finally:
        if locked:
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def ensure_session_secret(env_file: Optional[Path] = None) -> None:
    if os.getenv("HIREVIA_SESSION_SECRET"):
        return
    if not (
        os.getenv("HIREVIA_ADMIN_PASSWORD") is not None
        or os.getenv("HIREVIA_ADMIN_PASSWORD_HASH")
    ):
        return
    from dotenv import dotenv_values, set_key

    path = env_file or Path(os.getenv("HIREVIA_ENV_FILE", Path.cwd() / ".env"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    with _environment_lock(path):
        secret_value = os.getenv("HIREVIA_SESSION_SECRET") or dotenv_values(path).get(
            "HIREVIA_SESSION_SECRET"
        )
        if not secret_value:
            secret_value = secrets.token_urlsafe(48)
            set_key(str(path), "HIREVIA_SESSION_SECRET", secret_value)
        os.environ["HIREVIA_SESSION_SECRET"] = secret_value
    if os.name != "nt":
        path.chmod(0o600)


def bootstrap_initial_admin(env_file: Optional[Path] = None) -> bool:
    """Consume configured bootstrap credentials, creating an admin only once."""
    email = os.getenv("HIREVIA_ADMIN_EMAIL", "")
    password = os.environ.pop("HIREVIA_ADMIN_PASSWORD", None)
    password_hash = os.getenv("HIREVIA_ADMIN_PASSWORD_HASH")
    if password is None and not password_hash:
        return False
    try:
        return create_initial_admin(email, password=password, password_hash=password_hash)
    finally:
        if password is not None:
            _remove_bootstrap_password(env_file)


def _key(config: AuthConfig, value: str) -> str:
    return hmac.new(config.session_secret, value.encode("utf-8"), hashlib.sha256).hexdigest()


def _login_limit_key(config: AuthConfig, email: str, remote_address: str) -> str:
    return _key(config, f"{email.casefold()}\0{remote_address}")


def is_rate_limited(config: AuthConfig, email: str, remote_address: str) -> bool:
    key = _login_limit_key(config, email, remote_address)
    now = int(time.time())
    with _connection(config.database_path) as connection:
        row = connection.execute(
            "SELECT locked_until FROM login_limits WHERE limit_key = ?",
            (key,),
        ).fetchone()
    return bool(row and row[0] > now)


def record_failed_login(config: AuthConfig, email: str, remote_address: str) -> None:
    key = _login_limit_key(config, email, remote_address)
    now = int(time.time())
    with _connection(config.database_path) as connection:
        row = connection.execute(
            "SELECT attempts, window_started FROM login_limits WHERE limit_key = ?",
            (key,),
        ).fetchone()
        if row is None or now - row[1] >= LOGIN_WINDOW_SECONDS:
            attempts, started = 1, now
        else:
            attempts, started = row[0] + 1, row[1]
        locked_until = now + LOGIN_LOCKOUT_SECONDS if attempts >= MAX_LOGIN_ATTEMPTS else 0
        connection.execute(
            "INSERT OR REPLACE INTO login_limits VALUES (?, ?, ?, ?)",
            (key, attempts, started, locked_until),
        )


def clear_failed_logins(config: AuthConfig, email: str, remote_address: str) -> None:
    with _connection(config.database_path) as connection:
        connection.execute(
            "DELETE FROM login_limits WHERE limit_key = ?",
            (_login_limit_key(config, email, remote_address),),
        )


def create_session(config: AuthConfig) -> tuple[str, int]:
    token = secrets.token_urlsafe(32)
    expires_at = int(time.time()) + config.session_ttl_seconds
    token_hash = _key(config, token)
    with _connection(config.database_path) as connection:
        connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (int(time.time()),))
        connection.execute(
            "INSERT INTO sessions (token_hash, expires_at) VALUES (?, ?)",
            (token_hash, expires_at),
        )
    return token, expires_at


def session_is_valid(config: AuthConfig, token: Optional[str]) -> bool:
    if not token:
        return False
    with _connection(config.database_path) as connection:
        row = connection.execute(
            "SELECT expires_at FROM sessions WHERE token_hash = ?",
            (_key(config, token),),
        ).fetchone()
        if row and row[0] <= int(time.time()):
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (_key(config, token),))
            return False
    return bool(row)


def revoke_session(config: AuthConfig, token: Optional[str]) -> None:
    if not token:
        return
    with _connection(config.database_path) as connection:
        connection.execute("DELETE FROM sessions WHERE token_hash = ?", (_key(config, token),))