#!/usr/bin/env python3
"""Proactive device-health monitor for the phone fleet's two "smart" devices —
the WP826 cordless (the alarm/announce endpoint) and the GXW4216 FXS gateway.

WHY this exists (the gap it fills): switchboard-rtpmon already watches SIP
registration + qualify RTT for every extension and fires a FLEET-outage alert
when >= half the fleet drops. But two blind spots remain:

  1. The cordless is a battery + Wi-Fi device that is ALSO where power alarms are
     announced. Its battery dying, its Wi-Fi weakening, or its per-call audio
     quality degrading are invisible to Asterisk (the callee RTP leg is
     unmeasurable from the PBX) — yet they directly threaten alarm delivery.
     The WP826's own HTTP API reports all three (battery %, Wi-Fi RSSI, per-call
     MOS/jitter/loss). We poll it.
  2. A SINGLE critical device going offline (the cordless alone; the whole GXW)
     never trips the half-the-fleet fleet-outage gate. We add a per-device alert.

Design mirrors rtpmon (the foundation): pure classify/transition functions
(unit-tested), ha_client.set_state() pushed sensors for graphing, and a
consecutive-cycle one-shot notify() on unhealthy transitions (copied idiom from
rtpmon.outage_transition so an alert + its recovery collapse to one bell entry).
Gateway registration health is DERIVED from rtpmon's rollup sensor (the reliable,
already-gathered signal) rather than re-probed — the GXW blocks ICMP/HTTP off its
subnet, so an independent ping would false-alarm on a healthy gateway.

Env (bridged by the s6 run script from config.yaml):
  DEVICE_HEALTH_INTERVAL   poll seconds (default 120, floor 30)
  CORDLESS_IP              WP826 IP (default 192.168.1.71); '' disables cordless checks
  CORDLESS_PASSWORD        WP826 admin password; '' -> cordless API checks skipped
  CORDLESS_CERT_SHA256     SHA-256 fingerprint the WP826's certificate must match;
                           '' -> unverified (previous behaviour)
                           (reachability + registration still covered by rtpmon)
  GATEWAY_PORTS            comma ext range for the GXW ports (default '11,12,...,18')
  CORDLESS_BATTERY_CRIT    battery %% considered critical when discharging (default 15)
  CORDLESS_BATTERY_WARN    battery %% considered low when discharging (default 30)
  CORDLESS_WIFI_MIN        min acceptable Wi-Fi signal 0-5 (default 2)
  CORDLESS_MOS_MIN         min acceptable recent-call MOS (default 3.4)
  DEVICE_HEALTH_ALERTS     'false'/'0' -> publish sensors but never notify()
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import http.client
import json
import os
import socket
import ssl
import sys
import time

sys.path.insert(0, "/usr/share/switchboard/webui")  # ha_client lives with the webui

# --------------------------------------------------------------------------- #
# WP826 HTTP API client (pure-stdlib; mirrors tools/wp826.mjs).
# Auth: POST /cgi-bin/access {access:sha256(user)} -> salt; POST /cgi-bin/dologin
#   {username, password:sha256(pw+salt)} -> {sid}; sid + cookie gate the reads.
#   REQUIRES Referer/Origin headers or the phone 403s. TLS is the device's own
#   self-signed cert -> unverified context (LAN, no CA).
# --------------------------------------------------------------------------- #
_SHA = lambda s: hashlib.sha256(s.encode()).hexdigest()  # noqa: E731


def _ctx() -> ssl.SSLContext:
    # The handset serves its OWN self-signed certificate and offers no way to
    # install one signed by a CA, so chain/hostname validation cannot succeed.
    # Trust is established instead by pinning the certificate's fingerprint —
    # see _check_pin, which runs BEFORE any credential is sent.
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c


def cert_fingerprint(der: bytes) -> str:
    """Lower-case hex SHA-256 of a DER certificate (what we pin on)."""
    return hashlib.sha256(der).hexdigest()


def normalize_pin(pin: str) -> str:
    """Accept the fingerprint in any of the shapes a human might paste:
    colon- or space-separated, upper or lower case, optional 'sha256:' prefix."""
    p = str(pin or "").strip().lower()
    if p.startswith("sha256:"):
        p = p[7:]
    return "".join(ch for ch in p if ch in "0123456789abcdef")


def pin_matches(expected: str, actual_der: bytes) -> bool:
    """Constant-time compare of a configured pin against the presented cert.
    An EMPTY pin returns True: pinning is opt-in, and an install that has not
    set one keeps working exactly as before (documented in SECURITY.md)."""
    want = normalize_pin(expected)
    if not want:
        return True
    return hmac.compare_digest(want, cert_fingerprint(actual_der))


class _WP:
    def __init__(self, ip: str, password: str, user: str = "admin", timeout: float = 6.0,
                 cert_pin: str = ""):
        self.ip, self.pw, self.user, self.timeout = ip, password, user, timeout
        self.cert_pin = cert_pin
        self.cookies: dict[str, str] = {}
        self.sid = None

    def _req(self, method: str, path: str, body: str | None = None):
        conn = http.client.HTTPSConnection(self.ip, 443, timeout=self.timeout, context=_ctx())
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"https://{self.ip}/",
            "Origin": f"https://{self.ip}",
        }
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            # Establish TLS first and check the pin BEFORE writing the request:
            # the login body carries the admin password, so it must never reach
            # a peer whose certificate we did not expect.
            conn.connect()
            if self.cert_pin:
                der = conn.sock.getpeercert(True) if conn.sock else None
                if der is None or not pin_matches(self.cert_pin, der):
                    got = cert_fingerprint(der) if der else "(none)"
                    raise ssl.SSLCertVerificationError(
                        f"WP826 certificate does not match cordless_cert_sha256 "
                        f"(got {got[:16]}...); refusing to send credentials")
            conn.request(method, "/cgi-bin" + path, body, headers)
            resp = conn.getresponse()
            for k, v in resp.getheaders():
                if k.lower() == "set-cookie":
                    kv = v.split(";", 1)[0]
                    if "=" in kv:
                        ck, cvv = kv.split("=", 1)
                        self.cookies[ck.strip()] = cvv.strip()
            data = resp.read().decode(errors="replace")
            return resp.status, data
        finally:
            conn.close()

    def login(self) -> bool:
        try:
            st, a = self._req("POST", "/access", f"access={_SHA(self.user)}")
            salt = (json.loads(a) or {}).get("body")
            if not salt:
                return False
            st, d = self._req("POST", "/dologin", f"username={self.user}&password={_SHA(self.pw + salt)}")
            dj = json.loads(d)
            if dj.get("response") != "success":
                return False
            self.sid = dj["body"]["sid"]
            return True
        except Exception:
            return False

    def get(self, path: str):
        sep = "&" if "?" in path else "?"
        try:
            st, d = self._req("GET", f"{path}{sep}sid={self.sid}")
            return json.loads(d) if st == 200 else None
        except Exception:
            return None


def probe_cordless(ip: str, password: str, cert_pin: str = "",
                   cordless_ext: str = "") -> dict:
    """Return a raw device-health snapshot for the WP826, best-effort. Keys:
    reachable(bool: TCP:443 open), api_ok(bool: logged in + read), and — when api_ok —
    battery_pct/charging/battery_health, wifi_connected/wifi_signal/wifi_ssid,
    last_mos/last_mos_age_s/last_mos_ledger_tx, and rtp_judged (every scored
    record, see judge_rtp_records). `cordless_ext` lets the matcher tell the
    handset's own ledger legs from other phones'."""
    out = {"reachable": _tcp_open(ip, 443), "api_ok": False}
    if not password:
        return out
    wp = _WP(ip, password, cert_pin=cert_pin)
    if not wp.login():
        return out
    out["api_ok"] = True
    bat = wp.get("/api-get_battery_status") or {}
    b = bat.get("battery") or {}
    if b:
        out["battery_pct"] = _int(b.get("capacity"))
        out["charging"] = str(b.get("status", "")).lower() == "charging"
        out["battery_health"] = b.get("health")
    wifi = (wp.get("/api-wifi_status_get") or {}).get("status") or {}
    if wifi:
        out["wifi_connected"] = bool(wifi.get("connected"))
        out["wifi_signal"] = _int(wifi.get("signal"))
        out["wifi_ssid"] = (wifi.get("connection") or {}).get("ssid")
    # `or {}` alone is not enough: it catches None/"" but NOT a non-empty
    # string, and the handset does sometimes answer with rtpStatus as a plain
    # string. That reached last_call_mos().values() (the reader
    # judge_rtp_records has since replaced) and raised
    # "'str' object has no attribute 'values'", aborting the WHOLE cordless poll
    # cycle (seen live 2026-08-03) — so battery/Wi-Fi went unpublished until the
    # next cycle. Accept only a mapping.
    _rtp_raw = (wp.get("/api-get_rtp_status") or {}).get("rtpStatus")
    rtp = _rtp_raw if isinstance(_rtp_raw, dict) else {}
    # Ledger-gated: an RTP record may drive health only when its NEAREST ledger
    # leg is a real dialplan call — HA "announce" playback legs leave low-MOS
    # records that are not calls — and even then a low score counts only if that
    # leg's own transmit figures agree (see judge_rtp_records). The extension
    # decides whose leg that is: another phone's figures never judge this one.
    judged = judge_rtp_records(rtp, load_callqos_legs(), cordless_ext)
    out["rtp_judged"] = judged           # every scored record, for capture_low_mos
    last = newest_call(judged, now=time.time())
    if last is not None:
        out["last_mos"] = last["mos"]    # the phone's own conversational MOS for its leg
        if last["age_s"] is not None:
            out["last_mos_age_s"] = last["age_s"]  # seconds since that call ended (for a recency gate)
        # What the PBX measured on that SAME leg, in the direction the score is about.
        out["last_mos_ledger_tx"] = last["ledger_tx"]
    return out


# A ledger leg's `ts` (hangup epoch, PBX clock) and the phone's stopTimeSecond
# (handset clock) describe the same hangup; 90 s absorbs their skew plus the
# ledger's write latency without letting a neighbouring call match instead.
CALLQOS_MATCH_WINDOW_S = 90

# How far apart the two clocks put the SAME hangup, in practice. The three
# 2026-09-14 'degraded' notices, read as notice time minus their "Ns ago", place
# the handset's stopTimeSecond at most 0.7, 0.8 and 1.8 s after the ledger `ts`
# of its leg (both clocks count whole seconds, and the notice prints after the
# probe). Two legs whose distances from one record differ by no more than this
# cannot be told apart by time, so the cordless's OWN leg wins (judge_rtp_records).
# 5 s is the largest measured offset plus rounding on both clocks plus margin.
CALLQOS_CLOCK_SLOP_S = 5


# Legs the PBX originates to play something AT a phone. Mirrors PLAYBACK_TAGS in
# switchboard-callqos; kept as a literal because devhealth is a separate process
# that must not import from /usr/bin.
PLAYBACK_TAGS = frozenset({"wakeup-deliver", "page", "announce"})

# The ledger fields a handset score is judged against, plus the ones the capture
# keeps beside it for studying the score. `*_tx` is PBX -> handset: the audio the
# handset's own moscq is about, measured from the handset's OWN RTCP receiver
# reports. Everything else here is kept for the record, not for judging.
LEG_FIELDS = ("ts", "tag", "ext", "dur", "rxcount", "txcount",
              "loss_rx_pct", "loss_tx_pct", "jitter_rx_last_ms", "jitter_tx_last_ms",
              "mes_rx", "mes_tx", "rtt_ms", "rtt_max_ms", "rtt_samples", "quality")


def load_callqos_legs(path: str | None = None, max_bytes: int = 65536) -> list[dict]:
    """Recent call-ledger legs — EVERY leg, playback included — for matching the
    phone's RTP records to the leg each one describes (see judge_rtp_records).
    Each leg is LEG_FIELDS with `ts` a float and `tag` a string. Reads only the
    file's tail; the partial first line a mid-file seek can produce is dropped by
    the malformed-line skip. Missing/unreadable ledger -> [] (nothing can be
    confirmed).

    ★ THE LEDGER IS NEITHER APPEND-ONLY NOR UNBOUNDED, whatever this said until
    v0.103.1. switchboard-callqos caps it at MAX_RECORDS = 300 legs and enforces
    that by rewriting the WHOLE file and os.replace()-ing it into position on
    every write. Live on 2026-09-15 the file was sitting at exactly 300 rows and
    rolling, its oldest 2026-07-27. Two things follow, and both matter here:

      * the tail read is still correct, but it is not an optimisation over an
        ever-growing file — the whole ledger is about 300 lines, and max_bytes is
        what decides how much of that we look at;
      * the file is REPLACED, not extended, so its inode changes under any reader
        holding it open. Opening by path per call (as this does) is the only safe
        way to read it, and any future "seek where we left off" scheme would
        quietly read a file that no longer exists.

    A reader who believed the old comment would also believe the ledger holds
    every leg the system has ever recorded. It holds 300, which at this house's
    call volume reached back to 2026-07-27 — about seven weeks — and that window
    shortens as the phones get busier. Any analysis run against this file can
    only speak for whatever window the newest 300 legs happen to cover.

    ★ PLAYBACK LEGS ARE KEPT, AND TAGGED, ON PURPOSE (2026-09-14). v0.57.0
    dropped them here, so the matcher only ever saw the legs that remained — and
    it accepted a handset record within 90 s of ANY of them. A playback leg that
    ended beside a real call was therefore confirmed BY THAT CALL. The live
    shape: a wakeup-deliver leg hung up at 13:00:19Z and an operator leg twelve
    seconds later at 13:00:31Z, so the handset's score for the delivery could
    ride in on the operator leg. Removing the playback leg removed the only
    thing that could tell the two apart. Matching now keeps it, finds the
    NEAREST leg, and skips the record when that leg is playback."""
    p = path or os.environ.get("SWITCHBOARD_CALLQOS") or "/data/state/callqos.jsonl"
    try:
        with open(p, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except OSError:
        return []
    out: list[dict] = []
    for line in data.splitlines():
        # AttributeError: a syntactically-valid line that isn't an object
        # (e.g. a bare number) has no .get — skipped like any malformed line.
        try:
            rec = json.loads(line)
            ts = float(rec.get("ts"))
        except (AttributeError, TypeError, ValueError):
            continue
        leg = {k: rec.get(k) for k in LEG_FIELDS}
        leg["ts"] = ts
        leg["tag"] = str(rec.get("tag") or "")
        out.append(leg)
    return out


# ★★ WHEN A LOW HANDSET SCORE COUNTS (2026-09-14).
#
# The WP826 scored moscq 2.2 on three legs in eleven hours — an assistant leg at
# 02:51:58Z and two dial-42 wake-up legs at 12:42:31Z and 13:10:37Z — and each
# raised a 'degraded' episode on the alarm handset. For those same legs the PBX
# ledger, reading the handset's OWN RTCP receiver reports, recorded
# loss_tx_pct 0.0, mes_tx 87.9-88.0 and jitter_tx 2.4-6.5 ms. Same-shaped wake-up
# legs that morning scored 4.4. WHY the handset says 2.2 is NOT known: an earlier
# theory (a playback leg) does not cover these, and a tempting `rxmes/40` rule
# was refuted in August. Nothing here claims a mechanism. What it does is refuse
# to raise an alert that NO measurement of the same leg supports, and capture
# every low score (capture_low_mos) so the mechanism can be studied instead of
# guessed.
#
# The thresholds come from callqos, checked against the ledger. MES 78 is
# classify()'s floor for "good". 1.0 % is NOT one of classify()'s cut-offs (it
# labels at 0.5, 1.5 and 4 % and alerts above 3 %); it is the bound classify()'s
# own docstring gives for loss that is inaudible on G.711. Over the 190 non-playback
# legs from 2026-07-18 to 2026-09-14, the lowest credible mes_tx is 83.4 (78.5
# across all 267 credible legs, playback included), and exactly ONE leg reached
# 1 % transmit loss (1.339 %, a wake-up leg on 2026-08-25). So:
#   - loss >= 1.0 % is where G.711 loss stops being inaudible, and it would have
#     corroborated that one leg and no other;
#   - mes < 78 is below callqos's floor for "good" and below every credible
#     transmit MES the ledger has ever recorded.
# A handset score under mos_min (3.4, roughly MES 68) caused by the network or
# by jitter would have to show in the handset's own receiver reports well before
# either line. When it does not, the low score is published as
# last_mos_uncorroborated and does not degrade the sensor.
CORROBORATE_LOSS_TX_PCT = 1.0
CORROBORATE_MES_TX = 78.0


def _leg_num(v):
    """A ledger number, or None. A bool is not a measurement."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def ledger_tx_judgement(leg: dict | None) -> str:
    """What the PBX measured on a leg in the direction a handset score is about:
    'impaired', 'clean' or 'unmeasured'.

    Loss at or above CORROBORATE_LOSS_TX_PCT is positive evidence whatever the
    MES says. Otherwise the MES decides — but Asterisk writes mes 0.0 for a
    direction it could not score (no RTCP round completed), and a missing field
    is an older record, so neither can vouch for a clean leg: 'unmeasured'."""
    if not isinstance(leg, dict):
        return "unmeasured"
    loss, mes = _leg_num(leg.get("loss_tx_pct")), _leg_num(leg.get("mes_tx"))
    if loss is not None and loss >= CORROBORATE_LOSS_TX_PCT:
        return "impaired"
    if mes is None or mes <= 0:
        return "unmeasured"
    return "impaired" if mes < CORROBORATE_MES_TX else "clean"


