# Franklin Cartel

Golf group scoring, quotas and money.

Every file sits at the top level on purpose. GitHub's browser upload page
handles loose files reliably and folders unreliably, so this build has no
folders in it at all.

The hosted app keeps its data in Supabase. Set this in the Streamlit app's
Settings, under Secrets:

    CARTEL_DB_URL = "postgresql://..."

No golf data is in this repository. The database lives in Supabase.
