# Security Policy

## Reporting a vulnerability

If you find a security vulnerability in `wherefore`, please report it
privately rather than opening a public issue — this gives time to fix
it before details are public.

**How to report:** open a [GitHub Security Advisory](https://github.com/ArunMishra1/wherefore/security/advisories/new)
for this repository (preferred), or contact the maintainer directly via
GitHub.

Please include:
- A description of the vulnerability and its potential impact
- Steps to reproduce, or a minimal proof of concept
- Any relevant version/commit information

## What counts as a security concern here

This project ingests user-provided CSV/JSON files and calls an external
LLM API as part of its reasoning layer. Areas worth particular scrutiny:

- Anything that could lead to unintended code execution when parsing
  untrusted input files
- Prompt injection via dataset contents that could cause the AI
  reasoning layer to behave outside its intended scope
- Mishandling of API credentials (e.g. the Anthropic API key)

## Response expectations

This is an early-stage, primarily solo-maintained open-source project
(see project status in [`README.md`](./README.md)). There is currently
no dedicated security team and no guaranteed response time or bug
bounty program. Reports will be acknowledged and addressed on a
best-effort basis. This policy will be revisited as the project and its
contributor base grow.

## Supported versions

Pre-1.0: only the `main` branch is supported. There is no formal
version support matrix yet.
