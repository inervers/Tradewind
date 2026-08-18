# Security Policy

Tradewind is a local-first portfolio project. It processes API credentials, customer contacts,
email drafts, photos and diagnostic logs on the user's machine. These files are runtime data and
must not be committed to the repository or included in release archives.

## Reporting a problem

If you discover a vulnerability, open a GitHub Security Advisory or use GitHub's private
vulnerability reporting feature when available. Do not paste working credentials, customer data
or other private material into a public issue.

## Repository safeguards

- API keys are read from local configuration or environment variables.
- Runtime configuration, customer files, databases, photos, logs and build outputs are ignored.
- `detect-secrets` runs through pre-commit; TruffleHog runs in GitHub Actions.
- Demo records use synthetic identities and `example.com` addresses.
- Release packages are built from `packaging/default-data/`, not from the developer's runtime
  `data/` directory.

## If a credential is exposed

1. Revoke or rotate it immediately at the provider.
2. Review provider usage and billing logs.
3. Remove it from the current tree and Git history.
4. Force-push the cleaned history only after coordinating with collaborators.
5. Re-run both local and GitHub secret scans before publishing again.
