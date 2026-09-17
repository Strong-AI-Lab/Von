"""Private Unix-socket transport for an operator-bound Android host command.

Use an SSH StreamLocal reverse forward for a test host without inbound SSH.
Both socket parents must be private; only the trusted canonical controller gets
access. No TCP listener, client-selected command, credentials or new scheduler.
"""

import argparse
import json
import os
import socket
import sys
from pathlib import Path


def receive(sock, limit):
    parts = []
    length = 0
    while data := sock.recv(65536):
        parts.append(data)
        length += len(data)
        if length > limit:
            raise ValueError("Transport payload exceeds bound")
    return json.loads(b"".join(parts))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--binding")
    args = parser.parse_args()
    os.umask(0o077)
    path = Path(args.socket)
    info = path.parent.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise PermissionError("Transport socket directory must be owner-only")
    if not args.binding:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(120)
            client.connect(str(path))
            client.sendall(json.dumps(json.load(sys.stdin)).encode())
            client.shutdown(socket.SHUT_WR)
            print(json.dumps(receive(client, 32 * 1024**2)))
        return
    try:
        from .codex_von_android import binding, lifecycle
    except ImportError:
        from codex_von_android import binding, lifecycle
    spec = binding(args.binding)
    # An existing socket may be an active listener: never replace it blindly.
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(path))
        path.chmod(0o600)
        listener.listen(4)
        try:
            while True:
                client, _ = listener.accept()
                with client:
                    client.settimeout(120)
                    try:
                        result = lifecycle(spec, receive(client, 65536), args.binding)
                    except Exception as exc:
                        result = {"error": type(exc).__name__}
                    client.sendall(json.dumps(result).encode())
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
