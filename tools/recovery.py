"""No dead ends: present a next step without running it or assuming rollback."""

DEFAULT_NEXT_ACTION = ('Keep local work. Ask MAVEN: “Inspect the failed step and any saved receipt without '
                       'making changes. Tell me what completed and how to continue.” '
                       'If MAVEN cannot inspect it, give a maintainer the step name, error code and any '
                       'operation ID, not private inputs. Verify what changed before retrying a write.')

# Some tools report a partial or uncertain outcome as data, not a protocol error.
RECOVERY_STATES = frozenset({
    'error', 'operation_failed', 'failed', 'blocked', 'conflict', 'uncertain', 'unsynchronized',
    'outcome_unknown', 'partial', 'unavailable', 'authentication_required', 'incompatible',
    'rate_limited', 'interrupted', 'paused',
})


def recovery_action(result, *, failed=False):
    """Return the advertised remedy or the fallback; leave successful results alone."""
    if failed or result.get('error') or any(
        isinstance(value, str) and value in RECOVERY_STATES
        for value in (result.get('status'), result.get('state'))
    ):
        for key in ('next_action', 'recovery', 'guidance'):
            action = result.get(key)
            if isinstance(action, str) and action.strip():
                return action
        return DEFAULT_NEXT_ACTION
    return None
