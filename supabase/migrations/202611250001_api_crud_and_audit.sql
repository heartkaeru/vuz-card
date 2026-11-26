-- Provenance labels for API edits and DELETE access for audited child CRUD.
ALTER TABLE public.vuz_sources DROP CONSTRAINT IF EXISTS vuz_sources_source_kind_check;
ALTER TABLE public.vuz_sources ADD CONSTRAINT vuz_sources_source_kind_check
  CHECK(source_kind IN ('seed','mock','json','document','web','manual','ai'));

-- Contacts are card fields too: give them a record version and field-level audit.
ALTER TABLE public.vuz_official_contacts
  ADD COLUMN created_at timestamptz NOT NULL DEFAULT now(),
  ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now(),
  ADD COLUMN created_by uuid REFERENCES public.vuz_users(id),
  ADD COLUMN updated_by uuid REFERENCES public.vuz_users(id),
  ADD COLUMN version bigint NOT NULL DEFAULT 1 CHECK(version>0);
CREATE TRIGGER vuz_version BEFORE INSERT OR UPDATE ON public.vuz_official_contacts
  FOR EACH ROW EXECUTE FUNCTION public.vuz_version_row();
CREATE TRIGGER vuz_audit AFTER INSERT OR UPDATE OR DELETE ON public.vuz_official_contacts
  FOR EACH ROW EXECUTE FUNCTION public.vuz_audit_row();

-- Preserve and audit approved AI facts that do not yet have normalized columns.
ALTER TABLE public.vuz_legacy_fields
  ADD COLUMN created_at timestamptz NOT NULL DEFAULT now(),
  ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now(),
  ADD COLUMN created_by uuid REFERENCES public.vuz_users(id),
  ADD COLUMN updated_by uuid REFERENCES public.vuz_users(id),
  ADD COLUMN version bigint NOT NULL DEFAULT 1 CHECK(version>0);
CREATE TRIGGER vuz_version BEFORE INSERT OR UPDATE ON public.vuz_legacy_fields
  FOR EACH ROW EXECUTE FUNCTION public.vuz_version_row();
CREATE TRIGGER vuz_audit AFTER INSERT OR UPDATE OR DELETE ON public.vuz_legacy_fields
  FOR EACH ROW EXECUTE FUNCTION public.vuz_audit_row();

DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='service_role') THEN
  GRANT DELETE ON public.vuz_officials,public.vuz_official_contacts,public.vuz_university_metrics TO service_role;
 END IF;
END $$;
