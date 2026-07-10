"""Run the singleton process-owning miner supervisor."""

from __future__ import annotations

import signal

from django.core.management.base import BaseCommand, CommandError

from controller.miner_supervisor import (
    MinerSupervisor,
    SupervisorAlreadyRunning,
    SupervisorLeaseLost,
    SupervisorOptions,
)


class Command(BaseCommand):
    help = "Process miner commands, reconcile desired state, and recover crashed miners."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--once",
            action="store_true",
            help="Run one forced reconciliation cycle and exit cleanly.",
        )
        parser.add_argument(
            "--interval",
            type=float,
            default=None,
            help="Override the command polling interval in seconds.",
        )

    def handle(self, *args, **options) -> None:
        if options["interval"] is not None and options["interval"] < 0:
            raise CommandError("--interval cannot be negative.")

        runtime_options = SupervisorOptions.from_settings(
            command_poll_seconds=options["interval"]
        )
        supervisor = MinerSupervisor(options=runtime_options)
        stop_requested = False

        def request_stop(signum, frame) -> None:  # noqa: ARG001 - signal API
            nonlocal stop_requested
            stop_requested = True

        previous_term = signal.signal(signal.SIGTERM, request_stop)
        try:
            supervisor.startup()
            if options["once"]:
                supervisor.run_once(force_checks=True)
            else:
                supervisor.run_forever(should_stop=lambda: stop_requested)
        except SupervisorAlreadyRunning as exc:
            raise CommandError(str(exc)) from exc
        except SupervisorLeaseLost as exc:
            raise CommandError(str(exc)) from exc
        except KeyboardInterrupt:
            self.stdout.write("Stopping miner supervisor...")
        finally:
            supervisor.shutdown()
            signal.signal(signal.SIGTERM, previous_term)