def leg_carries_no_measurement(leg: dict | None) -> bool:
    """True for a ledger leg that CANNOT corroborate anything, ever.

    ★ A MISDIAL MUST NOT GET TO JUDGE THE CORDLESS (2026-09-15). A leg that was
    never connected still lands in the ledger, and the matcher below was happy to
    pick one as the nearest leg to a handset RTP record. When it did, the verdict
    read 'unmeasured' and a genuinely degraded score was quietly dropped —
    suppressed by a call that never happened.

    Live shape: ext 19 dialled an invalid room three times on 2026-09-15
    (12:50:54Z, 13:11:49Z, 13:11:52Z). Each was written `quality: "unreachable"`,
    `hcause: 34`, `billsec: 0`, `mes_tx: null` — Congestion() rejected the dial,
    no channel was ever created, no RTP flowed. There is nothing in such a record
    for `ledger_tx_judgement` to read and there never will be.

    The test is DELIBERATELY NARROW: the leg must both be unable to have carried
    audio (zero duration, or `unreachable`) AND have no transmit figure. An
    ordinary answered call that Asterisk simply could not score — the 13:00:31Z
    operator leg, `mes_tx: 0.0` on 18 seconds of real audio — is a leg, is still
    matched, and still reads 'unmeasured'. That is a measurement that came back
    empty, which is a different fact from a call that never took place, and the
    matcher must not conflate the two."""
    if not isinstance(leg, dict):
        return True
    dur = _leg_num(leg.get("dur"))
    zero_length = dur is not None and dur <= 0
    unreachable = str(leg.get("quality") or "").strip() == "unreachable"
    if not (zero_length or unreachable):
        return False
    return ledger_tx_judgement(leg) == "unmeasured"


