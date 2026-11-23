"""Shared protein FASTA validation rules."""

import pytest

from app.services.fasta_validation import ProteinFastaValidator, ProteinSequenceInfo


def test_fasta_metadata_combines_chains_and_wrapped_lines():
    fasta = "\n>first description\n" + "A" * 800 + ":\n" + "C" * 700 + "\n>second\nAC D\tE\nFG\n"
    assert ProteinFastaValidator.validate(fasta) == [
        ProteinSequenceInfo(sequence_length=1500, multi_chain=True, n_chains=2),
        ProteinSequenceInfo(sequence_length=6),
    ]


@pytest.mark.parametrize(
    "fasta",
    [
        "",
        "   ",
        "ACDE",
        "ACDE\n>record\nACDE",
        ">empty\n",
        ">empty\n>valid\nACDE",
        ">empty\n : \n",
        ">p\n:ACD",
        ">p\nACD:",
        ">p\nACD::EF",
        ">p\nACD: \t :EF",
        ">p\nAC1",
        ">p\nAC*",
        ">p\nAC-",
        ">p\nACX",
        ">p\nACB",
        ">p\nACU",
        ">p\nACO",
        ">p\nacd",
    ],
)
def test_rejects_invalid_protein_fasta(fasta):
    with pytest.raises(ValueError):
        ProteinFastaValidator.validate(fasta)


@pytest.mark.parametrize("n_chains", [1, 26])
def test_chain_limit_is_per_entry(n_chains):
    fasta = ">first\n" + ":".join(["ACD"] * n_chains) + "\n>second\nACD"
    entries = ProteinFastaValidator.validate(fasta, max_chains=26)
    assert len(entries) == 2
    assert entries[0].n_chains == n_chains
    assert entries[0].sequence_length == 3 * n_chains


def test_rejects_too_many_chains():
    with pytest.raises(ValueError, match="Entry 2 must have at most 26 chains"):
        ProteinFastaValidator.validate(
            ">first\nACD\n>second\n" + ":".join(["ACD"] * 27), max_chains=26
        )


def test_no_chain_limit_unless_requested():
    assert ProteinFastaValidator.validate(">p\n" + ":".join(["ACD"] * 27))[0].n_chains == 27


def test_invalid_character_error_identifies_entry_and_chain():
    with pytest.raises(ValueError, match="Entry 2, chain 2.*invalid protein characters: 1, X"):
        ProteinFastaValidator.validate(">valid\nACD\n>invalid\nACD:EF1X")
