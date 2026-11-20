-- PostgreSQL / Supabase: authoritative cards and pending legacy facts.
-- All objects are prefixed to coexist with existing application schemas.
CREATE FUNCTION public.vuz_name_key(value text) RETURNS text
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
 SELECT regexp_replace(translate(lower(btrim(regexp_replace(value, '[[:space:] ]+', ' ', 'g'))),
 'АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯё', 'абвгдеежзийклмнопрстуфхцчшщъыьэюяе'), '[«»“”"„]', '', 'g')
$$;
CREATE FUNCTION public.vuz_valid_inn(value text) RETURNS boolean
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
 SELECT CASE WHEN value !~ '^[0-9]{10}$' OR value='0000000000' THEN false ELSE
 (SELECT (sum(substr(value,n::int,1)::int*w)%11)%10 FROM unnest(ARRAY[2,4,10,3,5,9,4,6,8]) WITH ORDINALITY t(w,n))=substr(value,10,1)::int END
$$;
CREATE TABLE public.vuz_users (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), auth_subject uuid UNIQUE,
 email text NOT NULL CHECK(btrim(email)<>''), display_name text NOT NULL,
 role text NOT NULL DEFAULT 'viewer' CHECK(role IN ('viewer','editor','admin')),
 active boolean NOT NULL DEFAULT true, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX vuz_user_email ON public.vuz_users(lower(email));
CREATE TABLE public.vuz_sources (
 id uuid PRIMARY KEY, source_path text NOT NULL, sha256 text NOT NULL CHECK(sha256 ~ '^[a-f0-9]{64}$'),
 source_kind text NOT NULL CHECK(source_kind IN ('seed','mock','json','document','web')),
 is_demo boolean NOT NULL DEFAULT true, imported_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(source_path,sha256)
);
CREATE TABLE public.vuz_import_batches (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), started_at timestamptz NOT NULL DEFAULT now(),
 finished_at timestamptz, status text NOT NULL CHECK(status IN ('running','completed')),
 importer_version text NOT NULL, summary jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE TABLE public.vuz_legal_entities (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), inn text NOT NULL UNIQUE CHECK(public.vuz_valid_inn(inn)),
 official_name text NOT NULL, ogrn text CHECK(ogrn IS NULL OR ogrn ~ '^[0-9]{13}$'),
 kpp text CHECK(kpp IS NULL OR kpp ~ '^[0-9]{9}$'), legal_address text,
 verified_at timestamptz NOT NULL, evidence_url text NOT NULL CHECK(btrim(evidence_url)<>'')
);
CREATE TABLE public.vuz_universities (
 id uuid PRIMARY KEY, name text NOT NULL CHECK(btrim(name)<>''),
 name_key text GENERATED ALWAYS AS (public.vuz_name_key(name)) STORED UNIQUE,
 short_name text, legal_entity_id uuid REFERENCES public.vuz_legal_entities(id) ON DELETE RESTRICT,
 parent_id uuid REFERENCES public.vuz_universities(id) ON DELETE RESTRICT,
 unit_code text NOT NULL DEFAULT 'head', institution_type text NOT NULL DEFAULT 'university'
 CHECK(institution_type IN ('university','spo','mixed','unknown')),
 region text, city text, address text, website text, photo_url text, profile text,
 reported_inn text, is_demo boolean NOT NULL DEFAULT true,
 verification_status text NOT NULL DEFAULT 'needs_review'
 CHECK(verification_status IN ('unverified','ai_draft','needs_review','verified','rejected')),
 CHECK(parent_id IS NULL OR parent_id<>id),
 CHECK(verification_status<>'verified' OR (legal_entity_id IS NOT NULL AND NOT is_demo))
);
CREATE UNIQUE INDEX vuz_legal_unit ON public.vuz_universities(legal_entity_id,unit_code) WHERE legal_entity_id IS NOT NULL;
CREATE INDEX vuz_university_region ON public.vuz_universities(region,city);
CREATE TABLE public.vuz_university_aliases (
 id uuid PRIMARY KEY, university_id uuid NOT NULL REFERENCES public.vuz_universities(id) ON DELETE RESTRICT,
 alias text NOT NULL, alias_key text GENERATED ALWAYS AS (public.vuz_name_key(alias)) STORED,
 source_id uuid NOT NULL REFERENCES public.vuz_sources(id), UNIQUE(source_id,alias_key,university_id)
);
CREATE TABLE public.vuz_officials (
 id uuid PRIMARY KEY, university_id uuid NOT NULL REFERENCES public.vuz_universities(id) ON DELETE RESTRICT,
 full_name text NOT NULL CHECK(btrim(full_name)<>''), position text NOT NULL CHECK(btrim(position)<>''),
 department text, responsibilities text, biography text, academic_degree text, photo_url text, source_url text,
 valid_from date, valid_to date, appointment_status text NOT NULL DEFAULT 'unknown'
 CHECK(appointment_status IN ('current','former','unknown')), is_acting boolean NOT NULL DEFAULT false,
 verification_status text NOT NULL DEFAULT 'needs_review'
 CHECK(verification_status IN ('unverified','ai_draft','needs_review','verified','rejected')),
 is_demo boolean NOT NULL DEFAULT true, source_id uuid NOT NULL REFERENCES public.vuz_sources(id),
 external_key text NOT NULL, UNIQUE(source_id,external_key),
 CHECK(valid_to IS NULL OR valid_from IS NULL OR valid_to>valid_from), CHECK(NOT(is_demo AND verification_status='verified'))
);
CREATE INDEX vuz_official_university ON public.vuz_officials(university_id);
CREATE TABLE public.vuz_official_contacts (
 id uuid PRIMARY KEY, official_id uuid NOT NULL REFERENCES public.vuz_officials(id) ON DELETE RESTRICT,
 channel_type text NOT NULL CHECK(channel_type IN ('email','phone')), value_raw text NOT NULL,
 value_normalized text NOT NULL CHECK(btrim(value_normalized)<>''), extension text NOT NULL DEFAULT '',
 verification_status text NOT NULL DEFAULT 'needs_review'
 CHECK(verification_status IN ('unverified','ai_draft','needs_review','verified','rejected')),
 source_id uuid NOT NULL REFERENCES public.vuz_sources(id),
 UNIQUE(official_id,channel_type,value_normalized,extension)
);
CREATE TABLE public.vuz_metric_definitions (
 code text PRIMARY KEY, label text NOT NULL, unit text NOT NULL,
 value_kind text NOT NULL CHECK(value_kind IN ('money','count','ratio','number'))
);
INSERT INTO public.vuz_metric_definitions VALUES
 ('payroll_fund','ФОТ','RUB','money'),('payroll_via_sber','Перечисления через Сбер','RUB','money'),
 ('employee_count','Численность сотрудников','person','count'),('student_count','Численность студентов','person','count'),
 ('budget_student_count','Бюджетные студенты','person','count'),('payroll_share','Доля Сбера','ratio','ratio'),
 ('active_contracts_reported','Заявленное число договоров','contract','count');