def _nearest_leg(ts: int, legs: list[dict]) -> dict:
    """The leg that hung up nearest `ts`. The key is (distance, is-a-call):
    False sorts first, so an exact tie goes to the playback leg."""
    return min(legs, key=lambda lg: (abs(ts - lg["ts"]), lg["tag"] not in PLAYBACK_TAGS))


def judge_rtp_records(rtp_status: dict, ledger: list[dict] | None,
                      cordless_ext: str = "") -> list[dict]:
    """Every retained handset RTP record that carries a real MOS, each matched to
    the ledger leg it describes. One dict per record:

      key, record   the rtpStatus entry as the handset sent it
      mos, stop_ts  its moscq and stopTimeSecond
      match         'call' (the matched leg is a dialplan call), 'playback'
                    (the nearest leg, or the cordless's own leg preferred over
                    it, is a playback leg: skipped), or 'unmatched' (no leg
                    within CALLQOS_MATCH_WINDOW_S: skipped)
      leg           the matched leg (LEG_FIELDS), or None
      match_rule    'own-ext' (the leg is the cordless's own), 'nearest' (another
                    extension's leg, or no cordless_ext to tell), None (no leg)
      ledger_tx     ledger_tx_judgement(leg); 'unmeasured' for another
                    extension's leg

    HA "announce" playback to the handset leaves phone-side RTP records with low
    moscq (2.2-2.9 observed) — three false 'degraded' episodes fired
    2026-08-05/06 — so a record may drive health only when its NEAREST leg is a
    call. Nearest, not merely near: see load_callqos_legs for the playback leg a
    neighbouring call used to confirm. An exact tie goes to the playback leg,
    because when the ledger cannot say which leg a score belongs to the MOS is
    SKIPPED: a false 'degraded' costs alert trust, and genuinely poor real calls
    already notify separately via the callqos path.

    ★ WHOSE LEG IT IS (2026-09-14 review). A leg's `ext` is the channel that ran
    the context: the cordless for everything it dials and for the wake-up
    deliveries, announcements and HA pages played AT it, but the CALLER for a
    call made to it, and the DIALLING phone for a page dialled from a handset
    (that one Page()s every room from the dialler's own channel). Matching on
    time alone let another phone's leg that hung up a second nearer judge the
    cordless's score with THAT phone's transmit figures — clearing a real
    problem or corroborating a phantom one.
    So, when `cordless_ext` is known:
      - the cordless's own leg is preferred unless another leg hung up more than
        CALLQOS_CLOCK_SLOP_S nearer. Beyond that the own leg is an earlier call,
        and the record belongs to the nearer one (a call made to the cordless);
      - a record is still skipped when the NEAREST leg of any extension is
        playback — a dialled page is logged under the dialler, so preferring the
        own leg must not reopen the playback gate — and when the own leg it
        prefers is;
      - another extension's leg never judges the score: its `*_tx` figures are
        about a different phone, so it reads 'unmeasured' and cannot degrade.
    Not seen live: no leg of another extension hung up within 90 s of a cordless
    leg anywhere in the 2026-07-18..09-14 ledger. `cordless_ext=""` matches on
    time alone and judges whatever leg that finds, as before.

    ★ AND A LEG THAT NEVER CONNECTED IS NOT A CANDIDATE (2026-09-15). A
    zero-duration misdial is in the ledger like anything else, and being nearest
    used to be enough to make it the judge — after which the verdict read
    'unmeasured' and a real degrade was suppressed by a call that never happened.
    Such legs are filtered out before the nearest is chosen, unless every
    candidate is one. See leg_carries_no_measurement().

    `ledger=None` means no gating (every record is a 'call' with no leg); `[]`
    means the ledger was readable and empty, so nothing matches."""
    out: list[dict] = []
    ext = str(cordless_ext or "").strip()
    # Defence in depth beside the caller's isinstance check: the handset can
    # answer with rtpStatus as a plain STRING, and `or {}` does not catch a
    # non-empty one. Reaching .values() with a str raised
    # "'str' object has no attribute 'values'" and killed the whole poll cycle.
    if not isinstance(rtp_status, dict):
        return out
    for key, rec in rtp_status.items():
        if not isinstance(rec, dict):
            continue
        try:
            m = float(rec.get("moscq"))
        except (TypeError, ValueError):
            continue
        # The WP826 emits moscq 0.0 as a NO-MEASUREMENT sentinel — the real MOS
        # scale floors at 1.0 — and one reached a live alert as "MOS 0.0"
        # (2026-08-05). Below-scale is not a measurement: the record is ignored
        # as a candidate (an older valid record may still win), exactly like an
        # unparseable moscq.
        if m < 1.0:
            continue
        try:
            ts = int(rec.get("stopTimeSecond"))
        except (TypeError, ValueError):
            ts = 0
        leg, match = None, "call"
        if ledger is not None:
            near = [lg for lg in ledger if abs(ts - lg["ts"]) <= CALLQOS_MATCH_WINDOW_S]
            if not near:
                match = "unmatched"
            else:
                # A leg that carried no call cannot judge one. Drop the misdials
                # (see leg_carries_no_measurement) BEFORE anything picks a
                # nearest, so they cannot win on distance and cannot come back
                # through the own-extension preference below — the live misdials
                # were the cordless's OWN dials, so filtering only at the end
                # would have left them in. Playback legs are never dropped here,
                # whatever they carry: their job is to SKIP the record, and this
                # filter must not reopen that gate. When every candidate is a
                # misdial there is nothing better to pick, so they are all kept
                # and the record reads 'unmeasured' exactly as before.
                usable = [lg for lg in near if lg["tag"] in PLAYBACK_TAGS
                          or not leg_carries_no_measurement(lg)]
                near = usable or near
                leg = _nearest_leg(ts, near)
                own = [lg for lg in near
                       if ext and str(lg.get("ext") or "").strip() == ext
                       and abs(ts - lg["ts"]) <= abs(ts - leg["ts"]) + CALLQOS_CLOCK_SLOP_S]
                # A nearest playback leg skips the record whoever it was logged under.
                if leg["tag"] not in PLAYBACK_TAGS and own:
                    leg = _nearest_leg(ts, own)
                match = "playback" if leg["tag"] in PLAYBACK_TAGS else "call"
        rule, tx = None, ledger_tx_judgement(leg)
        if leg is not None:
            if ext and str(leg.get("ext") or "").strip() == ext:
                rule = "own-ext"
            else:
                rule = "nearest"
                if ext:
                    tx = "unmeasured"     # another phone's transmit figures, not this one's
        out.append({"key": str(key), "record": rec, "mos": m, "stop_ts": ts,
                    "match": match, "leg": leg, "match_rule": rule, "ledger_tx": tx})
    return out


