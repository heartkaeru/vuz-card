-- Field-level audit, server-controlled versions and restricted Data API access.
CREATE FUNCTION public.vuz_actor_id() RETURNS uuid LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog AS $$
DECLARE candidate text; result uuid;
BEGIN
 candidate:=NULLIF(current_setting('vuz.actor_id',true),'');
 IF candidate IS NULL THEN
   candidate:=COALESCE(NULLIF(current_setting('request.jwt.claims',true),'')::jsonb->>'sub','');
   SELECT id INTO result FROM public.vuz_users WHERE auth_subject::text=candidate AND active;
 ELSE
   SELECT id INTO result FROM public.vuz_users WHERE id::text=candidate AND active;
 END IF;
 RETURN result;
END $$;
CREATE FUNCTION public.vuz_can_read() RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog AS $$
 SELECT EXISTS(SELECT 1 FROM public.vuz_users WHERE id=public.vuz_actor_id() AND active)
$$;
CREATE FUNCTION public.vuz_can_write() RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog AS $$
 SELECT EXISTS(SELECT 1 FROM public.vuz_users WHERE id=public.vuz_actor_id() AND active AND role IN ('editor','admin'))
$$;
CREATE FUNCTION public.vuz_version_row() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog AS $$
DECLARE actor uuid; expected text;
BEGIN
 actor:=public.vuz_actor_id();
 IF TG_OP='INSERT' THEN
  NEW.version:=1; NEW.created_by:=actor; NEW.updated_by:=actor;
  NEW.created_at:=clock_timestamp(); NEW.updated_at:=NEW.created_at;
 ELSE
  IF NEW.id IS DISTINCT FROM OLD.id THEN RAISE EXCEPTION 'record ID is immutable'; END IF;
  expected:=NULLIF(current_setting('vuz.expected_version',true),'');
  IF expected IS NOT NULL AND expected::bigint<>OLD.version THEN
    RAISE EXCEPTION 'stale record version' USING ERRCODE='40001';
  END IF;
  IF (to_jsonb(NEW)-ARRAY['version','updated_at','updated_by'])=(to_jsonb(OLD)-ARRAY['version','updated_at','updated_by']) THEN
    RETURN NULL;
  END IF;
  NEW.created_at:=OLD.created_at; NEW.created_by:=OLD.created_by;
  NEW.version:=OLD.version+1; NEW.updated_at:=clock_timestamp(); NEW.updated_by:=actor;
 END IF;
 RETURN NEW;
END $$;
CREATE FUNCTION public.vuz_audit_row() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog AS $$
DECLARE previous jsonb; current_value jsonb; field text; row_id uuid; revision bigint;
 kind text; label text; role_name text;
BEGIN
 previous:=CASE WHEN TG_OP='INSERT' THEN '{}'::jsonb ELSE to_jsonb(OLD) END;
 current_value:=CASE WHEN TG_OP='DELETE' THEN '{}'::jsonb ELSE to_jsonb(NEW) END;
 row_id:=COALESCE((current_value->>'id')::uuid,(previous->>'id')::uuid);
 revision:=CASE WHEN TG_OP='DELETE' THEN (previous->>'version')::bigint+1 ELSE (current_value->>'version')::bigint END;
 kind:=COALESCE(NULLIF(current_setting('vuz.actor_type',true),''),CASE WHEN public.vuz_actor_id() IS NULL THEN 'system' ELSE 'human' END);
 IF kind NOT IN ('human','ai','migration','system') THEN RAISE EXCEPTION 'invalid audit actor type'; END IF;
 label:=COALESCE(NULLIF(current_setting('vuz.actor_label',true),''),session_user::text);
 role_name:=COALESCE(NULLIF(NULLIF(current_setting('role',true),''),'none'),session_user::text);
 FOR field IN SELECT key FROM jsonb_object_keys(previous||current_value) AS keys(key) LOOP
  IF field<>ALL(ARRAY['created_at','updated_at','created_by','updated_by','version'])
    AND (previous->field) IS DISTINCT FROM (current_value->field) THEN
    INSERT INTO public.vuz_audit_log(table_name,record_id,operation,field_name,old_value,new_value,
      record_version,actor_id,actor_type,actor_label,database_role)
    VALUES(TG_TABLE_NAME,row_id,TG_OP,field,previous->field,current_value->field,
      revision,public.vuz_actor_id(),kind,label,role_name);
  END IF;
 END LOOP;
 RETURN CASE WHEN TG_OP='DELETE' THEN OLD ELSE NEW END;
END $$;
CREATE FUNCTION public.vuz_protect_audit() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog AS $$ BEGIN RAISE EXCEPTION 'audit log is append-only'; END $$;
CREATE TRIGGER vuz_audit_immutable BEFORE UPDATE OR DELETE ON public.vuz_audit_log
FOR EACH ROW EXECUTE FUNCTION public.vuz_protect_audit();
DO $$
DECLARE table_name text;
BEGIN
 FOREACH table_name IN ARRAY ARRAY['vuz_users','vuz_legal_entities','vuz_universities','vuz_university_aliases',
 'vuz_officials','vuz_official_contacts','vuz_university_metrics','vuz_admission_campaigns','vuz_infrastructure',
 'vuz_projects','vuz_bank_products','vuz_product_recommendations','vuz_contracts','vuz_meetings','vuz_meeting_participants','vuz_follow_ups'] LOOP
  EXECUTE format('ALTER TABLE public.%I ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now()',table_name);
  EXECUTE format('ALTER TABLE public.%I ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now(), ADD COLUMN created_by uuid REFERENCES public.vuz_users(id), ADD COLUMN updated_by uuid REFERENCES public.vuz_users(id), ADD COLUMN version bigint NOT NULL DEFAULT 1 CHECK(version>0)',table_name);
  EXECUTE format('CREATE TRIGGER vuz_version BEFORE INSERT OR UPDATE ON public.%I FOR EACH ROW EXECUTE FUNCTION public.vuz_version_row()',table_name);
  EXECUTE format('CREATE TRIGGER vuz_audit AFTER INSERT OR UPDATE OR DELETE ON public.%I FOR EACH ROW EXECUTE FUNCTION public.vuz_audit_row()',table_name);
 END LOOP;
