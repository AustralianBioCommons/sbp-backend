"""Proteinfold workflow configuration and executor settings (modeled after bindflow).
"""
from __future__ import annotations

def get_proteinfold_default_params(out_dir: str, samplesheet_url: str, mode: str = "alphafold2") -> list[str]:
    """Get default parameters for proteinfold workflow."""
    proteinfold_base_path = "/g/data/if89/proteinfold_dbs/proteinfold_minidbs"
    return [
        f'outdir: "{out_dir}"',
        f'input: "{samplesheet_url}"',
        f'base_path: "{proteinfold_base_path}"',
        f'alphafold2_small_bfd_path: "{proteinfold_base_path}/small_bfd/*"',
        f'boltz_ccd_path: "{proteinfold_base_path}/params/ccd.pkl"',
        f'alphafold2_uniref30_path: "{proteinfold_base_path}/uniref30/*"',
        f'alphafold2_params_path: "{proteinfold_base_path}/params/alphafold_params_2022-12-06/*"',
        f'boltz2_aff_path: "{proteinfold_base_path}/params/boltz2_aff.ckpt"',
        f'colabfold_alphafold2_params_path: "{proteinfold_base_path}/params/alphafold_params_2022-12-06"',
        f'colabfold_uniref30_path: "{proteinfold_base_path}/colabfold_uniref30/*"',
        f'alphafold2_db: "{proteinfold_base_path}/"',
        f'alphafold2_mgnify_path: "{proteinfold_base_path}/mgnify/*"',
        f'alphafold2_bfd_path: "{proteinfold_base_path}/bfd/*"',
        f'alphafold2_pdb_mmcif_path: "{proteinfold_base_path}/pdb_mmcif/mmcif_files"',
        f'db: "{proteinfold_base_path}/"',
        f'alphafold2_uniprot_path: "{proteinfold_base_path}/uniprot/*"',
        f'colabfold_db: "{proteinfold_base_path}/"',
        f'alphafold2_pdb_obsolete_path: "{proteinfold_base_path}/pdb_mmcif/obsolete.dat"',
        'project: "yz52"',
        f'alphafold2_pdb_seqres_path: "{proteinfold_base_path}/pdb_seqres/*"',
        f'mode: "{mode}"',
        f'boltz_model_path: "{proteinfold_base_path}/params/boltz1_conf.ckpt"',
        f'boltz2_mols_path: "{proteinfold_base_path}/params/mols/"',
        f'alphafold2_uniref90_path: "{proteinfold_base_path}/uniref90/*"',
        f'boltz2_conf_path: "{proteinfold_base_path}/params/boltz2_conf.ckpt"',
        f'colabfold_envdb_path: "{proteinfold_base_path}/colabfold_envdb/*"',
        f'alphafold2_pdb70_path: "{proteinfold_base_path}/pdb70/**"',
    ]

def get_proteinfold_executor_script(aws_access_key: str = "", aws_secret_key: str = "", aws_region: str = "ap-southeast-2") -> str:
    """Get the executor pre-run script for proteinfold workflow on Gadi."""
    return f"""module load singularity
module load nextflow
export AWS_ACCESS_KEY_ID={aws_access_key}
export AWS_SECRET_ACCESS_KEY={aws_secret_key}
export AWS_REGION={aws_region}
"""

def get_proteinfold_config_profiles() -> list[str]:
    """Get config profiles for proteinfold workflow."""
    return ["singularity", "gadi"]
