"""
Shared fixtures for integration tests.

These tests run against a live container instance with the demo dataset.
The instance must already be running — use deploy scripts to set it up:

    bash deploy/setup.sh integration-test --init
    systemctl --user start mtgc-integration-test

Or pass an existing instance via --instance:

    uv run pytest tests/integration/ --instance sealed-collection

The fixture discovers the port automatically via `podman port`.
"""

import json
import subprocess
import urllib.request
import ssl

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--instance",
        default="integration-test",
        help="Container instance name to test against (default: integration-test)",
    )


@pytest.fixture(scope="session")
def instance_name(request):
    return request.config.getoption("--instance")


@pytest.fixture(scope="session")
def base_url(instance_name):
    """Discover the HTTPS base URL for the running container instance."""
    container_name = f"systemd-mtgc-{instance_name}"
    try:
        result = subprocess.run(
            ["podman", "port", container_name, "8081/tcp"],
            capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip(
            f"Container '{container_name}' not running. "
            f"Start it with: bash deploy/setup.sh {instance_name} --init && "
            f"systemctl --user start mtgc-{instance_name}"
        )

    port_line = result.stdout.strip()
    if not port_line:
        pytest.skip(f"Could not determine port for '{container_name}'")

    # Parse "0.0.0.0:36305" -> 36305
    port = port_line.split(":")[-1]
    url = f"https://localhost:{port}"

    # Verify the instance is actually responding
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        req = urllib.request.Request(f"{url}/")
        urllib.request.urlopen(req, context=ctx, timeout=5)
    except Exception:
        pytest.skip(f"Instance at {url} not responding")

    return url


@pytest.fixture(scope="session")
def api(base_url):
    """HTTP client for making API requests to the test instance."""
    return APIClient(base_url)


class APIClient:
    """Minimal HTTP client for integration tests (no external deps)."""

    def __init__(self, base_url: str):
        self.base_url = base_url
        self._ctx = ssl.create_default_context()
        self._ctx.check_hostname = False
        self._ctx.verify_mode = ssl.CERT_NONE

    def get(self, path: str) -> tuple:
        """GET request. Returns (status_code, parsed_json)."""
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url)
        try:
            resp = urllib.request.urlopen(req, context=self._ctx, timeout=30)
            body = json.loads(resp.read())
            return resp.status, body
        except urllib.error.HTTPError as e:
            body = json.loads(e.read())
            return e.code, body

    def post(self, path: str, data: dict, timeout: int = 30) -> tuple:
        """POST JSON request. Returns (status_code, parsed_json)."""
        return self._json_request("POST", path, data, timeout=timeout)

    def put(self, path: str, data: dict) -> tuple:
        """PUT JSON request. Returns (status_code, parsed_json)."""
        return self._json_request("PUT", path, data)

    def delete(self, path: str) -> tuple:
        """DELETE request. Returns (status_code, parsed_json)."""
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url, method="DELETE")
        try:
            resp = urllib.request.urlopen(req, context=self._ctx, timeout=30)
            body = json.loads(resp.read())
            return resp.status, body
        except urllib.error.HTTPError as e:
            body = json.loads(e.read())
            return e.code, body

    def _json_request(self, method: str, path: str, data: dict, timeout: int = 30) -> tuple:
        url = f"{self.base_url}{path}"
        body = json.dumps(data).encode()
        req = urllib.request.Request(
            url, data=body, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            resp = urllib.request.urlopen(req, context=self._ctx, timeout=timeout)
            resp_body = json.loads(resp.read())
            return resp.status, resp_body
        except urllib.error.HTTPError as e:
            resp_body = json.loads(e.read())
            return e.code, resp_body
