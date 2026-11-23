"""Protein FASTA validation and per-entry metadata."""

import io

from Bio import SeqIO
from Bio.Data.IUPACData import protein_letters
from Bio.SeqRecord import SeqRecord
from pydantic import BaseModel

PROTEIN_ALPHABET = frozenset(protein_letters)


class ProteinSequenceInfo(BaseModel):
    sequence_length: int
    multi_chain: bool = False
    n_chains: int = 1


class ProteinFastaValidator:
    """Parse FASTA text and validate standard, uppercase protein sequences."""

    entries: list[SeqRecord]

    def __init__(self, fasta_content: str):
        content = fasta_content.lstrip()
        if not content.startswith(">"):
            raise ValueError("FASTA content must start with a header beginning with >.")
        self.entries = list(SeqIO.parse(io.StringIO(content), "fasta"))
        if not self.entries:
            raise ValueError("FASTA must contain at least one entry.")

    @classmethod
    def validate(
        cls, fasta_content: str, max_chains: int | None = None
    ) -> list[ProteinSequenceInfo]:
        return cls(fasta_content).check_protein_sequences(max_chains=max_chains)

    def check_protein_sequences(self, max_chains: int | None = None) -> list[ProteinSequenceInfo]:
        entry_info = []
        for index, record in enumerate(self.entries, start=1):
            chains = "".join(str(record.seq).split()).split(":")
            n_chains = len(chains)
            if max_chains is not None and n_chains > max_chains:
                raise ValueError(f"Entry {index} must have at most {max_chains} chains.")
            for chain_index, chain in enumerate(chains, start=1):
                if not chain:
                    raise ValueError(f"Entry {index}, chain {chain_index} is empty.")
                invalid = set(chain) - PROTEIN_ALPHABET
                if invalid:
                    raise ValueError(
                        f"Entry {index}, chain {chain_index} contains invalid protein "
                        f"characters: {', '.join(sorted(invalid))}."
                    )
            entry_info.append(
                ProteinSequenceInfo(
                    sequence_length=sum(map(len, chains)),
                    multi_chain=n_chains > 1,
                    n_chains=n_chains,
                )
            )
        return entry_info
