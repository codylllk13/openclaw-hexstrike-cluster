# OpenClaw compute cluster

This repository contains the sanitized, reusable source for a two-machine
OpenClaw coding and compute queue. It provides bounded workers, isolated Git
snapshots, a local desktop console, and owner-only commands through an existing
Telegram bot.

Start with [the cluster guide](compute/README.md). The
[HexStrike runbook](compute/HEXSTRIKE.md) documents the isolated Kali agent,
ChatGPT subscription authentication, direct terminal launcher, and owner-only
`/hexstrike` Telegram command. Runtime credentials, account identifiers, network
addresses, machine inventory, source snapshots, job output, and private
configuration are intentionally excluded.
