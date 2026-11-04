"""Credit calculations, including per-record FASTA size pricing."""

import pytest

from app.schemas.workflows.shared import WispsInputInfo
from app.services.credits import get_sequence_size_multiplier, launch_credit_cost


@pytest.mark.parametrize(
    ("length", "expected"),
    [
        (1, 1),
        (999, 1),
        (1000, 1),
        (1001, 4),
        (1999, 4),
        (2000, 10),
        (2001, 10),
        (2999, 10),
        (3000, 16),
        (3001, 16),
        (4000, 16),
    ],
)
def test_sequence_size_boundaries(length, expected):
    assert get_sequence_size_multiplier(length) == expected


@pytest.mark.parametrize("length", [-1, 0, 4001, 10000])
def test_unsupported_sequence_size(length):
    with pytest.raises(ValueError, match="unsupported"):
        launch_credit_cost(
            "bulk-prediction",
            "boltz",
            None,
            wisps_input_info=WispsInputInfo(fasta_entry_lengths=[length]),
        )


@pytest.mark.parametrize(
    ("category", "tool", "designs", "wisps_input_info", "expected"),
    [
        (
            "bulk-prediction",
            "boltz",
            None,
            WispsInputInfo(fasta_entry_lengths=[800, 1500, 2500, 3500]),
            31,
        ),
        (
            " BULK-PREDICTION ",
            " COLABFOLD ",
            None,
            WispsInputInfo(fasta_entry_lengths=[800, 1500]),
            15,
        ),
        ("bulk-prediction", "boltz", None, WispsInputInfo(), None),
        ("bulk-prediction", "boltz", None, WispsInputInfo(fasta_entry_lengths=[]), None),
        ("bulk-prediction", "unknown", None, WispsInputInfo(fasta_entry_lengths=[800]), None),
        ("single-prediction", "boltz", None, None, 50),
        ("de-novo-design", "bindcraft", 3, None, 30),
        (
            "interaction-screening",
            "colabfold",
            None,
            WispsInputInfo(query_count=3, target_count=2),
            30,
        ),
    ],
)
def test_launch_cost(category, tool, designs, wisps_input_info, expected):
    assert (
        launch_credit_cost(category, tool, designs, wisps_input_info=wisps_input_info) == expected
    )
