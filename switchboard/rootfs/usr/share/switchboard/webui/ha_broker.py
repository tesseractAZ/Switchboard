#!/usr/bin/python3
"""Home Assistant broker: the asterisk user's only way to reach Home Assistant.

The add-on's Supervisor token grants the whole Home Assistant Core API
(config.yaml `homeassistant_api: true`). The root services keep it. Asterisk, its
AGIs, whisper-server and every program the dial plan runs are the `asterisk`
user, and since v0.110.0 they never hold it:

  * the s6 run scripts start them with SUPERVISOR_TOKEN and HASSIO_TOKEN removed
    from the environment;
  * the init step makes /run/s6/container_environment, where s6 keeps a copy of
    each variable, readable by root only.

What those programs still need from Home Assistant they ask this broker for, over
a Unix socket in /run/switchboard (0660 root:asterisk). A Unix socket rather than
a loopback port: the add-on uses host networking, so a TCP port on 127.0.0.1
would be reachable from the whole host, not only from this container.

The broker answers only the request shapes those programs make (``check``), each
with its arguments validated, and forwards an accepted request through
ha_client with the token. Everything else is refused and logged. ha_client uses
the broker by itself whenever it has no token and the socket exists, so the
callers did not change.

    status AGI, wake-up      reads: one entity, all states, the lights template,
                             the home location, calendar events
    automation AGI           light.turn_on / light.turn_off on one light
    wake-up delivery AGI     scene.turn_on on one scene
    announce AGI             media_player.play_media of this add-on's own clip URL
    assistant AGI            the built-in local conversation agent
    switchboard-callqos      sensor.switchboard_last_call, a persistent notification

Protocol: one JSON line {"method", "path", "body"} in, one JSON line
{"status": int|null, "text": str} or {"refused": reason} out.
"""

from __future__ import annotations

import json
import os
import pwd
import re
import socket
import socketserver
import struct
import sys
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import announce_audio  # noqa: E402
import ha_client  # noqa: E402

# The broker holds the token and never asks itself: with no token it must report
# Home Assistant unreachable, not forward the request back into its own socket.
ha_client.BROKER_SOCKET = ""

SOCKET_PATH = os.environ.get("SWITCHBOARD_HA_BROKER", "/run/switchboard/ha.sock")
MAX_REQUEST = 64 * 1024
WEBUI_PORT = os.environ.get("INGRESS_PORT") or "8099"
_lan_ip = announce_audio.lan_ip     # the tests replace it

# What a state read returns to the asterisk user: the entity, its state, and the
# two attributes the callers read (ha_reports: a thermostat's name and current
# temperature). Never camera access tokens or picture URLs, tracker coordinates,
# media titles or anything else a state's attributes carry.
STATE_ATTRS = ("friendly_name", "current_temperature")

# Exact (domain, service) pairs, narrower than ha_client's domain allow-list.
SERVICES = frozenset({
    ("light", "turn_on"), ("light", "turn_off"),
    ("scene", "turn_on"),
    ("media_player", "play_media"),
})
PUSHED_ENTITIES = frozenset({"sensor.switchboard_last_call"})
CONVERSATION_AGENT = "conversation.home_assistant"
_CLIP_URL = re.compile(r"http://[0-9A-Za-z.\-]{1,253}:[0-9]{1,5}/announce/a[0-9]{1,24}\.wav")
_ISO = re.compile(r"[0-9T:.+\-Z ]{10,40}")


def _keys_within(body, allowed: set) -> bool:
    return isinstance(body, dict) and set(body) <= allowed


def _entity_list(value, domain: str) -> bool:
    items = value if isinstance(value, list) else [value]
    return (0 < len(items) <= 32
            and all(isinstance(e, str) and ha_client.is_entity_id(e) and e.startswith(domain + ".")
                    for e in items))


