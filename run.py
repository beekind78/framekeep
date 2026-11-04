"""Launch, authenticate, reuse and stop Framekeep on this computer only."""

import argparse
import json
import secrets
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

import uvicorn

from framekeep.access import LocalAccess, RuntimeStore, listener_proof


def local_request(url, *, control=None, post=False):
    headers = {"Content-Type": "application/json"}
    if control is not None:
        headers["X-Framekeep-Control"] = control
    request = urllib.request.Request(url, data=b"{}" if post else None, headers=headers)
    # The launcher credential must never be routed through an HTTP proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=3) as response:
        if int(response.headers.get("Content-Length", "0")) > 16_384:
            raise ValueError("Unexpected response from the local listener.")
        payload = response.read(16_385)
    if len(payload) > 16_384:
        raise ValueError("Unexpected response from the local listener.")
    return json.loads(payload)


def reuse_instance(args, store, url):
    try:
        record = store.read(args.port)
    except FileNotFoundError:
        raise ValueError(
            f"Port {args.port} is unavailable and no matching private session was found. "
            "Close the previous Framekeep instance, then reopen Start Framekeep.cmd."
        ) from None
    challenge = secrets.token_hex(32)
    health = local_request(url + "/api/health?challenge=" + challenge)
    expected = listener_proof(record["control"], record["instance"], challenge)
    proof = health.get("proof", "")
    if (
        health.get("app") != "framekeep"
        or health.get("instance") != record["instance"]
        or not isinstance(proof, str)
        or not secrets.compare_digest(proof.encode(), expected.encode())
    ):
        raise ValueError("The existing listener could not prove it is your Framekeep instance.")
    # Do not send any credential until the random challenge proves the listener's identity.
    if args.stop:
        local_request(url + "/api/shutdown", control=record["control"], post=True)
    elif not args.no_browser:
        ticket = local_request(url + "/api/launch", control=record["control"], post=True)["ticket"]
        webbrowser.open(url + "/#launch=" + ticket)


def run(args):
    store = RuntimeStore()
    url = f"http://127.0.0.1:{args.port}"
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform == "win32":
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        listener.bind(("127.0.0.1", args.port))
        listener.listen(128)
    except OSError:
        listener.close()
        reuse_instance(args, store, url)
        return
    if args.stop:
        listener.close()
        return
    access = LocalAccess()
    try:
        # Own the socket before publishing a fresh, per-process protected credential.
        store.write(args.port, access)
        from framekeep.app import create_app

        app = create_app(access=access)
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning", access_log=False)
        )
        app.state.shutdown = lambda: setattr(server, "should_exit", True)

        def open_when_ready():
            deadline = time.monotonic() + 15
            while not server.started and time.monotonic() < deadline:
                if server.should_exit:
                    return
                time.sleep(0.05)
            if server.started:
                webbrowser.open(url + "/#launch=" + access.ticket())

        if not args.no_browser:
            threading.Thread(target=open_when_ready, daemon=True).start()
        server.run(sockets=[listener])
    finally:
        listener.close()
        store.remove(args.port, access.instance)


def main():
    parser = argparse.ArgumentParser(description="Framekeep local photo organizer")
    parser.add_argument("--port", type=int, default=47831)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--stop", action="store_true", help="Gracefully stop your authenticated local server")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Choose a TCP port between 1 and 65535.")
    try:
        run(args)
        return 0
    except (OSError, ValueError) as error:
        message = "Framekeep could not open securely. " + str(error)
        if sys.stderr is not None:
            print(message, file=sys.stderr)
        if sys.platform == "win32" and not args.no_browser:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, "Framekeep", 0x30)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
