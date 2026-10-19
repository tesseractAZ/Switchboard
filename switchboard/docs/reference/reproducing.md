# Reproducing on new hardware

Everything needed to rebuild Switchboard lives in this repository, so replacing
the Home Assistant host (a new Raspberry Pi, a restored image, a different
machine) is straightforward.

## Rebuild the add-on from GitHub

1. Install **Home Assistant OS** on the new host.
2. **Settings → Add-ons → Add-on Store → ⋮ → Repositories**, add
   `https://github.com/tesseractAZ/Switchboard`.
3. Install **Switchboard**. The add-on builds from source in the repo (Asterisk,
   whisper.cpp, espeak-ng, everything) — no external image to host.
4. To pin an exact prior version, check out that release's tag first: versions
   are tagged (`v0.1.0` … the current release) and published as a **GitHub
   Release** with the changelog, and each release since v0.81.0 carries a
   generated `.docx` + `.pdf` of this manual. `git checkout vX.Y.Z` gives you the
   precise source that built that release. One exception: **v0.42.1** has a
   changelog entry but no tag and no release — tagging was a manual step until
   v0.46.1, and that version was missed. Its source is merge commit `8fcb453`.

## Restore your configuration (the one thing not in the repo)

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

## The gateway and cordless are separate devices

The **GXW4216 gateway** and the **WP826 cordless** are independent hardware on
your LAN — swapping the Home Assistant host does **not** touch them, so their
settings survive. You only need to reconfigure them if you replace *those*
devices, in which case:

- **GXW** — re-apply Profile-1 and the per-port SIP credentials via the SSH shell
  ([§7.3](#73-configuring-the-gateway-from-the-cli-reliable-path)); the reference
  dial plan and `P85 = 3` send delay are in [§7.4](#74-dial-plan--the-send-delay).
- **WP826** — re-register it as its extension and, for the phonebook / distinctive
  ring / ringtone / speed-dial extras, re-run the tools in
  [`tools/`](../tools/) (`wp826.mjs`, `wp826-pcodes.md`).

Point the rebuilt gateway/cordless at the new host's IP as the SIP server and the
phones re-register — the add-on regenerates all of Asterisk's config from your
options on start.
