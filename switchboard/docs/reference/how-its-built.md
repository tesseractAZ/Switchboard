# How it's built

- **Asterisk 20 + PJSIP** is the only telephony engine; `chan_sip` is not used.
- On every start, **`switchboard-config`** regenerates
  `/etc/asterisk/{pjsip,extensions,confbridge,modules,pjsip_notify,rtp,manager,logger,features}.conf`
  from your add-on options (`/data/options.json`). The options are the source of
  truth; hand edits are overwritten, and since 0.109.0 they are refused: only the
  generator, under its own AppArmor child profile, may write those files.
- **Offline voice.** Speech-to-text is **whisper.cpp** (English `base.en` model,
  ~142 MB), kept resident in RAM by a loopback-only server with a per-call
  `whisper-cli` fallback. Text-to-speech is **espeak-ng**. Both run on-box under
  the unprivileged `asterisk` user — no cloud, no API key.
- The **Ingress dashboard** is a small single-worker FastAPI app that reads live
  state over the loopback-only Asterisk Manager (AMI) socket.
- **Services** run under s6-overlay: a one-shot config generator, then Asterisk,
  the web UI, the console (the board, its SSH front end and the web terminal),
  the resident recognizer, the wake-up scheduler (whose 20-second loop also
  files the announcement verdicts and runs the announcement auto-retry), the two
  health pollers, and the Home Assistant broker: since 0.110.0 the programs that
  run as the `asterisk` user hold no Home Assistant token and reach Home
  Assistant only through it
  ([SECURITY.md](SECURITY.md#4-home-assistant-access)). Each optional service
  idles when its feature is turned off (except the link-health poller, which
  keeps scrubbing the readable log; §11), so `wakeup_enabled: false` also stops
  announcement verdicts and retries. The web UI reads the same switch: with
  wake-up calls off it treats the retry as off and never holds an identical
  repeat back for a replay that cannot run.
- Built on the Home Assistant **Alpine 3.21** base image (a two-stage build that
  compiles whisper.cpp from source), with `fastapi` / `uvicorn` / `jinja2` and
  best-effort `espeak-ng`, `ffmpeg`, and the ConfBridge/Page modules.
- Runs under an **AppArmor** profile and **host networking** (required for SIP +
  RTP on your LAN). Since 0.108.0 the profile allows only the capabilities the add-on
  uses (no raw sockets) and write-locks the shipped code for every process, and
  Asterisk runs as the unprivileged `asterisk` user with no capabilities. Since
  0.109.0 it also write-locks the Asterisk configuration, Asterisk's spool and the
  s6 init trees, and the config generator runs under a child profile of its own
  ([SECURITY.md](SECURITY.md#the-apparmor-profile-is-narrowed-not-a-per-path-sandbox)).
  Architectures: `amd64`, `aarch64`.

---
