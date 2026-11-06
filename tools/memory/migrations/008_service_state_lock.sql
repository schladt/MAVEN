-- Row locking requires UPDATE privilege even when callers only read state.
-- Keep that privilege with the function owner, never with the application role.
CREATE FUNCTION maven.lock_service_state() RETURNS jsonb
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog AS $$
    SELECT jsonb_build_object('instance_id', instance_id, 'recovery_epoch', recovery_epoch,
        'schema_version', schema_version, 'state', state, 'data_scope', data_scope)
    FROM maven.service_state WHERE singleton FOR SHARE
$$
-- statement
REVOKE ALL ON FUNCTION maven.lock_service_state() FROM PUBLIC
