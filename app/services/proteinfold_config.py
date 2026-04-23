"""Proteinfold workflow configuration and executor settings (modeled after bindflow).
"""
from __future__ import annotations

_DB_BASE = "/g/data/if89/proteinfold_dbs/proteinfold_minidbs/"


def get_proteinfold_default_params(
    out_dir: str, samplesheet_url: str, mode: str = "alphafold2"
) -> list[str]:
    """Get default parameters for proteinfold workflow."""
    return [
        f'outdir: "{out_dir}"',
        f'input: "{samplesheet_url}"',
        f'alphafold2_db: "{_DB_BASE}"',
        f'alphafold2_bfd_path: "{_DB_BASE}/bfd/*"',
        f'alphafold2_small_bfd_path: "{_DB_BASE}/small_bfd/*"',
        f'alphafold2_params_path: "{_DB_BASE}/params/alphafold_params_2022-12-06/*"',
        f'alphafold2_mgnify_path: "{_DB_BASE}/mgnify/*"',
        f'alphafold2_pdb70_path: "{_DB_BASE}/pdb70/**"',
        f'alphafold2_pdb_mmcif_path: "{_DB_BASE}/pdb_mmcif/mmcif_files"',
        f'alphafold2_pdb_obsolete_path: "{_DB_BASE}/pdb_mmcif/obsolete.dat"',
        f'alphafold2_uniref30_path: "{_DB_BASE}/uniref30/*"',
        f'alphafold2_uniref90_path: "{_DB_BASE}/uniref90/*"',
        f'alphafold2_pdb_seqres_path: "{_DB_BASE}/pdb_seqres/*"',
        f'alphafold2_uniprot_path: "{_DB_BASE}/uniprot/*"',
        f'colabfold_db: "{_DB_BASE}"',
        f'colabfold_envdb_path: "{_DB_BASE}/colabfold_envdb/*"',
        f'colabfold_uniref30_path: "{_DB_BASE}/colabfold_uniref30/*"',
        f'colabfold_alphafold2_params_path: "{_DB_BASE}/params/alphafold_params_2022-12-06"',
        f'boltz_db: "{_DB_BASE}"',
        f'boltz_ccd_path: "{_DB_BASE}/params/ccd.pkl"',
        f'boltz_model_path: "{_DB_BASE}/params/boltz1_conf.ckpt"',
        f'boltz2_aff_path: "{_DB_BASE}/params/boltz2_aff.ckpt"',
        f'boltz2_conf_path: "{_DB_BASE}/params/boltz2_conf.ckpt"',
        f'boltz2_mols_path: "{_DB_BASE}/params/mols/"',
        'project: "yz52"',
        f'mode: "{mode}"',
        "use_gpu: true",
    ]


def get_proteinfold_executor_script(
    aws_access_key: str = "", aws_secret_key: str = "", aws_region: str = "ap-southeast-2"
) -> str:
    """Get the executor pre-run script for proteinfold workflow on Gadi."""
    return f"""module load singularity
module load nextflow
export AWS_ACCESS_KEY_ID={aws_access_key}
export AWS_SECRET_ACCESS_KEY={aws_secret_key}
export AWS_REGION={aws_region}
"""


def get_proteinfold_config_profiles() -> list[str]:
    """Get config profiles for proteinfold workflow."""
    return ["singularity"]
