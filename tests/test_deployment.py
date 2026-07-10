from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase
import yaml


class ComposeHealthcheckTests(SimpleTestCase):
    def test_web_probe_connects_to_loopback_with_a_configured_allowed_host(self):
        compose = yaml.safe_load(
            (Path(settings.BASE_DIR) / "docker-compose.yml").read_text(encoding="utf-8")
        )
        command = compose["services"]["web"]["healthcheck"]["test"]
        self.assertEqual(command[:3], ["CMD", "python", "-c"])

        cases = (
            ("farm.example.com", "farm.example.com", "0", None),
            (".example.com", "example.com", "0", None),
            ("*", "localhost", "0", None),
            ("*,.example.com", "example.com", "0", None),
            ("[::1]", "[::1]", "0", None),
            ("farm.example.com", "farm.example.com", "1", "https"),
        )
        for allowed_hosts, expected_host, secure_redirect, expected_proto in cases:
            with self.subTest(
                allowed_hosts=allowed_hosts,
                secure_redirect=secure_redirect,
            ):
                requests = []

                def capture_request(request, *, timeout):
                    requests.append((request, timeout))

                with (
                    patch.dict(
                        os.environ,
                        {
                            "DJANGO_ALLOWED_HOSTS": allowed_hosts,
                            "DJANGO_SECURE_SSL_REDIRECT": secure_redirect,
                        },
                    ),
                    patch("urllib.request.urlopen", side_effect=capture_request),
                ):
                    exec(command[3], {})

                self.assertEqual(len(requests), 1)
                request, timeout = requests[0]
                self.assertEqual(request.full_url, "http://127.0.0.1:8000/healthz/")
                self.assertEqual(request.get_header("Host"), expected_host)
                self.assertEqual(request.get_header("X-forwarded-proto"), expected_proto)
                self.assertEqual(timeout, 3)


