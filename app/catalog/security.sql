-- Apply with the existing trusted database owner/migration role.
-- The FastAPI backend owns these tables or uses a privileged server role.
-- No direct anon/authenticated PostgREST access is needed by the frontend.
-- AIO3 catalog member policies remain separate and unchanged.
alter table public.users enable row level security;
alter table public.user_openai_keys enable row level security;