def newest_call(judged: list[dict], now: float | None = None) -> dict | None:
    """The MOST RECENT 'call' among judged records — NOT the min across history
    (an old bad call must not pin the sensor 'degraded' forever). Picked by the
    latest stopTimeSecond; a copy with `age_s`, the seconds since that call ended
    (None if `now` is not given or the record has no timestamp). None when no
    record matched a call. moscq is the phone's own conversational MOS for its
    leg — the callee-side quality Asterisk cannot measure."""
    best = None
    for j in judged or []:
        if j.get("match") != "call":
            continue
        if best is None or j["stop_ts"] > best["stop_ts"]:
            best = j
    if best is None:
        return None
    age = int(now - best["stop_ts"]) if (now is not None and best["stop_ts"]) else None
    return {**best, "age_s": age}


def mos_verdict(judged: dict) -> str:
    """One word for how a scored record was treated. 'unmatched' and 'playback'
    were skipped outright. A 'call' is 'corroborated' (its leg's transmit side was
    impaired: a low score counts toward degraded), 'uncorroborated' (the ledger
    measured that direction clean) or 'unmeasured' (no credible figure for this
    handset: Asterisk could not score it, or the leg is another phone's)."""
    if judged.get("match") != "call":
        return str(judged.get("match") or "unmatched")
    return {"impaired": "corroborated",
            "clean": "uncorroborated"}.get(judged.get("ledger_tx"), "unmeasured")


