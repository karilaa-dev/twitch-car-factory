from __future__ import annotations

import os
from pathlib import Path
import subprocess
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
            ("farm.example.com", "farm.example.com"),
            (".example.com", "example.com"),
            ("*", "localhost"),
            ("*,.example.com", "example.com"),
            ("[::1]", "[::1]"),
        )
        for allowed_hosts, expected_host in cases:
            with self.subTest(allowed_hosts=allowed_hosts):
                requests = []

                def capture_request(request, *, timeout):
                    requests.append((request, timeout))

                with (
                    patch.dict(os.environ, {"DJANGO_ALLOWED_HOSTS": allowed_hosts}),
                    patch("urllib.request.urlopen", side_effect=capture_request),
                ):
                    exec(command[3], {})

                self.assertEqual(len(requests), 1)
                request, timeout = requests[0]
                self.assertEqual(request.full_url, "http://127.0.0.1:8000/healthz/")
                self.assertEqual(request.get_header("Host"), expected_host)
                self.assertEqual(request.get_header("X-forwarded-proto"), "https")
                self.assertEqual(timeout, 3)


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

    def test_arm64_emulation_is_installed_before_buildx(self):
        workflow = self.workflow()
        steps = workflow["jobs"]["build-and-push"]["steps"]
        uses = [step.get("uses") for step in steps]

        qemu_index = uses.index("docker/setup-qemu-action@v4")
        buildx_index = uses.index("docker/setup-buildx-action@v3")
        tags_index = next(index for index, step in enumerate(steps) if step.get("id") == "tags")
        login_index = uses.index("docker/login-action@v3")
        build_index = uses.index("docker/build-push-action@v6")

        self.assertLess(qemu_index, buildx_index)
        self.assertLess(buildx_index, tags_index)
        self.assertLess(tags_index, login_index)
        self.assertLess(login_index, build_index)
        self.assertEqual(steps[qemu_index]["with"]["platforms"], "arm64")
        self.assertEqual(
            steps[build_index]["with"]["platforms"],
            "linux/amd64,linux/arm64",
        )

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
