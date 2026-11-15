-- Correct JSONB subtraction precedence without changing the deployed migration 020.
CREATE OR REPLACE FUNCTION maven.guard_secret_adapter_home() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE candidate jsonb:=NEW.candidate; home uuid;
BEGIN
    IF candidate->>'slug' NOT LIKE 'secret-adapter-%' AND candidate->>'slug' NOT LIKE 'secret-grant-%' THEN
        RETURN NEW;
    END IF;
    IF NEW.data_scope<>'owner' OR candidate->>'page_kind' IS DISTINCT FROM 'procedure'
       OR candidate->'requires' IS DISTINCT FROM '["maven-secrets"]'::jsonb
       OR jsonb_typeof(candidate->'scope') IS DISTINCT FROM 'object'
       OR (candidate->'scope') - 'project' <> '{}'::jsonb OR NOT candidate->'scope' ? 'project' THEN
        RAISE EXCEPTION 'invalid secret procedure authority' USING ERRCODE='23514';
    END IF;
    home:=(candidate->'scope'->>'project')::uuid;
    IF NOT EXISTS (SELECT 1 FROM maven.records r JOIN maven.memory_items m ON m.id=r.id
        WHERE r.id=home AND r.kind='entity' AND r.data_scope='owner' AND m.project_id IS NULL
          AND m.data->>'entity_type'='project' AND maven.memory_visible(r.id))
       OR maven.project_archived(home) THEN
        RAISE EXCEPTION 'invalid secret procedure Home' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END
$$
