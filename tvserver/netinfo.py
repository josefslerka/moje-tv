"""Adresa serveru v domácí síti."""
from __future__ import annotations


def lan_addresses(port: int) -> dict:
    """Adresa serveru v domácí síti, aby šla zkopírovat pro Apple TV a telefon."""
    import socket
    import subprocess
    ip = ""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))   # nic se neodesílá, jen se vybere rozhraní
            ip = s.getsockname()[0]
    except OSError:
        pass
    name = ""
    try:
        name = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True,
                              timeout=2).stdout.strip()
    except Exception:  # noqa: BLE001 - mimo macOS název nemusí jít zjistit
        pass
    return {"ip_url": f"http://{ip}:{port}" if ip else "",
            "local_url": f"http://{name}.local:{port}" if name else ""}