class ComposeSecurityDefaultsTests(SimpleTestCase):
    security_environment = {
        "DJANGO_SECURE_COOKIES": "${DJANGO_SECURE_COOKIES:-0}",
        "DJANGO_SECURE_SSL_REDIRECT": "${DJANGO_SECURE_SSL_REDIRECT:-0}",
        "DJANGO_SECURE_HSTS_SECONDS": "${DJANGO_SECURE_HSTS_SECONDS:-0}",
        "DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS": (
            "${DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS:-0}"
        ),
        "DJANGO_SECURE_HSTS_PRELOAD": "${DJANGO_SECURE_HSTS_PRELOAD:-0}",
    }

    def test_compose_and_example_environment_default_to_direct_http(self):
        compose = yaml.safe_load(
            (Path(settings.BASE_DIR) / "docker-compose.yml").read_text(encoding="utf-8")
        )
        web_environment = compose["services"]["web"]["environment"]
        self.assertEqual(
            {name: web_environment[name] for name in self.security_environment},
            self.security_environment,
        )

        example_values = {}
        for line in (Path(settings.BASE_DIR) / ".env.example").read_text(
            encoding="utf-8"
        ).splitlines():
            if line and not line.startswith("#") and "=" in line:
                name, value = line.split("=", 1)
                example_values[name] = value

        self.assertEqual(example_values["DJANGO_ALLOWED_HOSTS"], "localhost,127.0.0.1,[::1]")
        self.assertEqual(example_values["DJANGO_CSRF_TRUSTED_ORIGINS"], "")
        self.assertEqual(
            {name: example_values[name] for name in self.security_environment},
            dict.fromkeys(self.security_environment, "0"),
        )

    def test_non_compose_production_defaults_remain_https_strict(self):
        environment = os.environ.copy()
        for name in self.security_environment:
            environment.pop(name, None)
        environment.update(
            {
                "DJANGO_SETTINGS_MODULE": "twitch_farm.settings",
                "DJANGO_DEBUG": "0",
                "DJANGO_SECRET_KEY": (
                    "standalone-production-default-test-abcdefghijklmnopqrstuvwxyz-0123456789"
                ),
            }
        )
        script = """
import json
from django.conf import settings
print(json.dumps({
    "session_cookie_secure": settings.SESSION_COOKIE_SECURE,
    "csrf_cookie_secure": settings.CSRF_COOKIE_SECURE,
    "ssl_redirect": settings.SECURE_SSL_REDIRECT,
    "hsts_seconds": settings.SECURE_HSTS_SECONDS,
    "hsts_include_subdomains": settings.SECURE_HSTS_INCLUDE_SUBDOMAINS,
    "hsts_preload": settings.SECURE_HSTS_PRELOAD,
}))
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=settings.BASE_DIR,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "session_cookie_secure": True,
                "csrf_cookie_secure": True,
                "ssl_redirect": True,
                "hsts_seconds": 31536000,
                "hsts_include_subdomains": True,
                "hsts_preload": True,
            },
        )


class DockerWorkflowTests(SimpleTestCase):
    def workflow(self):
        return yaml.safe_load(
            (Path(settings.BASE_DIR) / ".github/workflows/docker-build.yml").read_text(
                encoding="utf-8"
            )
        )

    def tag_step(self):
        return next(
            step
            for step in self.workflow()["jobs"]["build-and-push"]["steps"]
            if step.get("id") == "tags"
        )

    def run_tag_script(self, *, event_name, ref_name="", input_tag="", output_path):
        environment = os.environ.copy()
        environment.update(
            {
                "EVENT_NAME": event_name,
                "REF_NAME": ref_name,
                "INPUT_TAG": input_tag,
                "LC_ALL": "C",
                "GITHUB_REPOSITORY": "ExampleOwner/Twitch-Farm",
                "GITHUB_OUTPUT": str(output_path),
            }
        )
        return subprocess.run(
            ["/bin/bash", "-c", self.tag_step()["run"]],
            cwd=settings.BASE_DIR,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_smoke_gate_runs_before_registry_login_and_multiarch_publish(self):
        workflow = self.workflow()
        steps = workflow["jobs"]["build-and-push"]["steps"]
        uses = [step.get("uses") for step in steps]

        qemu_index = uses.index("docker/setup-qemu-action@v4")
        buildx_index = uses.index("docker/setup-buildx-action@v3")
        build_indexes = [
            index
            for index, value in enumerate(uses)
            if value == "docker/build-push-action@v6"
        ]
        self.assertEqual(len(build_indexes), 2)
        smoke_build_index, publish_build_index = build_indexes
        smoke_run_index = next(
            index
            for index, step in enumerate(steps)
            if step.get("name") == "Run fresh-volume crash/recovery smoke"
        )
        tags_index = next(index for index, step in enumerate(steps) if step.get("id") == "tags")
        login_index = uses.index("docker/login-action@v3")

        self.assertLess(qemu_index, buildx_index)
        self.assertLess(buildx_index, tags_index)
        self.assertLess(tags_index, smoke_build_index)
        self.assertLess(smoke_build_index, smoke_run_index)
        self.assertLess(smoke_run_index, login_index)
        self.assertLess(login_index, publish_build_index)
        self.assertEqual(steps[qemu_index]["with"]["platforms"], "arm64")
        self.assertEqual(
            steps[smoke_build_index]["with"],
            {
                "context": ".",
                "load": True,
                "platforms": "linux/amd64",
                "tags": "twitch-farm-smoke:${{ github.sha }}",
                "provenance": False,
                "sbom": False,
            },
        )
        self.assertEqual(
            steps[smoke_run_index]["env"],
            {"SMOKE_IMAGE": "twitch-farm-smoke:${{ github.sha }}"},
        )
        self.assertEqual(steps[smoke_run_index]["timeout-minutes"], 5)
        self.assertEqual(
            steps[smoke_run_index]["run"],
            'bash tests/docker_crash_recovery_smoke.sh "$SMOKE_IMAGE"',
        )
        self.assertTrue(steps[publish_build_index]["with"]["push"])
        self.assertEqual(steps[publish_build_index]["with"]["context"], ".")
        self.assertEqual(
            steps[publish_build_index]["with"]["platforms"],
            "linux/amd64,linux/arm64",
        )

    def test_smoke_gate_uses_dummy_config_and_disposable_state(self):
        script = (
            Path(settings.BASE_DIR) / "tests/docker_crash_recovery_smoke.sh"
        ).read_text(encoding="utf-8")

        self.assertIn("config.yaml.example", script)
        self.assertIn("TWITCH_FARM_FAKE_MINER=1", script)
        self.assertIn('docker volume create "$DATA_VOLUME"', script)
        self.assertIn("trap cleanup EXIT", script)
        self.assertIn('docker volume rm "$DATA_VOLUME"', script)
        self.assertIn("run_with_timeout()", script)
        self.assertIn("TRACKED_CONTAINERS=(", script)
        self.assertIn("cleanup_failed=0", script)
        self.assertIn("status=1", script)
        self.assertNotIn("docker run --rm", script)
        lines = script.splitlines()
        unbounded_docker_calls = [
            line
            for index, line in enumerate(lines)
            if line.lstrip().startswith("docker ")
            and not (
                index > 0
                and "run_with_timeout" in lines[index - 1]
                and lines[index - 1].rstrip().endswith("\\")
            )
        ]
        self.assertFalse(unbounded_docker_calls)
        for container in (
            "MIGRATE_CONTAINER",
            "SYNC_CONTAINER",
            "ENQUEUE_CONTAINER",
            "WORKER",
        ):
            self.assertIn(f'--name "${container}"', script)
        self.assertIn("'YOUR_PASSWORD_HERE' not in encoded", script)
        self.assertIn("b'YOUR_PASSWORD_HERE' in part", script)
        self.assertIn("part.startswith(b'--channel')", script)
        self.assertNotIn("/app/cookies", script)

        syntax = subprocess.run(
            ["/bin/bash", "-n", str(Path(settings.BASE_DIR) / "tests/docker_crash_recovery_smoke.sh")],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(syntax.returncode, 0, syntax.stderr)

    def test_release_permissions_are_scoped_per_job(self):
        workflow = self.workflow()

        self.assertNotIn("permissions", workflow)
        self.assertEqual(
            workflow["jobs"]["test"]["permissions"],
            {"contents": "read"},
        )
        self.assertEqual(
            workflow["jobs"]["build-and-push"]["permissions"],
            {"contents": "read", "packages": "write"},
        )
        checkout_steps = [
            step
            for job in workflow["jobs"].values()
            for step in job["steps"]
            if step.get("uses") == "actions/checkout@v4"
        ]
        self.assertEqual(len(checkout_steps), 2)
        for step in checkout_steps:
            self.assertEqual(step.get("with"), {"persist-credentials": False})

    def test_tag_step_keeps_github_expressions_out_of_shell_source(self):
        step = self.tag_step()
        script = step["run"]

        self.assertEqual(
            step["env"],
            {
                "EVENT_NAME": "${{ github.event_name }}",
                "REF_NAME": "${{ github.ref_name }}",
                "INPUT_TAG": "${{ inputs.tag }}",
                "LC_ALL": "C",
            },
        )
        self.assertNotIn("${{ github.ref_name }}", script)
        self.assertNotIn("${{ inputs.tag }}", script)
        self.assertIn("image_tag=\"$REF_NAME\"", script)
        self.assertIn("image_tag=\"$INPUT_TAG\"", script)
        self.assertIn("^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$", script)
        self.assertIn('>> "$GITHUB_OUTPUT"', script)
        self.assertNotIn(">> $GITHUB_OUTPUT", script)

    def test_tag_script_accepts_valid_push_and_dispatch_tags(self):
        cases = (
            (
                {"event_name": "push", "ref_name": "v2.0.0"},
                "tags=ghcr.io/exampleowner/twitch-farm:v2.0.0,"
                "ghcr.io/exampleowner/twitch-farm:latest\n",
            ),
            (
                {"event_name": "workflow_dispatch", "input_tag": "release_2-rc.1"},
                "tags=ghcr.io/exampleowner/twitch-farm:release_2-rc.1\n",
            ),
        )
        for arguments, expected in cases:
            with self.subTest(arguments=arguments), TemporaryDirectory() as temporary:
                output = Path(temporary) / "github output"
                result = self.run_tag_script(output_path=output, **arguments)

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(output.read_text(encoding="utf-8"), expected)

    def test_tag_script_rejects_invalid_and_malicious_inputs_without_execution(self):
        invalid_tags = (
            "",
            "-leading-dash",
            "bad/tag",
            "bad:tag",
            "valid\nsecond-output=attacker",
            "a" * 129,
        )
        for invalid_tag in invalid_tags:
            for event_name in ("push", "workflow_dispatch"):
                with (
                    self.subTest(event_name=event_name, invalid_tag=invalid_tag),
                    TemporaryDirectory() as temporary,
                ):
                    output = Path(temporary) / "github output"
                    output.touch()
                    result = self.run_tag_script(
                        event_name=event_name,
                        ref_name=invalid_tag,
                        input_tag=invalid_tag,
                        output_path=output,
                    )

                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(output.read_text(encoding="utf-8"), "")

        for event_name in ("push", "workflow_dispatch"):
            with self.subTest(event_name=event_name), TemporaryDirectory() as temporary:
                marker = Path(temporary) / "injected"
                payload = f"$(touch {marker})"
                output = Path(temporary) / "github output"
                output.touch()
                result = self.run_tag_script(
                    event_name=event_name,
                    ref_name=payload,
                    input_tag=payload,
                    output_path=output,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(marker.exists())
                self.assertEqual(output.read_text(encoding="utf-8"), "")
