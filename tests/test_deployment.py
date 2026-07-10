from __future__ import annotations

import os
from pathlib import Path
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
