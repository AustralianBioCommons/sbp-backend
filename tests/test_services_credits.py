"""Credit calculations, including per-record FASTA size pricing."""

import pytest

from app.schemas.workflows.shared import WispsInputInfo
from app.services.credits import (
    BULK_PREDICTION_SIZE_MULTIPLIERS,
    get_sequence_size_multiplier,
    launch_credit_cost,
)


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
    assert get_sequence_size_multiplier(length, BULK_PREDICTION_SIZE_MULTIPLIERS) == expected


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
            WispsInputInfo(query_lengths=[400, 400, 400], target_lengths=[400, 400]),
            18,
        ),
    ],
)
def test_launch_cost(category, tool, designs, wisps_input_info, expected):
    assert (
        launch_credit_cost(category, tool, designs, wisps_input_info=wisps_input_info) == expected
    )


@pytest.mark.parametrize(
    ("combined_length", "boltz_cost", "colabfold_cost"),
    [
        (1000, 1, 3),
        (1001, 3, 10),
        (2000, 3, 10),
        (2001, 5, 20),
        (3000, 5, 20),
        (3001, 9, 50),
        (4000, 9, 50),
    ],
)
@pytest.mark.parametrize("tool", ["boltz", "colabfold"])
def test_interaction_pair_length_boundaries(combined_length, boltz_cost, colabfold_cost, tool):
    assert launch_credit_cost(
        "interaction-screening",
        tool,
        None,
        wisps_input_info=WispsInputInfo(
            query_lengths=[400], target_lengths=[combined_length - 400]
        ),
    ) == (boltz_cost if tool == "boltz" else colabfold_cost)


@pytest.mark.parametrize(("tool", "expected"), [("boltz", 12), (" COLABFOLD ", 43)])
def test_interaction_sums_every_pair(tool, expected):
    # Pair lengths: 800, 1400, 1600, 2200; price each prediction independently.
    assert (
        launch_credit_cost(
            "interaction-screening",
            tool,
            None,
            wisps_input_info=WispsInputInfo(query_lengths=[400, 1200], target_lengths=[400, 1000]),
        )
        == expected
    )


def test_boltz_interaction_credit_example():
    # One query produces 20, 10, 5, and 1 pairs in the four combined-length bins.
    info = WispsInputInfo(
        query_lengths=[400],
        target_lengths=[600] * 20 + [1600] * 10 + [2600] * 5 + [3600],
    )

    cost = launch_credit_cost("interaction-screening", "boltz", None, wisps_input_info=info)

    assert cost == 84  # (20 × 1) + (10 × 3) + (5 × 5) + (1 × 9)


@pytest.mark.parametrize(
    "info",
    [
        None,
        WispsInputInfo(),
        WispsInputInfo(query_lengths=[], target_lengths=[400]),
        WispsInputInfo(query_lengths=[400], target_lengths=[]),
        WispsInputInfo(query_lengths=[400]),
        WispsInputInfo(target_lengths=[400]),
    ],
)
def test_interaction_missing_lengths(info):
    assert launch_credit_cost("interaction-screening", "boltz", None, wisps_input_info=info) is None


def test_interaction_unknown_tool():
    assert (
        launch_credit_cost(
            "interaction-screening",
            "unknown",
            None,
            wisps_input_info=WispsInputInfo(query_lengths=[400], target_lengths=[400]),
        )
        is None
    )


@pytest.mark.parametrize("tool", ["boltz", "colabfold"])
def test_interaction_rejects_oversized_pair(tool):
    with pytest.raises(ValueError, match="4001.*unsupported"):
        launch_credit_cost(
            "interaction-screening",
            tool,
            None,
            wisps_input_info=WispsInputInfo(query_lengths=[2000], target_lengths=[2001]),
        )
