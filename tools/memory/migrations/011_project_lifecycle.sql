-- Archival is a work barrier, not record invisibility: history remains readable.
-- Only explicit administration changes this table; application writes share the
-- same project advisory lock, including when no lifecycle row exists yet.
CREATE TABLE maven.project_lifecycle (
    project_id uuid PRIMARY KEY REFERENCES maven.memory_items(id),
    archived boolean NOT NULL,
    generation bigint NOT NULL CHECK (generation > 0),
    action_key text NOT NULL CHECK (action_key ~ '^[0-9a-f]{64}$'),
    actor_id text NOT NULL,
    changed_at timestamptz NOT NULL DEFAULT transaction_timestamp()
)
-- statement
REVOKE ALL ON maven.project_lifecycle FROM PUBLIC
-- statement
CREATE FUNCTION maven.require_project_work(project uuid) RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog AS $$
BEGIN
    IF project IS NULL THEN RETURN; END IF;
    PERFORM pg_advisory_xact_lock_shared(hashtextextended('project-work:' || project::text, 0));
    IF EXISTS (SELECT 1 FROM maven.project_lifecycle WHERE project_id=project AND archived) THEN
        RAISE EXCEPTION 'project_archived' USING ERRCODE = '55000';
    END IF;
END
$$
-- statement
REVOKE ALL ON FUNCTION maven.require_project_work(uuid) FROM PUBLIC
-- statement
CREATE FUNCTION maven.require_project_scope(scope_value jsonb) RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE project_text text := scope_value->>'project';
BEGIN
    -- Existing named scopes remain valid. Only UUID scopes bind cloud projects.
    IF project_text ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
        PERFORM maven.require_project_work(project_text::uuid);
    END IF;
END
$$
-- statement
REVOKE ALL ON FUNCTION maven.require_project_scope(jsonb) FROM PUBLIC
-- statement
CREATE FUNCTION maven.guard_project_work() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE item maven.memory_items%ROWTYPE; proposal_scope jsonb;
BEGIN
    IF TG_TABLE_NAME='memory_items' THEN
        IF TG_OP<>'INSERT' THEN
            PERFORM maven.require_project_work(OLD.project_id);
            IF OLD.data->>'entity_type'='project' THEN PERFORM maven.require_project_work(OLD.id); END IF;
        END IF;
        IF TG_OP<>'DELETE' THEN
            PERFORM maven.require_project_work(NEW.project_id);
            IF NEW.data->>'entity_type'='project' THEN PERFORM maven.require_project_work(NEW.id); END IF;
        END IF;
    ELSIF TG_TABLE_NAME='records' THEN
        SELECT * INTO item FROM maven.memory_items WHERE id=OLD.id;
        IF FOUND THEN
            PERFORM maven.require_project_work(item.project_id);
            IF item.data->>'entity_type'='project' THEN PERFORM maven.require_project_work(item.id); END IF;
        END IF;
        SELECT scope INTO proposal_scope FROM maven.pages WHERE id=OLD.id;
        PERFORM maven.require_project_scope(proposal_scope);
    ELSIF TG_TABLE_NAME='instruction_proposals' THEN
        IF TG_OP<>'INSERT' THEN
            PERFORM maven.require_project_scope(OLD.candidate->'scope');
            SELECT scope INTO proposal_scope FROM maven.pages WHERE id=OLD.page_id;
            PERFORM maven.require_project_scope(proposal_scope);
        END IF;
        IF TG_OP<>'DELETE' THEN
            PERFORM maven.require_project_scope(NEW.candidate->'scope');
            SELECT scope INTO proposal_scope FROM maven.pages WHERE id=NEW.page_id;
            PERFORM maven.require_project_scope(proposal_scope);
        END IF;
    ELSIF TG_TABLE_NAME='pages' THEN
        IF TG_OP<>'INSERT' THEN PERFORM maven.require_project_scope(OLD.scope); END IF;
        IF TG_OP<>'DELETE' THEN PERFORM maven.require_project_scope(NEW.scope); END IF;
    ELSIF TG_TABLE_NAME='instruction_challenges' THEN
        SELECT candidate->'scope' INTO proposal_scope FROM maven.instruction_proposals WHERE id=NEW.proposal_id;
        PERFORM maven.require_project_scope(proposal_scope);
        SELECT p.scope INTO proposal_scope FROM maven.pages p
        JOIN maven.instruction_proposals proposal ON proposal.page_id=p.id WHERE proposal.id=NEW.proposal_id;
        PERFORM maven.require_project_scope(proposal_scope);
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END
$$
-- statement
REVOKE ALL ON FUNCTION maven.guard_project_work() FROM PUBLIC
-- statement
CREATE TRIGGER project_work_memory BEFORE INSERT OR UPDATE OR DELETE ON maven.memory_items
FOR EACH ROW EXECUTE FUNCTION maven.guard_project_work()
-- statement
CREATE TRIGGER project_work_record BEFORE UPDATE OR DELETE ON maven.records
FOR EACH ROW EXECUTE FUNCTION maven.guard_project_work()
-- statement
CREATE TRIGGER project_work_proposal BEFORE INSERT OR UPDATE OR DELETE ON maven.instruction_proposals
FOR EACH ROW EXECUTE FUNCTION maven.guard_project_work()
-- statement
CREATE TRIGGER project_work_page BEFORE INSERT OR UPDATE OR DELETE ON maven.pages
FOR EACH ROW EXECUTE FUNCTION maven.guard_project_work()
-- statement
CREATE TRIGGER project_work_challenge BEFORE INSERT ON maven.instruction_challenges
FOR EACH ROW EXECUTE FUNCTION maven.guard_project_work()
