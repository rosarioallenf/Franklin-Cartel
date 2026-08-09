# Cartel

Golf group scoring, quotas and money for the 2026 Cartel.

The hosted copy keeps its data in Supabase. Set `CARTEL_DB_URL` in the
Streamlit Cloud app settings under **Secrets**:

```toml
CARTEL_DB_URL = "postgresql://..."
```

With no `CARTEL_DB_URL`, the app falls back to a local SQLite file — which is
how it runs on a laptop.

Nothing in this repository contains golf data. The database, the backups and
the generated reports are all excluded by `.gitignore`.
