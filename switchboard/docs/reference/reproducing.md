# Reproducing on new hardware

Everything needed to rebuild Switchboard lives in this repository, so replacing
the Home Assistant host (a new Raspberry Pi, a restored image, a different
machine) is straightforward.

## Rebuild the add-on from GitHub

1. Install **Home Assistant OS** on the new host.
2. **Settings → Apps → Install app → ⋮ → Repositories**, add
   `https://github.com/tesseractAZ/Switchboard`. (Before Home Assistant 2026.2,
   apps were called add-ons: **Settings → Add-ons → Add-on Store → ⋮ →
   Repositories**.)
3. Install **Switchboard**. The add-on builds from source in the repo (Asterisk,
   whisper.cpp, espeak-ng, everything) — no external image to host.
4. To pin an exact prior version, check out that release's tag first: versions
   are tagged (`v0.1.0` … the current release) and published as a **GitHub
   Release** with the changelog, and each release after v0.81.0 carries a
   generated `.docx` + `.pdf` of this manual. `git checkout vX.Y.Z` gives you the
   precise source that built that release. Two exceptions, both from when tagging
   was a manual step (until v0.46.1): **v0.42.1** has a changelog entry but no
   tag and no release (its source is merge commit `8fcb453`), and **v0.8.2** has
   neither a tag nor a changelog entry (its source is commit `20a02fd`).

## Restore your configuration

Your **runtime configuration** — room names, SIP `secret`s, the trunk
credentials, the cordless address — is **not** in the (public) repository by
design; secrets don't belong in source control. It lives in the add-on's options
(`/data/options.json`) and is captured by Home Assistant's own backups. Two ways
to restore it:

- **Home Assistant backup (recommended).** A full or partial HA backup includes
  each add-on's configuration; restoring one on the new host repopulates
  Switchboard's options automatically. Take a fresh backup whenever you change the
  configuration.
- **Re-enter by hand.** It's a handful of fields in the **Configuration** tab
  (rooms + secrets, and the trunk block if you use an outside line).

Besides the options, some per-install state is not in the repository either:
custom prompt recordings in the add-on's config folder
(`/app_configs/<slug>/prompts`, since 0.120.0), `options-overlay.json` in that
folder if you use one, and the SSH console's host key in
`/data/ssh_host_ed25519_key`. Re-entering the options by hand restores none of
them: copy the config folder across as well, and expect SSH clients to warn that
the host key has changed (see [The operator console](operator-console.md)).

## The gateway and cordless are separate devices

The **GXW4216 gateway** and the **WP826 cordless** are independent hardware on
your LAN — swapping the Home Assistant host does **not** touch them, so their
settings survive. You only need to reconfigure them if you replace *those*
devices, in which case:

- **GXW** — re-apply Profile-1 and the per-port SIP credentials via the SSH shell
  ([Configuring the gateway from the CLI](gxw4216-gateway.md#73-configuring-the-gateway-from-the-cli-reliable-path)); the reference
  dial plan and `P85 = 3` send delay are in [Dial plan & the send delay](gxw4216-gateway.md#74-dial-plan--the-send-delay).
- **WP826** — re-register it as its extension and, for the phonebook / distinctive
  ring / ringtone / speed-dial extras, re-run the tools in
  [`tools/`](../../../tools/) (`wp826.mjs`, `wp826-pcodes.md`).

Point the rebuilt gateway/cordless at the new host's IP as the SIP server and the
phones re-register — the add-on regenerates the Asterisk configuration files it
manages from your options on start.
