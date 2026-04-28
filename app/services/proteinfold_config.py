"""Proteinfold workflow configuration and executor settings (modeled after bindflow).
"""
from __future__ import annotations

_DB_BASE = "/g/data/if89/proteinfold_dbs/proteinfold_minidbs/"
_SINGULARITY_CACHE_DIR = "/g/data/if89/singularity_cache/"

_COLABFOLD_ALPHAFOLD2_PARAMS_TAGS = {
    "alphafold2_multimer_v1": "alphafold_params_colab_2021-10-27",
    "alphafold2_multimer_v2": "alphafold_params_colab_2022-03-02",
    "alphafold2_multimer_v3": "alphafold_params_colab_2022-12-06",
    "alphafold2_ptm": "alphafold_params_2022-12-06",
}


def get_proteinfold_default_params(
    out_dir: str, samplesheet_url: str, mode: str = "alphafold2"
) -> list[str]:
    """Get default parameters for proteinfold workflow."""
    tags_lines = "\n".join(
        f'    {k}: "{v}"' for k, v in _COLABFOLD_ALPHAFOLD2_PARAMS_TAGS.items()
    )
    return [
        f'outdir: "{out_dir}"',
        f'input: "{samplesheet_url}"',
        f'db: "{_DB_BASE}"',
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
        f"colabfold_alphafold2_params_tags:\n{tags_lines}",
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


def get_proteinfold_config_text() -> str:
    """Get Nextflow configText for the Seqera launch payload."""
    return f"""\
params {{
    use_gpu = true
}}

singularity {{
    enabled = true
    autoMounts = true
    cacheDir = '{_SINGULARITY_CACHE_DIR}'
    runOptions = '--bind /g/data'
}}

executor {{
    queueSize = 300
    pollInterval = '5 min'
    queueStatInterval = '5 min'
    submitRateLimit = '20 min'
}}

process {{
    executor = 'pbspro'
    storage = 'gdata/if89+gdata/li87'
    module = 'singularity'
    cache = 'lenient'
    stageInMode = 'symlink'
    queue = {{ task.memory < 128.GB ? 'normalbw' : (task.memory >= 128.GB && task.memory <= 190.GB ? 'normal' : (task.memory > 190.GB && task.memory <= 1020.GB ? 'hugemembw' : '')) }}
    beforeScript = 'module load singularity'

    withName: 'MMSEQS_COLABFOLDSEARCH' {{
        memory = 256.GB
    }}

    withLabel: 'process_gpu' {{
        queue = 'gpuvolta'
        cpus = 12
        gpus = 1
    }}
}}"""
