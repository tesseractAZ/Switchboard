#!/usr/bin/env python3
"""Regenerate the documentation screenshots in switchboard/docs/img/.

The screenshots in README.md and switchboard/DOCS.md are rendered from the REAL
user interfaces so they can never drift into fiction:

    dashboard.png  the Ingress dashboard — webui/app.py's INDEX_HTML, served with
                   a stubbed /api/status so it paints without a live PBX
    console.png    the operator console — console/console.py's render(), its ANSI
                   output converted to a styled terminal page

Both are fed the SAME fixed, generic example data (Kitchen / Living Room /
ext 101…). That is deliberate: this repository is public, so a screenshot must
never carry a real room name, LAN address, phone number, or SIP provider.

Run it after changing either UI:

    python3 scripts/build-screenshots.py

Requires a Chrome/Chromium binary (macOS Google Chrome, or google-chrome /
chromium / chromium-browser on Linux). Output is deterministic for a given
browser + font stack; regenerate on one machine so the committed pair stays
visually consistent.
"""

from __future__ import annotations

import argparse
import html as H
import importlib.machinery
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# --------------------------------------------------------------------------- #
# The example board. One definition drives BOTH screenshots so the dashboard and
# the console tell the same story: a live internal call, a ringing room, a
# message-waiting flag, an offline handset, a registered trunk, resident STT.
# `rtt` is microseconds (what the AMI reports); the UIs render it as ms.
# --------------------------------------------------------------------------- #
ROOMS = [
    {"ext": "101", "label": "Kitchen", "registered": True, "device_state": "Not in use",
     "rtt": "2100", "contact_status": "Reachable"},
    {"ext": "102", "label": "Living Room", "registered": True, "device_state": "Not in use",
     "rtt": "2400", "contact_status": "Reachable", "mwi": True},
    {"ext": "103", "label": "Study", "registered": True, "device_state": "In use",
     "call_state": "Talking", "peer": "Kitchen", "call_peer": "Kitchen",
     "codec": "ulaw", "call_codec": "ulaw", "channel": "PJSIP/103-1",
     "peer_channel": "PJSIP/101-1", "rtt": "1900", "contact_status": "Reachable"},
    {"ext": "104", "label": "Garage", "registered": True, "device_state": "Ringing",
     "call_state": "Ringing", "rtt": "3100", "contact_status": "Reachable"},
    {"ext": "105", "label": "Bedroom", "registered": True, "device_state": "Not in use",
     "rtt": "2600", "contact_status": "Reachable"},
    {"ext": "106", "label": "Cordless", "registered": True, "device_state": "Not in use",
     "rtt": "14800", "contact_status": "Reachable"},
    {"ext": "107", "label": "Guest Room", "registered": False, "device_state": "Unavailable"},
]
CALLS = [
    {"kind": "internal", "detail": "Study ↔ Kitchen", "state": "Talking",
     "duration": "02:14", "codec": "ulaw"},
    {"kind": "outside", "detail": "Garage ↔ Outside", "state": "Ringing",
     "duration": "00:03", "codec": "ulaw"},
]
# target_epoch is deliberately far in the future: a wake-up in the past is filtered
# out of the dashboard, which would silently drop the feature from the screenshot
# whenever this script is re-run later.
WAKEUPS = [{"ext": "105", "label": "Bedroom", "hhmm": "06:30", "target_epoch": 9999999999}]
TRUNK = {"enabled": True, "provider": "my-provider.example.com", "registration": "Registered"}
NOW = 1784596400  # fixed clock so re-runs are byte-stable

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
]


def find_chrome() -> str:
    for c in CHROME_CANDIDATES:
        p = c if Path(c).is_file() else shutil.which(c)
        if p:
            return p
    sys.exit("error: no Chrome/Chromium found (install Google Chrome, or chromium on Linux)")


def shoot(chrome: str, url: str, out: Path, width: int, height: int) -> None:
    """Capture one page at 2x (retina-crisp for docs)."""
    subprocess.run(
        [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
         "--force-device-scale-factor=2", f"--window-size={width},{height}",
         "--virtual-time-budget=4000", f"--screenshot={out}", url],
        capture_output=True, check=False,
    )
    if not out.exists() or out.stat().st_size == 0:
        sys.exit(f"error: chrome produced no screenshot for {url}")


