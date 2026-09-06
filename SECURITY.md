# Security policy

**Report a vulnerability privately** through this repository's
[Security Advisories](https://github.com/tesseractAZ/Switchboard/security/advisories/new).
Please do not open a public issue for a security problem, and please do not send
details by email.

This file exists at the repository root because GitHub only detects a security
policy at the root, in `.github/`, or in `docs/` — a policy anywhere else is
invisible, and this project's was, so the "Report a vulnerability" button never
appeared.

The full model — what this add-on trusts, the toll-fraud defences, the LAN-local
risks it deliberately accepts, and what you must configure yourself — is in
**[switchboard/SECURITY.md](switchboard/SECURITY.md)**.

## Scope

This is a home telephone system. It runs on your own hardware, on your own
network, and it can place calls that cost money. The parts most worth reporting:

- Anything that lets an **outside caller** reach a feature code, a transfer, or
  the outbound trunk.
- Anything that exposes the operator console, the web UI, or the announce
  endpoint beyond what you configured.
- Anything that writes personal data — recordings, transcripts, telephone
  numbers — somewhere readable from outside the add-on container.

## What this system cannot do

**These phones cannot reach emergency services.** Dialling `911` is answered with
a spoken notice saying so. That is a deliberate limitation, not a vulnerability;
see [DOCS §9](switchboard/DOCS.md#9-adding-an-outside-line-sip-trunk).
