# Publishing Switchboard as its own repository

This folder is a **complete, standalone Home Assistant add-on repository**. It
currently lives inside `GPTTesting/Switchboard/` only because the automation
session that built it could not create a new GitHub repo (the integration token
lacked repo-creation permission). Moving it to its own `Switchboard` repo is a
two-minute job.

## Option A — from your machine (recommended)

```bash
# 1. Create an empty repo on GitHub named "Switchboard" (no README/license).
#    https://github.com/new

# 2. From a clone of GPTTesting, copy this folder out and push it standalone:
git clone https://github.com/tesseractAZ/GPTTesting.git
cp -r GPTTesting/Switchboard switchboard-repo
cd switchboard-repo
git init -b main
git add .
git commit -m "Initial commit: Switchboard HA add-on"
git remote add origin https://github.com/tesseractAZ/Switchboard.git
git push -u origin main
```

## Option B — keep history with git subtree

```bash
git clone https://github.com/tesseractAZ/GPTTesting.git
cd GPTTesting
git subtree split --prefix=Switchboard -b switchboard-only
git push https://github.com/tesseractAZ/Switchboard.git switchboard-only:main
```

## After publishing

Add the repo to Home Assistant: **Settings → Add-ons → Add-on Store → ⋮ →
Repositories → `https://github.com/tesseractAZ/Switchboard`**, then install
**Switchboard**.

> If you re-run the automation with the new `Switchboard` repo in scope, it can
> push there directly and this folder can be deleted from `GPTTesting`.
