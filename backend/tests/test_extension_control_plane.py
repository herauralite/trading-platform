from app.services.execution_status import validate_command_transition


def test_command_transition_graph_accepts_expected_paths():
    assert validate_command_transition("queued", "dispatched")
    assert validate_command_transition("dispatched", "acked")
    assert validate_command_transition("acked", "running")
    assert validate_command_transition("running", "succeeded")


def test_command_transition_graph_rejects_invalid_paths():
    assert not validate_command_transition("queued", "succeeded")
    assert not validate_command_transition("failed", "running")
