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

This project ingests user-provided CSV/JSON/Parquet/Excel files and
calls an external LLM API as part of its reasoning layer. Areas worth
particular scrutiny:

- Anything that could lead to unintended code execution when parsing
  untrusted input files
- Prompt injection via dataset contents that could cause the AI
  reasoning layer to behave outside its intended scope
- Mishandling of API credentials (e.g. the Anthropic API key)

## Data sent to the Claude API

`--explain` sends cell values from mismatched rows to the Claude API
so it can write a plain-English explanation. By default, values are
passed through a redaction layer (`reasoning/redaction.py`) before
this happens: common structured sensitive patterns — email addresses,
US Social Security Numbers, credit card numbers, US phone numbers —
are masked with a `[REDACTED:category]` placeholder before they ever
leave your machine.

**Be precise about what this is and isn't.** This is pattern-based
detection of *structurally recognizable* sensitive data — it is **not**
a general PII detector. It will not recognize that a name, a home
address in a free-text field, or a non-US identifier format is
sensitive. If your data contains sensitive information that doesn't
match one of the patterns above, it will be sent to the API unredacted
unless you take other precautions (e.g. running `wherefore` without
`--explain`, or pre-sanitizing the file yourself).

Redaction can be disabled with `--no-redact` for cases where you've
already vetted your data — it is on by default specifically so that
running `--explain` for the first time doesn't require knowing this
flag exists.

If you find a real sensitive-data pattern that should be added to the
redaction layer, or a false positive that incorrectly masks legitimate
data, please report it the same way as any other vulnerability above.

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
