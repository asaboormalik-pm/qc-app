# Public Repository Publication Checklist

Use this checklist before making the repository or a release artifact public.

## Credentials and infrastructure

- [ ] Confirm `.env` and all `.env.*` files except `.env.example` are ignored and untracked.
- [ ] Scan tracked text and release artifacts for API keys, bearer tokens, passwords, private keys, customer data, internal hosts, IP addresses, paths, and network shares.
- [ ] Keep `.env.example` limited to placeholders and empty secret fields.
- [ ] Confirm CI copies only placeholder configuration into public artifacts.
- [ ] Store live configuration in an approved secret manager, protected deployment channel, or local ignored file.
- [ ] Rotate any credential or endpoint exposed before these controls were added.
- [ ] Assess Git history and existing GitHub release artifacts for historic exposure. If removal is required, coordinate history rewriting and force-push impact with repository owners; this change does not rewrite history.

## Product claims and artifacts

- [ ] Ensure README claims match the connector code, not companion systems that are absent from this repository.
- [ ] Use only approved, sanitized screenshots. Omit screenshots when none are available.
- [ ] Verify public documentation contains no customer names, production data, internal routes, or environment-specific filesystem paths.
- [ ] Review generated `build/` and `dist/` files before publishing; they are tracked in this repository and can preserve old configuration or compiled source strings.

## Release readiness

- [ ] Run the relevant unit tests and build on supported target platforms.
- [ ] Test an approved printer and completion callback in a non-production or controlled environment.
- [ ] Verify rollback media and owner contacts are available.