# --------------------------------------------------------------------------- #
# Low-MOS capture. PRIVATE: /data/state is add-on-only (container shell blocked,
# backups encrypted, add-on API 403). The handset's record can name the far end
# and carry LAN ports, so this is never mirrored to /share.
# --------------------------------------------------------------------------- #
CAPTURE_MAX_BYTES = 512 * 1024
_CAPTURE_TAIL_BYTES = 65536
# Dedupe keys already on disk, per path. The handset keeps a record for hours
# and is polled every two minutes, so without this one call would be written
# ~100 times. Keyed on the path so a test (or a moved file) reloads cleanly.
_capture_seen: dict = {"path": None, "keys": set()}


def _capture_path() -> str:
    return os.environ.get("SWITCHBOARD_CORDLESS_MOS_LOG") or "/data/state/cordless-mos.jsonl"


def _capture_key(j: dict) -> str:
    """One row per distinct JUDGEMENT of a record. The verdict is part of the
    key: a record first seen before its ledger leg was written ('unmatched')
    and judged again once it was is two findings, and both are worth keeping."""
    rec = j.get("record") if isinstance(j.get("record"), dict) else {}
    return "|".join(str(x) for x in (j.get("stop_ts"), rec.get("startTimeSecond"),
                                     j.get("mos"), mos_verdict(j)))


def _load_capture_keys(path: str) -> set:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - _CAPTURE_TAIL_BYTES))
            data = f.read()
    except OSError:
        return set()
    keys = set()
    for line in data.splitlines():
        try:
            keys.add(str(json.loads(line)["dedupe"]))
        except (KeyError, TypeError, ValueError):
            continue
    return keys


def _clip(v):
    """A handset value as a bounded JSON scalar — a misbehaving handset must not
    be able to bloat the ledger or break the write."""
    if v is None or isinstance(v, (bool, int, float)):
        return v
    return str(v)[:256]


def capture_low_mos(judged: list[dict], mos_min: float, now: float | None = None,
                    path: str | None = None) -> int:
    """Append every NOT-YET-CAPTURED handset record scoring below `mos_min` to a
    private, capped JSONL ledger, whatever happened to it — skipped as playback,
    unmatched, corroborated or not. Returns the number of rows written.

    This exists because the 2.2 on the alarm handset has had two explanations:
    one (`rxmes/40`) was refuted, and the other (playback legs) does not cover
    the legs that raised the 2026-09-14 alerts. Each row keeps the whole
    rtpStatus record as received, the
    ledger leg it matched (ts, tag and the quality fields), the offset between
    the two clocks and the verdict — so the 2.2 legs can be set beside the 4.4
    legs of the same shape and the mechanism read off the data. Best-effort: a
    failed write is reported and retried next cycle, and never stops the poll."""
    p = path or _capture_path()
    if _capture_seen["path"] != p:
        _capture_seen["path"] = p
        _capture_seen["keys"] = _load_capture_keys(p)
    seen = _capture_seen["keys"]
    now = time.time() if now is None else now
    rows = []
    for j in judged or []:
        try:
            if not float(j.get("mos")) < float(mos_min):
                continue
        except (TypeError, ValueError):
            continue
        key = _capture_key(j)
        if key in seen or any(k == key for k, _ in rows):
            continue
        rec = j.get("record") if isinstance(j.get("record"), dict) else {}
        leg = j.get("leg") if isinstance(j.get("leg"), dict) else None
        stop = j.get("stop_ts") or 0
        rows.append((key, {
            "ts": _now_iso(),
            "verdict": mos_verdict(j),
            "moscq": j.get("mos"),
            "mos_min": mos_min,
            "stop_ts": stop,
            "age_s": int(now - stop) if stop else None,
            "record_key": j.get("key"),
            "record": {str(k)[:64]: _clip(v) for k, v in list(rec.items())[:64]},
            "leg": {k: leg.get(k) for k in LEG_FIELDS} if leg else None,
            # Why THAT leg: the cordless's own, or the nearest of any extension.
            "match_rule": j.get("match_rule"),
            # handset stop minus ledger hangup: the two clocks' disagreement.
            "leg_offset_s": round(stop - leg["ts"], 1) if leg else None,
            "dedupe": key,
        }))
    if not rows:
        return 0
    try:
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        _rotate_tail(p, CAPTURE_MAX_BYTES)
        with open(p, "a", encoding="utf-8") as fh:
            for _key, row in rows:
                fh.write(json.dumps(row, separators=(",", ":")) + "\n")
        # Root writes, nobody else reads. Tighter than linkhealth.jsonl's 0640:
        # nothing running as `asterisk` needs this file, and a handset record
        # can carry the far end's name or number.
        os.chmod(p, 0o600)
    except (OSError, TypeError, ValueError) as e:
        print(f"[devhealth] low-MOS capture not written ({e}); retrying next cycle", flush=True)
        return 0
    for key, row in rows:
        seen.add(key)
        print(f"[devhealth] captured handset MOS {row['moscq']:.1f} "
              f"({row['verdict']}) to {p}", flush=True)
    return len(rows)