def check(method: str, path: str, body) -> str | None:
    """None when the request is one the asterisk user's programs make, else the
    reason it is refused. Pure, so every rule is unit-tested."""
    if not isinstance(method, str) or not isinstance(path, str):
        return "malformed request"
    if len(path) > 512 or any(c in path for c in "\r\n\t #\\") or ".." in path:
        return "malformed path"
    parts = urlsplit(path)
    route, query = parts.path, parts.query
    segs = route.split("/")[1:]

    if method == "GET":
        if body is not None:
            return "GET with a body"
        if route == "/" and not query:
            return None
        if route == "/states" and not query:
            return None
        if route == "/config" and not query:
            return None
        if len(segs) == 2 and segs[0] == "states" and not query:
            return None if ha_client.is_entity_id(segs[1]) else "bad entity id"
        if len(segs) == 2 and segs[0] == "calendars":
            if not (ha_client.is_entity_id(segs[1]) and segs[1].startswith("calendar.")):
                return "bad calendar id"
            q = parse_qs(query, keep_blank_values=True, strict_parsing=False)
            if set(q) != {"start", "end"} or any(len(v) != 1 or not _ISO.fullmatch(v[0]) for v in q.values()):
                return "bad calendar range"
            return None
        return "GET not allowed"

    if method != "POST":
        return "method not allowed"
    if route == "/template" and not query:
        return None if body == {"template": ha_client._LIGHTS_TEMPLATE} else "only the lights template"
    if len(segs) == 2 and segs[0] == "states" and not query:
        if segs[1] not in PUSHED_ENTITIES:
            return "entity not pushable"
        if not (_keys_within(body, {"state", "attributes"}) and isinstance(body.get("state"), str)
                and len(body["state"]) <= 255
                and isinstance(body.get("attributes", {}), dict)):
            return "bad state body"
        return None
    if len(segs) == 3 and segs[0] == "services":
        domain, service = segs[1], segs[2]
        if (domain, service) == ("conversation", "process"):
            if query != "return_response=true":
                return "bad conversation query"
            if not (_keys_within(body, {"text", "agent_id", "language"})
                    and isinstance(body.get("text"), str) and 0 < len(body["text"]) <= 1000
                    and body.get("agent_id", CONVERSATION_AGENT) == CONVERSATION_AGENT
                    and re.fullmatch(r"[a-z]{2}(-[A-Za-z]{2})?", str(body.get("language", "en")))):
                return "bad conversation body"
            return None
        if query:
            return "unexpected query"
        if (domain, service) == ("persistent_notification", "create"):
            ok = (_keys_within(body, {"message", "title", "notification_id"})
                  and all(isinstance(v, str) for v in body.values())
                  and len(body.get("message", "")) <= 500 and len(body.get("title", "")) <= 120
                  and re.fullmatch(r"[a-z0-9_]{0,64}", body.get("notification_id", "")))
            return None if ok else "bad notification body"
        if (domain, service) not in SERVICES:
            return "service not allowed"
        if domain == "light":
            ok = _keys_within(body, {"entity_id"}) and isinstance(body.get("entity_id"), str) \
                and ha_client.is_light_entity(body["entity_id"])
            return None if ok else "bad light target"
        if domain == "scene":
            ok = _keys_within(body, {"entity_id"}) and _entity_list(body.get("entity_id"), "scene")
            return None if ok else "bad scene target"
        if domain == "media_player":
            ok = (_keys_within(body, {"entity_id", "media_content_id", "media_content_type"})
                  and _entity_list(body.get("entity_id"), "media_player")
                  and isinstance(body.get("media_content_id"), str)
                  and _CLIP_URL.fullmatch(body["media_content_id"])
                  and body.get("media_content_type") == "music")
            if not ok:
                return "bad play_media body"
            # This add-on's own clip, from this host's web UI port only.
            u = urlsplit(body["media_content_id"])
            if u.hostname != _lan_ip() or str(u.port) != str(WEBUI_PORT):
                return "clip URL is not this add-on's"
            return None
    return "POST not allowed"


def _peer_uid(sock: socket.socket) -> int | None:
    try:
        creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        return struct.unpack("3i", creds)[1]
    except (OSError, AttributeError):
        return None


def _allowed_uids() -> set:
    uids = {0}
    try:
        uids.add(pwd.getpwnam("asterisk").pw_uid)
    except KeyError:
        pass
    return uids


def _log(msg: str) -> None:
    sys.stderr.write(f"[ha-broker] {msg}\n")
    sys.stderr.flush()