CREATE TABLE public.vuz_university_metrics (
 id uuid PRIMARY KEY, university_id uuid NOT NULL REFERENCES public.vuz_universities(id) ON DELETE RESTRICT,
 metric_code text NOT NULL REFERENCES public.vuz_metric_definitions(code), value numeric(24,6) NOT NULL CHECK(value>=0),
 unit text NOT NULL, period_start date, period_end date,
 period_kind text NOT NULL DEFAULT 'unknown' CHECK(period_kind IN ('month','quarter','year','as_of','unknown')),
 aggregation_kind text NOT NULL DEFAULT 'unknown' CHECK(aggregation_kind IN ('period_total','ytd','snapshot','average','unknown')),
 scope_code text NOT NULL DEFAULT 'unknown', methodology text NOT NULL DEFAULT 'unknown',
 scenario text NOT NULL DEFAULT 'reported' CHECK(scenario IN ('actual','plan','estimate','reported')),
 source_as_of timestamptz, raw_value text NOT NULL,
 verification_status text NOT NULL DEFAULT 'needs_review'
 CHECK(verification_status IN ('unverified','ai_draft','needs_review','verified','rejected')),
 is_demo boolean NOT NULL DEFAULT true, source_id uuid NOT NULL REFERENCES public.vuz_sources(id),
 external_key text NOT NULL, UNIQUE(source_id,external_key),
 CHECK((period_start IS NULL)=(period_end IS NULL)), CHECK(period_end IS NULL OR period_end>=period_start),
 CHECK(metric_code<>'payroll_share' OR value<=1),
 CHECK(NOT(is_demo AND verification_status='verified')),
 CHECK(verification_status<>'verified' OR (period_start IS NOT NULL AND scope_code<>'unknown' AND methodology<>'unknown' AND aggregation_kind<>'unknown'))
);
CREATE UNIQUE INDEX vuz_verified_metric ON public.vuz_university_metrics(university_id,metric_code,period_start,period_end,period_kind,aggregation_kind,scope_code,methodology,scenario,unit) WHERE verification_status='verified';
CREATE INDEX vuz_metrics_university ON public.vuz_university_metrics(university_id,metric_code);
CREATE TABLE public.vuz_admission_campaigns (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), university_id uuid NOT NULL REFERENCES public.vuz_universities(id) ON DELETE RESTRICT,
 academic_year integer NOT NULL CHECK(academic_year BETWEEN 1900 AND 2200), program_code text NOT NULL,
 program_name text NOT NULL, education_level text NOT NULL, study_mode text NOT NULL,
 budget_places integer CHECK(budget_places>=0), paid_places integer CHECK(paid_places>=0),
 applications integer CHECK(applications>=0), passing_score numeric(8,2) CHECK(passing_score>=0),
 tuition_rub numeric(18,2) CHECK(tuition_rub>=0), source_id uuid REFERENCES public.vuz_sources(id),
 verification_status text NOT NULL DEFAULT 'needs_review' CHECK(verification_status IN ('unverified','ai_draft','needs_review','verified','rejected')),
 UNIQUE(university_id,academic_year,program_code,education_level,study_mode)
);
CREATE TABLE public.vuz_infrastructure (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), university_id uuid NOT NULL REFERENCES public.vuz_universities(id) ON DELETE RESTRICT,
 feature_code text NOT NULL, feature_name text NOT NULL, available boolean,
 capacity integer CHECK(capacity>=0), details text, as_of date NOT NULL,
 source_id uuid REFERENCES public.vuz_sources(id),
 verification_status text NOT NULL DEFAULT 'needs_review' CHECK(verification_status IN ('unverified','ai_draft','needs_review','verified','rejected')),
 UNIQUE(university_id,feature_code,as_of)
);
CREATE TABLE public.vuz_projects (
 id uuid PRIMARY KEY, university_id uuid NOT NULL REFERENCES public.vuz_universities(id) ON DELETE RESTRICT,
 name text NOT NULL, kind text NOT NULL DEFAULT 'university' CHECK(kind IN ('university','federal','regional','research','sber')),
 partnership_status text NOT NULL DEFAULT 'reported' CHECK(partnership_status IN ('reported','planned','active','completed','cancelled')),
 description text, starts_on date, ends_on date, sber_confirmed boolean NOT NULL DEFAULT false,
 is_demo boolean NOT NULL DEFAULT true, source_id uuid NOT NULL REFERENCES public.vuz_sources(id),
 external_key text NOT NULL, verification_status text NOT NULL DEFAULT 'needs_review'
 CHECK(verification_status IN ('unverified','ai_draft','needs_review','verified','rejected')),
 UNIQUE(source_id,external_key), CHECK(ends_on IS NULL OR starts_on IS NULL OR ends_on>=starts_on),
 CHECK(NOT sber_confirmed OR (kind='sber' AND NOT is_demo AND verification_status='verified'))
);
CREATE TABLE public.vuz_bank_products (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), code text NOT NULL UNIQUE, name text NOT NULL,
 description text, active boolean NOT NULL DEFAULT true
);
CREATE TABLE public.vuz_product_recommendations (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), university_id uuid NOT NULL REFERENCES public.vuz_universities(id) ON DELETE RESTRICT,
 product_id uuid NOT NULL REFERENCES public.vuz_bank_products(id) ON DELETE RESTRICT,
 status text NOT NULL DEFAULT 'proposed' CHECK(status IN ('proposed','reviewed','offered','accepted','rejected')),
 rationale text NOT NULL, proposed_by text NOT NULL CHECK(proposed_by IN ('human','ai')),
 reviewed_by uuid REFERENCES public.vuz_users(id), reviewed_at timestamptz,
 source_id uuid REFERENCES public.vuz_sources(id), UNIQUE(university_id,product_id),
 CHECK(status='proposed' OR (reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL))
);
CREATE TABLE public.vuz_contracts (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), university_id uuid NOT NULL REFERENCES public.vuz_universities(id) ON DELETE RESTRICT,
 number text NOT NULL, signed_on date NOT NULL, contract_type text NOT NULL, subject text NOT NULL,
 amount numeric(20,2) CHECK(amount>=0), currency text CHECK(currency ~ '^[A-Z]{3}$'),
 valid_from date, valid_to date, status text NOT NULL DEFAULT 'unknown' CHECK(status IN ('draft','active','completed','terminated','unknown')),
 source_id uuid REFERENCES public.vuz_sources(id), UNIQUE(university_id,number,signed_on,contract_type),
 CHECK((amount IS NULL)=(currency IS NULL)), CHECK(valid_to IS NULL OR valid_from IS NULL OR valid_to>=valid_from)
);
CREATE TABLE public.vuz_meetings (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), university_id uuid NOT NULL REFERENCES public.vuz_universities(id) ON DELETE RESTRICT,
 title text NOT NULL, starts_at timestamptz, ends_at timestamptz, location text,
 status text NOT NULL DEFAULT 'planned' CHECK(status IN ('planned','held','cancelled','unknown')),
 agenda text, summary text, source_id uuid REFERENCES public.vuz_sources(id),
 CHECK(ends_at IS NULL OR (starts_at IS NOT NULL AND ends_at>=starts_at))
);
CREATE TABLE public.vuz_meeting_participants (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), meeting_id uuid NOT NULL REFERENCES public.vuz_meetings(id) ON DELETE RESTRICT,
 official_id uuid REFERENCES public.vuz_officials(id) ON DELETE RESTRICT,
 user_id uuid REFERENCES public.vuz_users(id) ON DELETE RESTRICT,
 name_snapshot text NOT NULL, role_snapshot text, CHECK(official_id IS NULL OR user_id IS NULL)
);
CREATE TABLE public.vuz_follow_ups (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), meeting_id uuid NOT NULL REFERENCES public.vuz_meetings(id) ON DELETE RESTRICT,
 description text NOT NULL, owner_id uuid REFERENCES public.vuz_users(id), due_on date,
 status text NOT NULL DEFAULT 'open' CHECK(status IN ('open','in_progress','done','cancelled')),
 result text, completed_at timestamptz, CHECK(status<>'done' OR (result IS NOT NULL AND completed_at IS NOT NULL))
);
CREATE TABLE public.vuz_import_records (
 id uuid PRIMARY KEY, batch_id uuid NOT NULL REFERENCES public.vuz_import_batches(id),
 source_id uuid NOT NULL REFERENCES public.vuz_sources(id), source_key text NOT NULL,
 university_id uuid NOT NULL REFERENCES public.vuz_universities(id) ON DELETE RESTRICT,
 payload jsonb NOT NULL, payload_hash text NOT NULL CHECK(payload_hash ~ '^[a-f0-9]{64}$'),
 imported_at timestamptz NOT NULL DEFAULT now(), UNIQUE(source_id,source_key)
);
CREATE TABLE public.vuz_legacy_fields (
 id uuid PRIMARY KEY, university_id uuid NOT NULL REFERENCES public.vuz_universities(id) ON DELETE RESTRICT,
 source_id uuid NOT NULL REFERENCES public.vuz_sources(id), source_key text NOT NULL, field_name text NOT NULL,
 value jsonb NOT NULL, reason text NOT NULL, UNIQUE(source_id,source_key,field_name)
);
CREATE TABLE public.vuz_audit_log (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, table_name text NOT NULL, record_id uuid NOT NULL,
 operation text NOT NULL CHECK(operation IN ('INSERT','UPDATE','DELETE')), field_name text NOT NULL,
 old_value jsonb, new_value jsonb, record_version bigint NOT NULL,
 changed_at timestamptz NOT NULL DEFAULT clock_timestamp(), actor_id uuid,
 actor_type text NOT NULL CHECK(actor_type IN ('human','ai','migration','system')),
 actor_label text NOT NULL, database_role text NOT NULL
);
CREATE INDEX vuz_audit_record ON public.vuz_audit_log(table_name,record_id,record_version);
CREATE INDEX vuz_audit_date ON public.vuz_audit_log(changed_at);
