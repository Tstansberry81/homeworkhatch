"""Lock the public schema down for Supabase.

Supabase exposes the `public` schema through its auto-generated Data API (PostgREST), and
the publishable/anon key is public by design. This app never uses that API — it talks to
Postgres directly — so every app table gets Row Level Security with no policies (the API
sees nothing) and the anon/authenticated roles lose their grants. The app connects as the
table owner, which bypasses RLS, so it is unaffected. Runs after every `flask db upgrade`
(see migrations/env.py), which also covers tables added by future migrations. It is a
no-op on databases without Supabase's roles, and safe to run repeatedly.
"""

LOCKDOWN_SQL = """
DO $$
DECLARE t record;
BEGIN
  FOR t IN SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tableowner = current_user LOOP
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t.tablename);
  END LOOP;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon')
     AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
    REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon, authenticated;
    REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM anon, authenticated;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM anon, authenticated;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM anon, authenticated;
  END IF;
END $$;
"""


def lock_down_public_schema(connection) -> None:
    if connection.dialect.name != "postgresql":
        return
    from sqlalchemy import text

    connection.execute(text(LOCKDOWN_SQL))
