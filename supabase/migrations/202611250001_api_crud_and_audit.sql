-- Provenance labels for API edits and DELETE access for audited child CRUD.
ALTER TABLE public.vuz_sources DROP CONSTRAINT IF EXISTS vuz_sources_source_kind_check;
ALTER TABLE public.vuz_sources ADD CONSTRAINT vuz_sources_source_kind_check
  CHECK(source_kind IN ('seed','mock','json','document','web','manual','ai'));

DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='service_role') THEN
  GRANT DELETE ON public.vuz_officials,public.vuz_official_contacts,public.vuz_university_metrics TO service_role;
 END IF;
END $$;
