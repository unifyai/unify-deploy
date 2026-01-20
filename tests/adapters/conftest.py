"""
Pytest configuration file for the adapters tests.

This file contains shared fixtures and configuration for testing the adapters server.
"""

from dotenv import load_dotenv
import pytest
import os
import sys
import time
import requests
import subprocess
import signal
from pathlib import Path
from typing import Generator

load_dotenv()
load_dotenv(".env.temp")

# Add the project root to the Python path so tests can import modules
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))


@pytest.fixture
def adapters_server() -> Generator[str, None, None]:
    """
    Fixture that starts the local adapters server for testing.

    Yields the base URL of the running server.
    """
    server_url = "http://localhost:8080"

    # Check if server is already running
    try:
        response = requests.get(f"{server_url}/health", timeout=2)
        if response.status_code == 200:
            print("✅ Adapters server already running")
            yield server_url
            return
    except requests.exceptions.RequestException:
        pass

    # Start the server
    print("🚀 Starting adapters server for testing...")

    # Start the FastAPI server with uvicorn
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "adapters.main:app",
            "--host",
            "0.0.0.0",
            "--port",
            "8080",
        ],
        cwd=project_root,
        env=os.environ,
        stdout=sys.stdout,
        stderr=sys.stderr,
        preexec_fn=os.setsid if os.name != "nt" else None,
    )

    # Wait for server to start
    max_wait = 30
    wait_time = 0
    while wait_time < max_wait:
        try:
            response = requests.get(f"{server_url}/health", timeout=2)
            if response.status_code == 200:
                print("✅ Adapters server started successfully")
                break
        except requests.exceptions.RequestException:
            pass

        time.sleep(1)
        wait_time += 1

    if wait_time >= max_wait:
        process.terminate()
        raise RuntimeError("Failed to start adapters server within 30 seconds")

    yield server_url

    # Cleanup: stop the server
    print("🛑 Stopping adapters server...")
    try:
        if os.name != "nt":
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=10)
    except (subprocess.TimeoutExpired, ProcessLookupError):
        process.kill()


@pytest.fixture
def test_client(adapters_server: str):
    """
    Test client for making requests to the adapters server.

    Args:
        adapters_server: Base URL of the running server

    Returns:
        requests.Session: Configured session for testing
    """
    session = requests.Session()
    session.base_url = adapters_server

    def request(method: str, endpoint: str, **kwargs):
        """Helper method to make requests to the server."""
        url = f"{session.base_url}{endpoint}"
        print(f"Making request to {url} {method} {kwargs}")
        return session.request(method, url, **kwargs)

    session.make_request = request
    return session


# Configure pytest options
def pytest_configure(config):
    """Configure pytest with custom options."""
    config.addinivalue_line(
        "markers", "slow: marks tests as slow (deselect with '-m \"not slow\"')"
    )
