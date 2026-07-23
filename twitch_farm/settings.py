"""Django settings for the Twitch Farm controller."""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured


BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "development-only-change-me")
DEBUG = env_bool("DJANGO_DEBUG", True)
INSECURE_SECRET_KEYS = {
    "development-only-change-me",
    "replace-with-a-long-random-secret",
    "container-build-only",
}
if not DEBUG and (SECRET_KEY in INSECURE_SECRET_KEYS or len(SECRET_KEY) < 50):
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY must be an explicit random value of at least 50 characters "
        "when DJANGO_DEBUG is false."
    )


def credential_keys() -> tuple[str, ...]:
    raw = os.getenv("TWITCH_FARM_CREDENTIAL_KEYS", "")
    values = tuple(value.strip() for value in raw.split(",") if value.strip())
    if not values and DEBUG:
        values = (
            base64.urlsafe_b64encode(
                hashlib.sha256(f"twitch-farm:{SECRET_KEY}".encode()).digest()
            ).decode("ascii"),
        )
    if not values:
        raise ImproperlyConfigured(
            "TWITCH_FARM_CREDENTIAL_KEYS must contain at least one Fernet key."
        )
    for value in values:
        try:
            decoded = base64.b64decode(
                value.encode("ascii"),
                altchars=b"-_",
                validate=True,
            )
        except Exception as exc:
            raise ImproperlyConfigured(
                "TWITCH_FARM_CREDENTIAL_KEYS contains an invalid Fernet key."
            ) from exc
        if len(decoded) != 32:
            raise ImproperlyConfigured(
                "TWITCH_FARM_CREDENTIAL_KEYS contains an invalid Fernet key."
            )
    return values


TWITCH_FARM_CREDENTIAL_KEYS = credential_keys()
ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]").split(",")
    if host.strip()
]
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "controller.apps.ControllerConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
]
if not DEBUG:
    MIDDLEWARE.append("whitenoise.middleware.WhiteNoiseMiddleware")
MIDDLEWARE += [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "twitch_farm.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]

WSGI_APPLICATION = "twitch_farm.wsgi.application"
ASGI_APPLICATION = "twitch_farm.asgi.application"

DATABASE_PATH = Path(os.getenv("TWITCH_FARM_DB", BASE_DIR / "data" / "db.sqlite3"))
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": DATABASE_PATH,
        "OPTIONS": {
            "timeout": int(os.getenv("SQLITE_BUSY_TIMEOUT_SECONDS", "30")),
            # Acquire SQLite's single writer slot before an atomic block reads
            # state. Without BEGIN IMMEDIATE, two web workers can both read a
            # stale desired state and the later read-to-write upgrade fails
            # immediately with SQLITE_BUSY instead of honoring busy_timeout.
            "transaction_mode": "IMMEDIATE",
        },
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
CSRF_FAILURE_VIEW = "controller.api.csrf_failure"
DATA_UPLOAD_MAX_MEMORY_SIZE = 11 * 1024 * 1024
# Every accepted legacy ZIP stays in memory.  The importer caps the archive at
# 10 MiB and never needs Django's temporary-file upload handler.
FILE_UPLOAD_MAX_MEMORY_SIZE = 11 * 1024 * 1024
DATA_UPLOAD_MAX_NUMBER_FILES = 1
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        )
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "controller:login"
LOGIN_REDIRECT_URL = "controller:dashboard"
LOGOUT_REDIRECT_URL = "controller:login"

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = env_bool("DJANGO_SECURE_COOKIES", not DEBUG)
CSRF_COOKIE_SECURE = env_bool("DJANGO_SECURE_COOKIES", not DEBUG)
SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", not DEBUG)
SECURE_HSTS_SECONDS = int(
    os.getenv("DJANGO_SECURE_HSTS_SECONDS", "31536000" if not DEBUG else "0")
)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool(
    "DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS", not DEBUG
)
SECURE_HSTS_PRELOAD = env_bool("DJANGO_SECURE_HSTS_PRELOAD", not DEBUG)
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

TWITCH_FARM_RUNTIME_DIR = Path(
    os.getenv("TWITCH_FARM_RUNTIME_DIR", BASE_DIR / "runtime")
)
TWITCH_FARM_WORKER_LOCK = Path(
    os.getenv("TWITCH_FARM_WORKER_LOCK", DATABASE_PATH.parent / "miner-worker.lock")
)
TWITCH_FARM_LOG_FILE = Path(
    os.getenv("TWITCH_FARM_LOG_FILE", DATABASE_PATH.parent / "logs" / "twitch-farm.log")
)
TWITCH_FARM_LOG_WRITER = env_bool("TWITCH_FARM_LOG_WRITER", False)
MINER_AUTHENTICATION_HANDSHAKE_SECONDS = float(
    os.getenv("MINER_AUTHENTICATION_HANDSHAKE_SECONDS", "1900")
)

if TWITCH_FARM_LOG_WRITER:
    TWITCH_FARM_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "standard"},
        "runtime_file": {
            "class": "logging.handlers.RotatingFileHandler",
            "formatter": "standard",
            "filename": str(TWITCH_FARM_LOG_FILE),
            "maxBytes": 5 * 1024 * 1024,
            "backupCount": 3,
            "encoding": "utf-8",
            "delay": True,
        },
    },
    "root": {
        "handlers": ["console", "runtime_file"] if TWITCH_FARM_LOG_WRITER else ["console"],
        "level": os.getenv("LOG_LEVEL", "INFO"),
    },
}
