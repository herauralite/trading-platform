def validate_command_transition(current: str, target: str) -> bool:
    graph = {
        "queued": {"dispatched", "expired"},
        "dispatched": {"acked", "running", "failed", "expired"},
        "acked": {"running", "failed", "expired"},
        "running": {"succeeded", "failed", "expired"},
        "succeeded": set(),
        "failed": set(),
        "expired": set(),
    }
    return target in graph.get(current, set())
