"""Bootstrap the local environment and open the application in a browser."""

import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import time
from urllib.error import URLError
from urllib.request import urlopen
import venv
import webbrowser

ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = 5000
URL = f"http://{HOST}:{PORT}"
PORT_FILE = ROOT / "instance" / "launcher-port"


def source_version():
    digest = hashlib.sha256()
    for name in ("app.py", "launcher.py", "data_transfer.py", "database_migrations.py"):
        digest.update(name.encode("utf-8"))
        digest.update((ROOT / name).read_bytes())
    return digest.hexdigest()


def instance_id(database_path):
    value = f"{ROOT}\0{Path(database_path).resolve()}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def expected_instance():
    database = os.environ.get(
        "BASKET_DATABASE", str(ROOT / "instance" / "basket.sqlite3")
    )
    return instance_id(database)


def is_running(url=None):
    try:
        with urlopen((url or URL) + "/health", timeout=1) as response:
            payload = json.load(response)
            return (
                isinstance(payload, dict)
                and payload.get("instance") == expected_instance()
                and payload.get("source_version") == source_version()
            )
    except (URLError, OSError, ValueError):
        return False


def known_addresses():
    addresses = [URL]
    try:
        previous_port = int(PORT_FILE.read_text(encoding="ascii").strip())
        if 1 <= previous_port <= 65535:
            addresses.append(f"http://{HOST}:{previous_port}")
    except (OSError, ValueError):
        pass
    return list(dict.fromkeys(addresses))


def free_port():
    with socket.socket() as probe:
        try:
            probe.bind((HOST, PORT))
        except OSError:
            probe.bind((HOST, 0))
        return probe.getsockname()[1]


def prepare_environment():
    environment = ROOT / ".venv"
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        print(
            "Подготавливаю приложение. Это нужно только при первом запуске.", flush=True
        )
        venv.EnvBuilder(with_pip=True).create(environment)
    requirements = ROOT / "requirements.txt"
    fingerprint = hashlib.sha256(requirements.read_bytes()).hexdigest()
    marker = environment / ".basket-requirements.sha256"
    if (
        not marker.is_file()
        or marker.read_text(encoding="utf-8").strip() != fingerprint
    ):
        print(
            "Устанавливаю необходимые библиотеки. Для установки нужен интернет.",
            flush=True,
        )
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "-r",
                str(requirements),
            ],
            cwd=ROOT,
            check=True,
        )
        marker.write_text(fingerprint, encoding="utf-8")
    return python


def main():
    os.chdir(ROOT)
    for address in known_addresses():
        if is_running(address):
            webbrowser.open(address)
            print("Приложение уже работает. Открываю его в браузере.")
            return 0
    server = None
    try:
        port = free_port()
        address = f"http://{HOST}:{port}"
        if port != PORT:
            print(f"Порт {PORT} занят. Использую свободный порт {port}.", flush=True)
        python = prepare_environment()
        environment = os.environ.copy()
        environment["BASKET_PORT"] = str(port)
        server = subprocess.Popen(
            [str(python), str(ROOT / "app.py")], cwd=ROOT, env=environment
        )
        deadline = time.monotonic() + 30
        while not is_running(address):
            if server.poll() is not None:
                print("Приложение не запустилось. Сообщение об ошибке находится выше.")
                return 1
            if time.monotonic() >= deadline:
                print(
                    "Приложение не ответило за 30 секунд. Сохраните текст сообщений из этого окна."
                )
                return 1
            time.sleep(0.1)
        PORT_FILE.parent.mkdir(parents=True, exist_ok=True)
        PORT_FILE.write_text(str(port), encoding="ascii")
        print(f"Сайт открыт по адресу {address}", flush=True)
        print(
            "Это окно можно свернуть. Для завершения работы нажмите Ctrl+C.", flush=True
        )
        webbrowser.open(address)
        return server.wait()
    except KeyboardInterrupt:
        return 0
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"Не удалось подготовить приложение: {error}")
        print(
            "Проверьте установку Python и подключение к интернету. Инструкция есть в README.md."
        )
        return 1
    finally:
        if server is not None and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()


if __name__ == "__main__":
    raise SystemExit(main())
