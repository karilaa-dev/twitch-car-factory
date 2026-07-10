"""Controller application configuration."""

from django.apps import AppConfig
from django.db.backends.signals import connection_created


def configure_sqlite_connection(sender, connection, **kwargs) -> None:
    if connection.vendor != "sqlite":
        return
    timeout_seconds = connection.settings_dict.get("OPTIONS", {}).get("timeout", 5)
    busy_timeout_ms = max(0, int(float(timeout_seconds) * 1000))
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA journal_mode=WAL")
        # Desired state and incident history must survive a host power loss once
        # Django reports the transaction committed. WAL+FULL trades a small
        # amount of write throughput for that durability guarantee.
        cursor.execute("PRAGMA synchronous=FULL")
        cursor.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")


class ControllerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "controller"

    def ready(self) -> None:
        connection_created.connect(
            configure_sqlite_connection,
            dispatch_uid="controller.configure_sqlite_connection",
        )
