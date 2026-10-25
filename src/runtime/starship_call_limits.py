"""Explicit JSON null means no call-count cap; clocks and authority still apply.

Finite historical grants retain their exact limits. Booleans, negative counts,
missing fields and infinity are not spellings of unlimited authorization.
"""


def valid_call_limit(limit):
    return limit is None or (type(limit) is int and limit >= 0)


def call_count_allowed(count, limit):
    return (type(count) is int and count >= 0 and valid_call_limit(limit)
            and (limit is None or count <= limit))


def has_call_slot(used, limit):
    return type(used) is int and used >= 0 and call_count_allowed(used + 1, limit)


def remaining_calls(used, limit):
    if type(used) is not int or used < 0 or not valid_call_limit(limit):
        raise ValueError("invalid_call_count_or_limit")
    return None if limit is None else max(0, limit - used)


def combined_call_limit(*limits):
    if not all(valid_call_limit(limit) for limit in limits):
        raise ValueError("invalid_call_limit")
    return None if any(limit is None for limit in limits) else sum(limits)


def canonical_uncapped_jev_escalations(envelope):
    """Only the complete current event-normal grant opts into this route.

    Jev request count/deadlines remain bounded. Removing a field, copying the
    marker to another grant, or nulling a historical grant never upgrades it.
    """
    if (type(envelope) is not dict or envelope.get("mode") != "live"
            or "maximum_llm_calls" not in envelope or envelope["maximum_llm_calls"] is not None
            or envelope.get("deepseek_call_budget_policy") != "unlimited_jev_escalations_v1"):
        return False
    from .starship_replanning import contract, digest
    try:
        return digest(envelope) == digest(contract("live", "event_normal"))
    except (TypeError, ValueError):
        return False
