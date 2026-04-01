from pathlib import Path


PALETTE_SOURCE = Path(__file__).resolve().parents[1] / "ui" / "serial_tab.py"


def test_palette_includes_documented_ppg_workflow_commands():
    source = PALETTE_SOURCE.read_text()

    expected_commands = [
        "adpd ppg list",
        "adpd ppg slota show",
        "adpd ppg slotab show",
        "adpd ppg slota2 show",
        "adpd ppg slota reset",
        "adpd ppg slota2 start",
    ]

    for command in expected_commands:
        assert command in source, command


def test_palette_groups_ppg_actions_by_user_task_and_keeps_custom_fallback():
    source = PALETTE_SOURCE.read_text()

    expected_labels = [
        "Explore",
        "PPG Profiles",
        "PPG Start & Capture",
        "Probe & Inspect",
        "Interfaces",
        "Storage & Bus",
        "Custom command",
        "enter any command…",
    ]

    for label in expected_labels:
        assert label in source, label
