-- Permanently remove one university and its university-owned records.
-- Shared source files and import batches are retained.
CREATE FUNCTION public.vuz_purge_university(target_university_id uuid)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $$
DECLARE
 legal_entity uuid;
 official_ids uuid[] := ARRAY[]::uuid[];
 meeting_ids uuid[] := ARRAY[]::uuid[];
 purge_ids uuid[] := ARRAY[]::uuid[];
BEGIN
 SELECT legal_entity_id INTO legal_entity
 FROM public.vuz_universities WHERE id=target_university_id FOR UPDATE;
 IF NOT FOUND THEN RETURN false; END IF;
 IF EXISTS (SELECT 1 FROM public.vuz_universities WHERE parent_id=target_university_id) THEN
   RAISE EXCEPTION 'university_has_children' USING ERRCODE='P0001';
 END IF;

 PERFORM set_config('vuz.actor_type','system',true);
 PERFORM set_config('vuz.actor_label','card-purge',true);
 purge_ids := ARRAY[target_university_id];

 SELECT COALESCE(array_agg(id),ARRAY[]::uuid[]) INTO official_ids
 FROM public.vuz_officials WHERE university_id=target_university_id;
 SELECT COALESCE(array_agg(id),ARRAY[]::uuid[]) INTO meeting_ids
 FROM public.vuz_meetings WHERE university_id=target_university_id;
 purge_ids := purge_ids || official_ids || meeting_ids;

 SELECT purge_ids || COALESCE(array_agg(id),ARRAY[]::uuid[]) INTO purge_ids
 FROM public.vuz_official_contacts WHERE official_id=ANY(official_ids);
 SELECT purge_ids || COALESCE(array_agg(id),ARRAY[]::uuid[]) INTO purge_ids
 FROM public.vuz_meeting_participants
 WHERE meeting_id=ANY(meeting_ids) OR official_id=ANY(official_ids);
 SELECT purge_ids || COALESCE(array_agg(id),ARRAY[]::uuid[]) INTO purge_ids
 FROM public.vuz_follow_ups WHERE meeting_id=ANY(meeting_ids);
 SELECT purge_ids || COALESCE(array_agg(id),ARRAY[]::uuid[]) INTO purge_ids
 FROM public.vuz_university_aliases WHERE university_id=target_university_id;
 SELECT purge_ids || COALESCE(array_agg(id),ARRAY[]::uuid[]) INTO purge_ids
 FROM public.vuz_university_metrics WHERE university_id=target_university_id;
 SELECT purge_ids || COALESCE(array_agg(id),ARRAY[]::uuid[]) INTO purge_ids
 FROM public.vuz_admission_campaigns WHERE university_id=target_university_id;
 SELECT purge_ids || COALESCE(array_agg(id),ARRAY[]::uuid[]) INTO purge_ids
 FROM public.vuz_infrastructure WHERE university_id=target_university_id;
 SELECT purge_ids || COALESCE(array_agg(id),ARRAY[]::uuid[]) INTO purge_ids
 FROM public.vuz_projects WHERE university_id=target_university_id;
 SELECT purge_ids || COALESCE(array_agg(id),ARRAY[]::uuid[]) INTO purge_ids
 FROM public.vuz_product_recommendations WHERE university_id=target_university_id;
 SELECT purge_ids || COALESCE(array_agg(id),ARRAY[]::uuid[]) INTO purge_ids
 FROM public.vuz_contracts WHERE university_id=target_university_id;
 SELECT purge_ids || COALESCE(array_agg(id),ARRAY[]::uuid[]) INTO purge_ids
 FROM public.vuz_import_records WHERE university_id=target_university_id;
 SELECT purge_ids || COALESCE(array_agg(id),ARRAY[]::uuid[]) INTO purge_ids
 FROM public.vuz_legacy_fields WHERE university_id=target_university_id;

 DELETE FROM public.vuz_official_contacts WHERE official_id=ANY(official_ids);
 DELETE FROM public.vuz_meeting_participants
   WHERE meeting_id=ANY(meeting_ids) OR official_id=ANY(official_ids);
 DELETE FROM public.vuz_follow_ups WHERE meeting_id=ANY(meeting_ids);
 DELETE FROM public.vuz_meetings WHERE id=ANY(meeting_ids);
 DELETE FROM public.vuz_officials WHERE id=ANY(official_ids);
 DELETE FROM public.vuz_university_aliases WHERE university_id=target_university_id;
 DELETE FROM public.vuz_university_metrics WHERE university_id=target_university_id;
 DELETE FROM public.vuz_admission_campaigns WHERE university_id=target_university_id;
 DELETE FROM public.vuz_infrastructure WHERE university_id=target_university_id;
 DELETE FROM public.vuz_projects WHERE university_id=target_university_id;
 DELETE FROM public.vuz_product_recommendations WHERE university_id=target_university_id;
 DELETE FROM public.vuz_contracts WHERE university_id=target_university_id;
 DELETE FROM public.vuz_import_records WHERE university_id=target_university_id;
 DELETE FROM public.vuz_legacy_fields WHERE university_id=target_university_id;
 DELETE FROM public.vuz_universities WHERE id=target_university_id;

 IF legal_entity IS NOT NULL
    AND NOT EXISTS (SELECT 1 FROM public.vuz_universities WHERE legal_entity_id=legal_entity) THEN
   purge_ids := purge_ids || ARRAY[legal_entity];
   DELETE FROM public.vuz_legal_entities WHERE id=legal_entity;
 END IF;

 ALTER TABLE public.vuz_audit_log DISABLE TRIGGER vuz_audit_immutable;
 DELETE FROM public.vuz_audit_log WHERE record_id=ANY(purge_ids);
 ALTER TABLE public.vuz_audit_log ENABLE TRIGGER vuz_audit_immutable;
 RETURN true;
END;
$$;

REVOKE ALL ON FUNCTION public.vuz_purge_university(uuid) FROM PUBLIC;
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='service_role') THEN
   EXECUTE 'GRANT EXECUTE ON FUNCTION public.vuz_purge_university(uuid) TO service_role';
 END IF;
END $$;