def _rotate_tail(path: str, max_bytes: int, keep_frac: float = 0.5) -> None:
    """Trim an append-only ledger to its newest records. Best-effort.

    A copy of rtpmon/poller.py's helper (see its docstring for the v0.77.0
    zero-byte truncation it replaced); tests/test_ledger_rotation.py runs every
    copy over one input and requires identical bytes, so the copies cannot drift.
    """
    try:
        if os.path.getsize(path) <= max_bytes:
            return
        keep = max(1, int(max_bytes * keep_frac))
        with open(path, "rb") as fh:
            fh.seek(-keep, os.SEEK_END)
            tail = fh.read()
        # Everything before the first newline is half a record. Drop it: a
        # reader must never have to guess whether the first line is complete.
        nl = tail.find(b"\n")
        tail = tail[nl + 1:] if nl != -1 else b""
        with open(path, "wb") as fh:
            fh.write(tail)
    except OSError:
        pass                              # a trim must never break the write


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _tcp_open(ip: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# Pure classification (unit-tested — no I/O).
# --------------------------------------------------------------------------- #
def classify_cordless(snap: dict, th: dict) -> tuple[str, list[str]]:
    """(level, reasons) for the cordless. level ∈ {'ok','degraded','critical'}.
    CRITICAL = the alarm endpoint is offline, or its battery is about to die
    (discharging under the critical %%). DEGRADED = a quality/robustness risk that
    doesn't yet threaten delivery (weak/lost Wi-Fi, low-but-charging battery,
    recent poor call MOS, or API unreadable while the device still answers TCP)."""
    reasons: list[str] = []
    if not snap.get("reachable") and not snap.get("api_ok"):
        return "critical", ["cordless unreachable (alarm/announce endpoint offline)"]

    bp, charging = snap.get("battery_pct"), snap.get("charging")
    if bp is not None and not charging:
        if bp <= th["battery_crit"]:
            reasons.append(f"battery {bp}% and discharging (below {th['battery_crit']}% — endpoint will drop)")
            level = "critical"
        elif bp <= th["battery_warn"]:
            reasons.append(f"battery {bp}% and discharging (low)")
        # else fine

    if snap.get("api_ok"):
        if snap.get("wifi_connected") is False:
            reasons.append("Wi-Fi disconnected")
        elif snap.get("wifi_signal") is not None and snap["wifi_signal"] < th["wifi_min"]:
            reasons.append(f"Wi-Fi signal weak ({snap['wifi_signal']}/5)")
        # Call quality: only flag when the phone's LAST call was BOTH poor AND recent
        # (within mos_window). The phone retains a few historical RTP records, so a bad
        # call hours ago must not pin this sensor 'degraded' — and callqos already owns
        # per-call alerting; here it's a supporting, current-state signal only.
        # ...and only when the PBX's own measurement of that leg agrees. Three
        # 'degraded' episodes on 2026-09-14 rested on a handset 2.2 for legs the
        # ledger scored 0 % loss / MES 88 from the handset's own receiver
        # reports (see CORROBORATE_*). A missing judgement counts as no support.
        m, age = snap.get("last_mos"), snap.get("last_mos_age_s")
        if (m is not None and m < th["mos_min"] and age is not None
                and age <= th["mos_window"]
                and snap.get("last_mos_ledger_tx") == "impaired"):
            reasons.append(f"last call quality poor (MOS {m:.1f}, {age}s ago)")
    elif snap.get("reachable"):
        reasons.append("cordless answers on the network but its admin API is unreadable "
                       "(wrong cordless_password, or a cordless_cert_sha256 mismatch)")

    if any("battery" in r and "will drop" in r for r in reasons):
        return "critical", reasons
    return ("degraded", reasons) if reasons else ("ok", [])


# After THIS add-on restarts, Asterisk drops every registration and the GXW
# re-REGISTERs its ports on its own timer. During that window "all ports down" is
# the expected state, not an outage, so the all-down CRITICAL is held back until
# it passes. Anything still down afterwards alerts normally, and a PARTIAL outage
# is never suppressed. Without this, every add-on restart fired "the GXW gateway
# likely lost power or its uplink" (observed repeatedly on 2026-08-03) — an alarm
# that names the wrong component and trains the reader to ignore it.
#
# ★ THE "~4.5 MINUTES" THIS COMMENT USED TO CLAIM IS REFUTED. Measured off the
# timestamped forensic log on 2026-08-30: "Asterisk Ready." 20:31:27 -> 8/8
# Reachable 20:32:17 = 50 SECONDS (contacts at +1, +2, +2, +9, +12, +13, +23,
# +47, +50 s). A second boot bounds it at <= 67 s. The old figure was folklore
# and it mattered: it made a 50-second delivery-loss window look like a
# 4.5-minute one, and it would have hidden a genuinely slow re-registration
# inside "normal".
#
# The VALUE deliberately stays at 360 s anyway. The measurement is n=2, and the
# asymmetry is stark: too long merely delays a CRITICAL for a gateway that is
# already down, while too short resurrects the false-alarm storm this grace was
# written to stop. 360 s is now a documented 5.4x margin over the slowest
# observed convergence, not a guess. The right long-term fix is to END the grace
# early once all ports have been seen up at least once, which removes the blind
# window entirely instead of tuning it.
GATEWAY_STARTUP_GRACE_S = 360


def classify_gateway(down_exts: list[str], gw_exts: list[str],
                     uptime_s: float | None = None) -> tuple[str, list[str]]:
    """(level, reasons) for the GXW, DERIVED from which of its FXS-port extensions
    are currently down per rtpmon's rollup. All ports down = the gateway itself
    dropped (critical); some down = degraded (a handset unplugged or a partial
    fault); none = ok.

    `uptime_s` is this service's own age. Inside GATEWAY_STARTUP_GRACE_S an
    all-down reading is reported as 'degraded' (still visible, still published)
    rather than a critical "the gateway lost power" claim."""
    if not gw_exts:
        return "ok", []
    down = [e for e in gw_exts if e in set(down_exts or [])]
    if not down:
        return "ok", []
    if len(down) >= len(gw_exts):
        if uptime_s is not None and uptime_s < GATEWAY_STARTUP_GRACE_S:
            return "degraded", [
                f"all {len(gw_exts)} gateway ports still unregistered "
                f"{int(uptime_s)}s after start — normal while the GXW re-registers"]
        return "critical", [f"all {len(gw_exts)} gateway ports unregistered — the GXW gateway likely lost power or its uplink"]
    return "degraded", [f"{len(down)} of {len(gw_exts)} gateway ports down (exts {', '.join(down)})"]


_RANK = {"ok": 0, "degraded": 1, "critical": 2}


def health_transition(level: str, st: dict, min_cycles: int = 2,
                      fresh_evidence: bool = True) -> str:
    """One-shot, hysteretic device-alert state machine (mirrors rtpmon.outage_transition
    but for a 3-level single device). `st` carries {'cycles','level','alerted'}.
    Returns an event to notify on:
      'critical' / 'degraded' — fire once, after `min_cycles` consecutive unhealthy
        cycles at a level AT OR ABOVE the last alerted one (a worsening re-alerts);
      'recovered' — fire once when it returns to ok after having alerted AND a
        genuinely new measurement cleared it;
      'stale-clear' — same transition, but nothing was re-measured: the alert
        lapsed only because its evidence aged out of the judging window;
      '' — nothing.

    The recovered/stale-clear split matters more than it looks. classify_cordless
    flags a poor call only while it is INSIDE mos_window, so the level returns to
    'ok' on its own once the bad call is old enough — with nothing new observed.
    Reporting that as "recovered — back to normal" is an affirmative claim about
    the present made on no present evidence, and an operator who sees a recovery
    notification reasonably believes something was re-checked. Live on
    2026-09-01, a "cordless degraded: last call quality poor" was followed
    immediately by "cordless recovered: recovered" with no call in between.
    Silence would have been more honest; saying which of the two happened is
    better still.
    The consecutive-cycle gate rejects a single flaky poll (one dropped Wi-Fi frame,
    a transient API timeout)."""
    if level == "ok":
        st["cycles"] = 0
        st["level"] = "ok"
        if st.get("alerted"):
            st["alerted"] = None
            return "recovered" if fresh_evidence else "stale-clear"
        return ""
    # unhealthy (degraded/critical)
    if level == st.get("level"):
        st["cycles"] = st.get("cycles", 0) + 1
    else:
        st["cycles"] = 1
        st["level"] = level
    if st["cycles"] < min_cycles:
        return ""
    prev = st.get("alerted")
    # fire when we haven't alerted yet, or the situation escalated above what we alerted
    if prev is None or _RANK[level] > _RANK[prev]:
        st["alerted"] = level
        return level
    return ""


# --------------------------------------------------------------------------- #
# I/O helpers.
# --------------------------------------------------------------------------- #
def rollup_is_stale(attrs: dict, now: float | None = None,
                    tolerance: float = 2.5) -> bool:
    """True when rtpmon's rollup is too old to derive gateway health from.

    A pushed Home Assistant sensor never expires: if rtpmon dies (or its AMI
    wedges) while HA stays up, `sensor.switchboard_link_health` keeps its last
    good attributes forever. Deriving gateway health from that frozen snapshot
    reports whatever the fleet looked like when the poller stopped — green if it
    was green, and permanently 'degraded' if it stopped mid-restart, which is
    how a transient warm-up snapshot became a 4-minute false 'degraded' twice on
    2026-08-11.

    Staleness is judged against the rollup's OWN advertised poll interval
    (`poll_interval_s`), so changing link_health_interval cannot silently break
    this. A rollup with no timestamp is treated as fresh: an older rtpmon did
    not stamp it, and refusing to work with it would be a regression rather
    than a safety net."""
    stamped = attrs.get("measured_at")
    if not stamped:
        return False
    try:
        when = datetime.datetime.fromisoformat(str(stamped))
    except (TypeError, ValueError):
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    try:
        interval = float(attrs.get("poll_interval_s") or 300)
    except (TypeError, ValueError):
        interval = 300.0
    now_ts = now if now is not None else datetime.datetime.now(
        datetime.timezone.utc).timestamp()
    return (now_ts - when.timestamp()) > max(60.0, interval * tolerance)


def gateway_down_exts_from_rollup() -> list[str] | None:
    """Read rtpmon's rollup sensor for the currently-down extensions (the reliable,
    already-gathered registration signal). None if unavailable (HA down / rtpmon
    off) OR if the rollup is stale — a frozen snapshot is worse than no reading,
    because the caller republishes it as current gateway health."""
    try:
        import ha_client
        s = ha_client.get_state("sensor.switchboard_link_health")
    except Exception:
        return None
    if not s:
        return None
    a = s.get("attributes", {}) if isinstance(s, dict) else {}
    if rollup_is_stale(a):
        sys.stderr.write("[switchboard-devhealth] link-health rollup is stale "
                         f"(measured_at={a.get('measured_at')}); skipping gateway "
                         "health this cycle\n")
        return None
    exts = list(a.get("unreachable_exts", []) or []) + list(a.get("offline_exts", []) or [])
    return [str(e) for e in dict.fromkeys(exts)]  # de-dup, stringify


def resolve_cordless_ip(cordless_ext: str, fallback_ip: str) -> str:
    """The cordless's CURRENT IP, taken from its live SIP registration so a
    DHCP-moved handset is auto-followed without editing cordless_ip. rtpmon
    publishes the registered contact IP as ``contact_ip`` on
    sensor.switchboard_link_<ext>; use it when present, otherwise fall back to the
    configured static IP (also covers: no cordless_ext set, HA down, rtpmon off,
    or the cordless de-registered)."""
    if not cordless_ext:
        return fallback_ip
    try:
        import ha_client
        s = ha_client.get_state(f"sensor.switchboard_link_{cordless_ext}")
    except Exception:
        return fallback_ip
    ip = (s.get("attributes", {}) or {}).get("contact_ip") if isinstance(s, dict) else None
    return str(ip).strip() if ip else fallback_ip


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _poll_interval() -> int:
    """This poller's own cadence — the same floor run() applies."""
    return max(30, _env_int("DEVICE_HEALTH_INTERVAL", 120))


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _freshness() -> dict:
    """Freshness stamp for every sensor this poller pushes.

    A pushed Home Assistant sensor never expires. rollup_is_stale() above closes
    the case where ONE poller dies while another survives to notice and say
    "unknown" out loud — but nothing inside the add-on can close the case where
    the ADD-ON ITSELF is gone: no code is left running to publish anything, so
    every sensor simply freezes at its last value and keeps asserting it.

    That is not hypothetical. Through the 60-minute whole-host outage of
    2026-08-25 the PBX did not exist, yet `cordless_health` read "ok" and
    `trunk_health` read "Registered" for the entire hour — no 'unavailable', no
    'unknown', no gap marker. Anything reading those sensors during the outage
    got positive confirmation that a dead phone system was healthy.

    Only a consumer OUTSIDE the add-on can detect that, by comparing this stamp
    against now. `link_health` already carried it; the categorical health
    sensors — the ones a human or an automation actually trusts — did not.
    """
    return {"measured_at": _now_iso(), "poll_interval_s": _poll_interval()}


def _thresholds() -> dict:
    return {
        "battery_crit": _env_int("CORDLESS_BATTERY_CRIT", 15),
        "battery_warn": _env_int("CORDLESS_BATTERY_WARN", 30),
        "wifi_min": _env_int("CORDLESS_WIFI_MIN", 2),
        "mos_min": float(os.environ.get("CORDLESS_MOS_MIN", "3.4") or 3.4),
        "mos_window": _env_int("CORDLESS_MOS_WINDOW_S", 900),  # only a call within 15 min counts
    }


def _publish_cordless(level: str, reasons: list[str], snap: dict,
                      th: dict | None = None) -> None:
    try:
        import ha_client
    except Exception:
        return
    attrs = {
        "friendly_name": "Cordless health",
        "icon": "mdi:phone-in-talk" if level == "ok" else ("mdi:phone-alert" if level == "critical" else "mdi:phone-cog"),
        "reasons": reasons,
        "reachable": bool(snap.get("reachable") or snap.get("api_ok")),
        # kept even though it now equals the state: consumers keyed on
        # attributes.health while the state was the battery %.
        "health": level,
    }
    # last_mos_age_s was measured every cycle and never published: on
    # 2026-09-14 the sensor read 'ok' beside last_mos 2.2 with nothing to say
    # whether that call was two minutes or two hours old.
    for k in ("battery_pct", "charging", "battery_health", "wifi_connected", "wifi_signal",
              "wifi_ssid", "last_mos", "last_mos_age_s"):
        if snap.get(k) is not None:
            attrs[k] = snap[k]
    # A low score the ledger does not support is SHOWN, not hidden — it simply
    # does not degrade the state (classify_cordless). Always a bool beside
    # last_mos, so "false" is a statement rather than a missing key.
    if th is not None and snap.get("last_mos") is not None:
        attrs["last_mos_uncorroborated"] = bool(
            snap["last_mos"] < th["mos_min"]
            and snap.get("last_mos_ledger_tx") != "impaired")
    # The state is ALWAYS the level word. It used to be the battery % whenever
    # the battery read succeeded, which made a battery-driven 'critical'
    # invisible in the state itself (live 2026-08-03: battery 3% discharging
    # still showed state '3'; 'critical' only appeared once the handset died
    # and the read failed). The % remains as the battery_pct attribute; no
    # unit_of_measurement, since the state is no longer numeric.
    attrs.update(_freshness())
    ha_client.set_state("sensor.switchboard_cordless_health", level, attrs)


def _publish_gateway(level: str, reasons: list[str], down: list[str], gw_exts: list[str]) -> None:
    try:
        import ha_client
    except Exception:
        return
    ha_client.set_state("sensor.switchboard_gateway_health", level, {
        "friendly_name": "GXW gateway health",
        "icon": "mdi:router-network" if level == "ok" else "mdi:router-network-wireless",
        "ports_total": len(gw_exts),
        "ports_up": len(gw_exts) - len([e for e in gw_exts if e in set(down or [])]),
        "down_exts": [e for e in gw_exts if e in set(down or [])],
        "reasons": reasons,
        "health": level,
        **_freshness(),
    })


def _publish_gateway_unknown(reason: str, gw_exts: list[str]) -> None:
    """Publish gateway health as an explicit ``unknown``.

    A pushed Home Assistant sensor never expires, so simply NOT publishing keeps
    the last value on the dashboard — green stays green while the monitor has
    gone blind. v0.51.0 added the staleness check but then just skipped the
    cycle, which is the same fail-open shape the check was written to close.
    Saying "unknown" out loud is the whole point."""
    try:
        import ha_client
    except Exception:
        return
    ha_client.set_state("sensor.switchboard_gateway_health", "unknown", {
        "friendly_name": "GXW gateway health",
        "icon": "mdi:router-network-off",
        "ports_total": len(gw_exts),
        "reasons": [reason],
        "health": "unknown",
        **_freshness(),
    })


def _notify(device: str, event: str, reasons: list[str]) -> None:
    if os.environ.get("DEVICE_HEALTH_ALERTS", "true").lower() in ("false", "0", "no"):
        return
    try:
        import ha_client
    except Exception:
        return
    nid = f"switchboard_{device}_health"
    label = "Cordless" if device == "cordless" else "GXW gateway"
    if event == "recovered":
        ha_client.notify(f"{label} recovered — back to normal.",
                         title=f"Switchboard: {label.lower()} OK", notification_id=nid)
    elif event == "stale-clear":
        # NOT "back to normal" -- nothing was re-measured. Say what actually
        # happened so the reader does not infer a fresh all-clear.
        ha_client.notify(
            f"The {label.lower()} alert cleared because its evidence aged out — "
            "nothing new was measured. Treat the current state as unknown until "
            "the next call.",
            title=f"Switchboard: {label.lower()} alert lapsed", notification_id=nid)
    else:
        why = "; ".join(reasons) or event
        title = f"Switchboard: {label.lower()} {'CRITICAL' if event == 'critical' else 'degraded'}"
        ha_client.notify(f"{label} {event}: {why}", title=title, notification_id=nid)


def run() -> None:
    interval = max(30, _env_int("DEVICE_HEALTH_INTERVAL", 120))
    cordless_ip = os.environ.get("CORDLESS_IP", "192.168.1.71").strip()
    cordless_ext = os.environ.get("CORDLESS_EXT", "").strip()
    # This service's own start, used to hold back the gateway all-down CRITICAL
    # while the GXW is merely re-registering after our restart (see
    # GATEWAY_STARTUP_GRACE_S). monotonic: immune to wall-clock/NTP steps.
    _started = time.monotonic()
    cordless_pw = os.environ.get("CORDLESS_PASSWORD", "")
    cordless_cert_pin = os.environ.get("CORDLESS_CERT_SHA256", "")
    if cordless_pw and not normalize_pin(cordless_cert_pin):
        print("[devhealth] NOTE: cordless_cert_sha256 is unset — the WP826's "
              "self-signed certificate is not verified. Set it (see DOCS §8) so "
              "the admin password cannot be captured by a LAN impostor.", flush=True)
    gw_exts = [e.strip() for e in os.environ.get("GATEWAY_PORTS", "11,12,13,14,15,16,17,18").split(",") if e.strip()]
    th = _thresholds()
    cst: dict = {}
    gst: dict = {}
    last_ip = None
    follow = f"auto-follow ext {cordless_ext}" if cordless_ext else "static"
    print(f"[devhealth] up: cordless={cordless_ip or '(disabled)'} ({follow}) "
          f"api={'yes' if cordless_pw else 'no-password'} "
          f"gateway_ports={','.join(gw_exts)} every {interval}s", flush=True)
    while True:
        # --- cordless (IP auto-followed from its live SIP registration) ---
        probe_ip = resolve_cordless_ip(cordless_ext, cordless_ip)
        if probe_ip != last_ip:
            if last_ip is not None:
                print(f"[devhealth] cordless IP now {probe_ip} (was {last_ip}) — following DHCP", flush=True)
            last_ip = probe_ip
        if probe_ip:
            try:
                snap = probe_cordless(probe_ip, cordless_pw, cordless_cert_pin, cordless_ext)
                level, reasons = classify_cordless(snap, th)
                _publish_cordless(level, reasons, snap, th)
                # Every low handset score goes on the record, judged or skipped.
                # Its own guard: a capture failure must never cost the alert
                # state machine below its cycle.
                try:
                    capture_low_mos(snap.get("rtp_judged") or [], th["mos_min"])
                except Exception as e:
                    print(f"[devhealth] low-MOS capture error: {e}", flush=True)
                # Identify the call the MOS verdict rests on by its END time, so
                # a clear can be attributed. age_s grows for the SAME call every
                # cycle, so the difference tells apart "a newer call was measured"
                # from "the same old call finally aged out of mos_window".
                _age = snap.get("last_mos_age_s")
                call_end = (time.time() - _age) if isinstance(_age, (int, float)) else None
                prev_end = cst.get("mos_call_end")
                fresh = (call_end is not None
                         and (prev_end is None or call_end > prev_end + 1))
                if level != "ok" and call_end is not None:
                    cst["mos_call_end"] = call_end
                ev = health_transition(level, cst, fresh_evidence=fresh)
                if ev:
                    if ev == "stale-clear":
                        cst.pop("mos_call_end", None)
                    print(f"[devhealth] cordless {ev}: {'; '.join(reasons) or ev}", flush=True)
                    _notify("cordless", ev, reasons)
            except Exception as e:
                print(f"[devhealth] cordless poll error: {e}", flush=True)
        # --- gateway (derived from rtpmon rollup) ---
        try:
            down = gateway_down_exts_from_rollup()
            if down is not None:
                level, reasons = classify_gateway(down, gw_exts, time.monotonic() - _started)
                _publish_gateway(level, reasons, down, gw_exts)
                ev = health_transition(level, gst)
                if ev:
                    print(f"[devhealth] gateway {ev}: {'; '.join(reasons) or ev}", flush=True)
                    _notify("gateway", ev, reasons)
            else:
                # No usable rollup (link-health poller down, stale, or HA
                # unreadable). Publish the blindness instead of leaving the last
                # reading standing — and reset the transition state so recovery
                # re-announces cleanly rather than comparing against a level we
                # can no longer vouch for.
                _publish_gateway_unknown(
                    "link-health rollup unavailable or stale — gateway health "
                    "cannot be derived", gw_exts)
                gst["level"] = None
                gst["cycles"] = 0
        except Exception as e:
            print(f"[devhealth] gateway poll error: {e}", flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    run()