END $$;
-- Parent validation on both insert and update; descendants must remain within the legal entity.
CREATE FUNCTION public.vuz_check_parent() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
 IF NEW.parent_id=NEW.id THEN RAISE EXCEPTION 'university parent cycle'; END IF;
 IF NEW.parent_id IS NOT NULL THEN
  IF NOT EXISTS(SELECT 1 FROM public.vuz_universities p WHERE p.id=NEW.parent_id
      AND p.legal_entity_id IS NOT DISTINCT FROM NEW.legal_entity_id) THEN RAISE EXCEPTION 'parent legal entity mismatch'; END IF;
  IF EXISTS(WITH RECURSIVE parents AS (
    SELECT id,parent_id FROM public.vuz_universities WHERE id=NEW.parent_id
    UNION SELECT u.id,u.parent_id FROM public.vuz_universities u JOIN parents p ON u.id=p.parent_id)
    SELECT 1 FROM parents WHERE id=NEW.id) THEN RAISE EXCEPTION 'university parent cycle'; END IF;
 END IF;
 IF EXISTS(SELECT 1 FROM public.vuz_universities c WHERE c.parent_id=NEW.id AND c.legal_entity_id IS DISTINCT FROM NEW.legal_entity_id)
 THEN RAISE EXCEPTION 'child legal entity mismatch'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER vuz_parent BEFORE INSERT OR UPDATE ON public.vuz_universities
FOR EACH ROW EXECUTE FUNCTION public.vuz_check_parent();
-- The Auth FK is added only in a real Supabase project; plain PostgreSQL stays usable for tests.
DO $$ BEGIN
 IF to_regclass('auth.users') IS NOT NULL THEN
  ALTER TABLE public.vuz_users ADD CONSTRAINT vuz_auth_user_fk FOREIGN KEY(auth_subject) REFERENCES auth.users(id) ON DELETE SET NULL;
 END IF;
END $$;
DO $$
DECLARE table_name text; role_name text;
BEGIN
 FOR table_name IN SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname='public' AND tablename LIKE 'vuz\_%' ESCAPE '\' LOOP
  EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY',table_name);
  EXECUTE format('REVOKE ALL ON public.%I FROM PUBLIC',table_name);
  IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='anon') THEN EXECUTE format('REVOKE ALL ON public.%I FROM anon',table_name); END IF;
  IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN
   EXECUTE format('REVOKE ALL ON public.%I FROM authenticated',table_name);
   EXECUTE format('GRANT SELECT ON public.%I TO authenticated',table_name);
   EXECUTE format('CREATE POLICY vuz_read ON public.%I FOR SELECT TO authenticated USING(public.vuz_can_read())',table_name);
   IF table_name IN ('vuz_universities','vuz_officials','vuz_official_contacts','vuz_university_metrics','vuz_admission_campaigns','vuz_infrastructure','vuz_projects','vuz_bank_products','vuz_product_recommendations','vuz_contracts','vuz_meetings') THEN
    EXECUTE format('GRANT INSERT,UPDATE ON public.%I TO authenticated',table_name);
    EXECUTE format('CREATE POLICY vuz_write ON public.%I FOR ALL TO authenticated USING(public.vuz_can_write()) WITH CHECK(public.vuz_can_write())',table_name);
   END IF;
  END IF;
  IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='service_role') THEN
   EXECUTE format('REVOKE ALL ON public.%I FROM service_role',table_name);
   IF table_name IN ('vuz_audit_log','vuz_users') THEN EXECUTE format('GRANT SELECT ON public.%I TO service_role',table_name);
   ELSE EXECUTE format('GRANT SELECT,INSERT,UPDATE ON public.%I TO service_role',table_name); END IF;
  END IF;
 END LOOP;
 FOREACH role_name IN ARRAY ARRAY['anon','authenticated','service_role'] LOOP
  IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname=role_name) THEN
   EXECUTE format('GRANT USAGE ON SCHEMA public TO %I',role_name);
  END IF;
 END LOOP;
END $$;
-- Audit functions are callable only by triggers. RLS helpers are read-only SECURITY DEFINER functions.
REVOKE ALL ON FUNCTION public.vuz_audit_row(),public.vuz_version_row(),public.vuz_protect_audit(),public.vuz_check_parent() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.vuz_actor_id(),public.vuz_can_read(),public.vuz_can_write() FROM PUBLIC;
DO $$ DECLARE role_name text; BEGIN
 FOREACH role_name IN ARRAY ARRAY['authenticated','service_role'] LOOP
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname=role_name) THEN
 EXECUTE format('GRANT EXECUTE ON FUNCTION public.vuz_actor_id(),public.vuz_can_read(),public.vuz_can_write() TO %I',role_name);
 END IF; END LOOP;
END $$;
