import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
from urllib.request import urlopen

from werkzeug.serving import make_server

from app import create_app
import launcher


def test_reuses_only_the_matching_application(tmp_path, monkeypatch):
    database = str(tmp_path / "launch.sqlite3")
    application = create_app(
        {"TESTING": True, "SECRET_KEY": "launch-test", "DATABASE": database}
    )
    server = make_server("127.0.0.1", 0, application)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setattr(launcher, "URL", url)
    monkeypatch.setenv("BASKET_DATABASE", database)
    opened = []
    monkeypatch.setattr(launcher.webbrowser, "open", lambda value: opened.append(value))
    try:
        assert launcher.is_running()
        assert launcher.main() == 0
        assert opened == [url]
        application.config["SOURCE_VERSION"] = "old-build"
        assert not launcher.is_running()
        application.config["SOURCE_VERSION"] = launcher.source_version()
        monkeypatch.setenv("BASKET_DATABASE", str(tmp_path / "another.sqlite3"))
        assert not launcher.is_running()
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_dependency_setup_is_repeated_only_when_requirements_change(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    python = (
        tmp_path
        / ".venv"
        / ("Scripts/python.exe" if launcher.os.name == "nt" else "bin/python")
    )
    python.parent.mkdir(parents=True)
    python.touch()
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("Flask==3.1.2\n", encoding="utf-8")
    installs = []
    monkeypatch.setattr(
        launcher.subprocess, "run", lambda command, **options: installs.append(command)
    )
    assert launcher.prepare_environment() == python
    assert launcher.prepare_environment() == python
    assert len(installs) == 1
    requirements.write_text("Flask==3.1.2\nopenpyxl==3.1.5\n", encoding="utf-8")
    launcher.prepare_environment()
    assert len(installs) == 2


def test_launcher_starts_server_before_opening_browser(tmp_path, monkeypatch):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    database = str(tmp_path / "launched.sqlite3")
    monkeypatch.setenv("BASKET_DATABASE", database)
    monkeypatch.setenv("SECRET_KEY", "launch-test")
    monkeypatch.setattr(launcher, "PORT", port)
    monkeypatch.setattr(launcher, "PORT_FILE", tmp_path / "instance" / "launcher-port")
    monkeypatch.setattr(launcher, "URL", url)
    monkeypatch.setattr(launcher, "prepare_environment", lambda: Path(sys.executable))
    original_popen = subprocess.Popen
    processes = []

    def start_server(command, **options):
        process = original_popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **options,
        )
        processes.append(process)
        return process

    monkeypatch.setattr(launcher.subprocess, "Popen", start_server)
    opened = []

    def open_browser(value):
        with urlopen(value + "/health", timeout=2) as response:
            assert json.load(response)["instance"] == launcher.expected_instance()
        with urlopen(value, timeout=2) as response:
            assert response.status == 200
        opened.append(value)
        processes[0].terminate()
        return True

    monkeypatch.setattr(launcher.webbrowser, "open", open_browser)
    launcher.main()
    assert opened == [url]
    assert processes[0].poll() is not None
    assert Path(database).is_file()


def test_busy_port_uses_another_address_and_reuses_it(tmp_path, monkeypatch):
    foreign_db = str(tmp_path / "other.sqlite3")
    foreign_app = create_app(
        {"TESTING": True, "SECRET_KEY": "test", "DATABASE": foreign_db}
    )
    foreign_server = make_server("127.0.0.1", 0, foreign_app)
    thread = threading.Thread(target=foreign_server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{foreign_server.server_port}"
    target_db = tmp_path / "new-instance" / "basket.sqlite3"
    port_file = tmp_path / "new-instance" / "launcher-port"
    monkeypatch.setenv("BASKET_DATABASE", str(target_db))
    monkeypatch.setenv("SECRET_KEY", "test")
    monkeypatch.setattr(launcher, "PORT", foreign_server.server_port)
    monkeypatch.setattr(launcher, "URL", base_url)
    monkeypatch.setattr(launcher, "PORT_FILE", port_file)
    monkeypatch.setattr(launcher, "prepare_environment", lambda: Path(sys.executable))
    original_popen = subprocess.Popen
    processes = []

    def start_server(command, **options):
        process = original_popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options
        )
        processes.append(process)
        return process

    monkeypatch.setattr(launcher.subprocess, "Popen", start_server)
    opened = []

    def open_browser(value):
        opened.append(value)
        assert value != base_url
        with urlopen(value + "/health", timeout=2) as response:
            assert json.load(response)["instance"] == launcher.expected_instance()
        if len(opened) == 1:
            assert launcher.main() == 0
            assert len(processes) == 1
            processes[0].terminate()
        return True

    monkeypatch.setattr(launcher.webbrowser, "open", open_browser)
    try:
        launcher.main()
        assert len(opened) == 2 and opened[0] == opened[1]
        assert target_db.is_file()
        assert port_file.is_file()
        with urlopen(base_url + "/health", timeout=2) as response:
            assert json.load(response)["instance"] == launcher.instance_id(foreign_db)
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
        foreign_server.shutdown()
        thread.join(timeout=5)
        foreign_server.server_close()