# --------------------------------------------------------------------------- #
# 1. Dashboard — the real INDEX_HTML with a stubbed API and a forced light theme
# --------------------------------------------------------------------------- #
def build_dashboard_html(repo_root: Path) -> str:
    app = (repo_root / "switchboard/rootfs/usr/share/switchboard/webui/app.py").read_text()
    m = re.search(r'INDEX_HTML = """(.*?)^"""', app, re.S | re.M)
    if not m:
        sys.exit("error: could not extract INDEX_HTML from webui/app.py")
    html = m.group(1)

    status = {"ami_ok": True, "rooms": ROOMS, "calls": CALLS, "wakeups": WAKEUPS,
              "trunk": TRUNK, "stt": "up"}
    stub = """<script>
(function(){
  const STATUS = %s;
  window.fetch = function(u){
    const url = (typeof u === 'string') ? u : (u && u.url) || '';
    if (url.indexOf('api/status') !== -1)
      return Promise.resolve(new Response(JSON.stringify(STATUS),
        {status:200, headers:{'Content-Type':'application/json'}}));
    return Promise.resolve(new Response('{}', {status:200}));
  };
  // The Lights panel needs a live Home Assistant; hide it rather than show
  // "unavailable" in a marketing shot.
  function hideLights(){
    document.querySelectorAll('h2,h3').forEach(h=>{
      if(h.textContent.trim().replace(/[^A-Za-z]/g,'')==='Lights'){
        let n=h; while(n){const x=n.nextElementSibling; n.style.display='none'; n=x;}
      }});
  }
  window.addEventListener('load',()=>{setTimeout(hideLights,300);setTimeout(hideLights,900);});
})();
</script>""" % json.dumps(status)
    html = html.replace("<head>", "<head>\n" + stub, 1)
    # Headless Chrome reports prefers-color-scheme: dark. Pin the light theme so
    # the committed screenshot is stable regardless of the host's appearance:
    # make the dark block unmatchable, then restore light UA colors explicitly
    # (the page declares `color-scheme: light dark`, which otherwise leaves the
    # text white on the light canvas).
    html = html.replace("@media (prefers-color-scheme: dark)",
                        "@media (prefers-color-scheme: dark) and (min-width: 999999px)")
    html = html.replace("</head>",
                        '<style>:root{color-scheme:light !important;}'
                        'body{color:#14171a !important;background:#f6f7f9 !important;}</style>\n</head>', 1)
    return html


# --------------------------------------------------------------------------- #
# 2. Console — console.py's own render(), ANSI translated to HTML
# --------------------------------------------------------------------------- #
ANSI_PALETTE = {"32": "#4ec9a0", "31": "#e06c75", "33": "#e5c07b",
                "36": "#56b6c2", "34": "#61afef", "90": "#7d828c"}


def ansi_to_html(line: str) -> str:
    out, i, opened = [], 0, 0
    while i < len(line):
        if line[i] == "\x1b":
            m = re.match(r"\x1b\[([0-9;]*)m", line[i:])
            if m:
                for code in (m.group(1).split(";") if m.group(1) else ["0"]):
                    if code in ("0", ""):
                        out.append("</span>" * opened); opened = 0
                    elif code == "1":
                        out.append('<span style="font-weight:700">'); opened += 1
                    elif code == "2":
                        out.append('<span style="opacity:.6">'); opened += 1
                    elif code in ANSI_PALETTE:
                        out.append(f'<span style="color:{ANSI_PALETTE[code]}">'); opened += 1
                i += m.end(); continue
        out.append(H.escape(line[i])); i += 1
    return "".join(out) + "</span>" * opened


def build_console_html(repo_root: Path) -> str:
    share = repo_root / "switchboard/rootfs/usr/share/switchboard"
    for sub in ("console", "webui", "wakeup"):
        sys.path.insert(0, str(share / sub))
    console = importlib.machinery.SourceFileLoader(
        "console", str(share / "console/console.py")).load_module()

    board = {"ami_ok": True, "rooms": ROOMS, "calls": CALLS, "wakeups": WAKEUPS,
             "trunk_reg": TRUNK["registration"], "stt": "up", "ts": 0.0}
    # h is sized to the content so the board isn't vertically centred inside a
    # tall, mostly-empty terminal.
    lines = console.render(board, {"sel": 0, "mode": "normal", "w": 94, "h": 22}, NOW)
    while lines and not lines[0].strip():
        lines.pop(0)
    body = "\n".join(ansi_to_html(l) for l in lines)
    return (
        '<!doctype html><html><head><meta charset="utf-8"><style>\n'
        "html,body{margin:0;background:#0f1216}\n"
        ".wrap{padding:26px;background:#0f1216;display:inline-block}\n"
        ".term{padding:20px 26px;background:#12151b;color:#c6ccd6;"
        'font:15px/1.55 "SF Mono","Menlo","DejaVu Sans Mono",monospace;white-space:pre;'
        "border-radius:10px;box-shadow:0 8px 30px rgba(0,0,0,.4)}\n"
        f'</style></head><body><div class="wrap"><div class="term">{body}</div></div></body></html>'
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Regenerate the documentation screenshots")
    ap.add_argument("--repo-root", default=None)
    ap.add_argument("--out-dir", default=None, help="default: switchboard/docs/img")
    args = ap.parse_args()

    repo_root = Path(args.repo_root).resolve() if args.repo_root \
        else Path(__file__).resolve().parent.parent
    out_dir = Path(args.out_dir).resolve() if args.out_dir \
        else repo_root / "switchboard/docs/img"
    out_dir.mkdir(parents=True, exist_ok=True)
    chrome = find_chrome()

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / "dashboard.html").write_text(build_dashboard_html(repo_root), encoding="utf-8")
        (tmp / "console.html").write_text(build_console_html(repo_root), encoding="utf-8")
        # Sizes are tuned to the fixed example data above; a different font stack
        # may leave a little background padding, which is harmless.
        shoot(chrome, (tmp / "dashboard.html").as_uri(), out_dir / "dashboard.png", 900, 1066)
        shoot(chrome, (tmp / "console.html").as_uri(), out_dir / "console.png", 944, 604)

    for name in ("dashboard.png", "console.png"):
        p = out_dir / name
        print(f"wrote {p} ({p.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