def _trim_state(st):
    if not isinstance(st, dict):
        return None
    attrs = st.get("attributes") if isinstance(st.get("attributes"), dict) else {}
    return {"entity_id": st.get("entity_id"), "state": st.get("state"),
            "attributes": {k: attrs[k] for k in STATE_ATTRS if k in attrs}}


def filter_reply(method: str, path: str, status, text: str):
    """Trim what a read hands back to only what the callers use. Returns
    (status, text); a reply that cannot be parsed for trimming is withheld."""
    if method != "GET" or status != 200:
        return status, text
    route = urlsplit(path).path
    try:
        if route == "/states":
            data = [t for t in map(_trim_state, json.loads(text)) if t]
        elif route.startswith("/states/"):
            data = _trim_state(json.loads(text))
        elif route == "/config":
            d = json.loads(text)
            data = {"latitude": d.get("latitude"), "longitude": d.get("longitude"),
                    "unit_system": {"temperature": (d.get("unit_system") or {}).get("temperature")}}
        else:
            return status, text
    except (ValueError, TypeError, AttributeError):
        _log(f"withheld an unparseable reply to GET {route}")
        return 502, ""
    return status, json.dumps(data)


def handle_line(line: bytes, forward=None) -> dict:
    """One request line in, one reply dict out. ``forward`` is ha_client._request
    in production and a fake in the tests."""
    forward = forward or ha_client._request
    try:
        req = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return {"refused": "not JSON"}
    if not isinstance(req, dict):
        return {"refused": "not an object"}
    method, path, body = req.get("method"), req.get("path"), req.get("body")
    reason = check(method, path, body)
    if reason:
        _log(f"refused {str(method)[:8]} {str(path)[:120]}: {reason}")
        return {"refused": reason}
    status, text = forward(method, path, body)
    status, text = filter_reply(method, path, status, text or "")
    return {"status": status, "text": text}


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        uid = self.server.peer_uid(self.request)
        if uid not in self.server.allowed_uids:
            _log(f"refused a connection from uid {uid}")
            return
        line = self.rfile.readline(MAX_REQUEST + 1)
        if len(line) > MAX_REQUEST:
            reply = {"refused": "request too large"}
        else:
            reply = handle_line(line, self.server.forward)
        try:
            self.wfile.write(json.dumps(reply).encode() + b"\n")
        except OSError:
            pass


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    forward = None              # ha_client._request; the tests pass a fake
    peer_uid = staticmethod(_peer_uid)
    allowed_uids: set = set()


def make_server(path: str = SOCKET_PATH) -> _Server:
    """Bind the socket 0660, group asterisk, and return the server unstarted."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    old = os.umask(0o117)                      # the socket is created 0660
    try:
        server = _Server(path, _Handler)
    finally:
        os.umask(old)
    try:
        os.chown(path, 0, pwd.getpwnam("asterisk").pw_gid)
    except (KeyError, OSError) as exc:
        _log(f"WARNING: could not give the socket to group asterisk: {exc}")
    # The mode comes from the umask around bind(), with no chmod afterwards:
    # connecting to a Unix socket needs write permission, so group asterisk must
    # have it, and nothing else may. Checked rather than assumed.
    mode = os.stat(path).st_mode & 0o777
    if mode != 0o660:
        _log(f"WARNING: socket mode is {oct(mode)}, expected 0o660")
    server.allowed_uids = _allowed_uids()
    return server


def _warm(stop_after: float = 300.0) -> None:
    """Find the working Home Assistant address once, before any caller waits on
    it: a cold forward may try both candidates (twice the client's timeout)."""
    import time
    deadline = time.monotonic() + stop_after
    while time.monotonic() < deadline:
        if ha_client.available():
            return
        time.sleep(10)


def serve(path: str = SOCKET_PATH) -> None:
    if not os.environ.get("SUPERVISOR_TOKEN") and not os.environ.get("HA_BASE_URL"):
        _log("WARNING: no Supervisor token; every forwarded request will report HA unreachable")
    server = make_server(path)
    _log(f"listening on {path}")
    import threading
    threading.Thread(target=_warm, daemon=True).start()
    server.serve_forever()


if __name__ == "__main__":
    serve()
