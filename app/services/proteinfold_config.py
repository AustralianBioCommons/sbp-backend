"""Proteinfold workflow configuration and executor settings (modeled after bindflow).
"""
from __future__ import annotations

from typing import Any

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
) -> dict[str, Any]:
    """Get default parameters for proteinfold workflow."""
    return {
        "outdir": out_dir,
        "input": samplesheet_url,
        "db": _DB_BASE,
        "alphafold2_db": _DB_BASE,
        "alphafold2_bfd_path": f"{_DB_BASE}/bfd/*",
        "alphafold2_small_bfd_path": f"{_DB_BASE}/small_bfd/*",
        "alphafold2_params_path": f"{_DB_BASE}/params/alphafold_params_2022-12-06/*",
        "alphafold2_mgnify_path": f"{_DB_BASE}/mgnify/*",
        "alphafold2_pdb70_path": f"{_DB_BASE}/pdb70/**",
        "alphafold2_pdb_mmcif_path": f"{_DB_BASE}/pdb_mmcif/mmcif_files",
        "alphafold2_pdb_obsolete_path": f"{_DB_BASE}/pdb_mmcif/obsolete.dat",
        "alphafold2_uniref30_path": f"{_DB_BASE}/uniref30/*",
        "alphafold2_uniref90_path": f"{_DB_BASE}/uniref90/*",
        "alphafold2_pdb_seqres_path": f"{_DB_BASE}/pdb_seqres/*",
        "alphafold2_uniprot_path": f"{_DB_BASE}/uniprot/*",
        "colabfold_db": _DB_BASE,
        "colabfold_envdb_path": f"{_DB_BASE}/colabfold_envdb/*",
        "colabfold_uniref30_path": f"{_DB_BASE}/colabfold_uniref30/*",
        "colabfold_alphafold2_params_path": f"{_DB_BASE}/params/alphafold_params_2022-12-06",
        "boltz_db": _DB_BASE,
        "boltz_ccd_path": f"{_DB_BASE}/params/ccd.pkl",
        "boltz_model_path": f"{_DB_BASE}/params/boltz1_conf.ckpt",
        "boltz2_aff_path": f"{_DB_BASE}/params/boltz2_aff.ckpt",
        "boltz2_conf_path": f"{_DB_BASE}/params/boltz2_conf.ckpt",
        "boltz2_mols_path": f"{_DB_BASE}/params/mols/",
        "project": "yz52",
        "mode": mode,
        "use_gpu": True,
        "colabfold_alphafold2_params_tags": dict(_COLABFOLD_ALPHAFOLD2_PARAMS_TAGS),
    }


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
// Enable use of Singularity to run containers
singularity {{
    enabled = true
    autoMounts = true
    cacheDir = "{_SINGULARITY_CACHE_DIR}"
}}

executor {{
    queueSize = 300
    pollInterval = '5 min'
    queueStatInterval = '5 min'
    submitRateLimit = '20 min'
}}

// Define process resource limits
process {{
    executor = 'pbspro'
    storage = "gdata/ll61+gdata/if89+gdata/li87"
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
}}

// Write custom trace file with outputs required for SU calculation
def trace_timestamp = new java.util.Date().format('yyyy-MM-dd_HH-mm-ss')
trace {{
    enabled = true
    overwrite = false
    file = "./gadi-nf-core-trace-${{trace_timestamp}}.txt"
    fields = 'name,status,exit,duration,realtime,cpus,%cpu,memory,%mem,rss'
}}"""
