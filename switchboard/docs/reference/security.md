# Security

The security model, the toll-fraud threat model, the console services and their
exposure, secret handling, and the short list of things
**you** must configure are documented in **[SECURITY.md](SECURITY.md)**. The
essentials:

- The Ingress dashboard is reachable only from the Home Assistant Supervisor.
- The Asterisk Manager socket is loopback-only. Since 0.109.0 it has two accounts,
  each with a fresh random secret every boot. Neither can run a program through
  the Manager, and neither holds Asterisk's CLI `command` class; SECURITY.md says
  what the two accounts do and do not separate.
- The trunk blocks international/premium prefixes and confines transfers to
  internal destinations.
- Inside the container, Asterisk runs as the unprivileged `asterisk` user with no
  capabilities, and the AppArmor profile refuses raw sockets and any write to the
  shipped code or the Asterisk configuration, root included.
- **Change the default room secrets** before your phones register.
- The SSH console listens on every interface by default (`console_bind`), and
  every login needs a `console_users` password: with none configured, every
  login is refused. The board itself has no network listener, only a root-only
  Unix socket. The standalone web terminal binds to `127.0.0.1` by default, and
  its sign-in is off until `console_users` has an entry — so configure one before
  you move its bind. The sidebar console (§10) needs neither.
- Two read-only GETs on port 8099 are deliberately reachable from the LAN
  without authentication. `/phonebook.xml` lets the cordless fetch its remote
  phonebook and exposes room names and internal extensions only
  ([§8](#8-the-wp826-wifi-cordless-optional)). `/announce/a<digits>.wav` is the
  speaker announcement clip a Home Assistant media player fetches; its name is
  random and it is deleted five to six minutes after it is written. Handset
  announcement clips are not served to the LAN (since v0.106.0).
  `POST /api/announce/{ext}` is accepted from the LAN only with a matching
  `X-Announce-Token`, and never while `announce_token` is blank.

---
