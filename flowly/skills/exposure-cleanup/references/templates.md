# Draft templates

The helper renders drafts with `scripts/exposure_cleanup.py draft`. It supports:

- `--kind auto`: choose CCPA for `US-CA`, GDPR for `EU`/`UK`/`GB`, otherwise a
  generic opt-out.
- `--kind generic`: removal or suppression without making a jurisdictional legal
  claim.
- `--kind ccpa`: CCPA/CPRA deletion and opt-out. Use only for California
  residents.
- `--kind gdpr`: GDPR or UK-GDPR erasure. Use only for EU/UK/GB residents.
- `--kind indirect`: remove only the subject's own identifiers from someone
  else's associated record.

Drafts are not sent automatically. After the operator sends a draft, record the
submission and disclosure field names with:

```bash
python3 scripts/exposure_cleanup.py record <subject> <broker> submitted \
  --disclosed full_name --disclosed contact_email --channel email
```

Use `--listing <url>` unless a blind opt-out is explicitly appropriate and the
operator accepts the disclosure. Prefer verified listing URLs because they keep
requests narrow and auditable.
