"""Run manifests: what produced a run (code, config, data, environment), without secrets or conversation text."""
from __future__ import annotations

import json
import platform
import subprocess
import sys
import time
from pathlib import Path

from .config import Config
from .data.loader import fingerprint


def git_state(repo: Path) -> dict:
    def run(*args):
        try:
            return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""
    return {"commit": run("rev-parse", "HEAD"), "branch": run("rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": bool(run("status", "--porcelain", "--untracked-files=no"))}


def build_manifest(cfg: Config, files: dict[str, Path], extra: dict | None = None) -> dict:
    repo = Path(__file__).resolve().parents[2]
    from .reactor.message import official
    m = {
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "git": git_state(repo),
        "config_name": cfg.name, "config_hash": cfg.hash(), "retrieval_mode": cfg.mode(), "config": cfg.to_dict(),
        "data": {name: {"path": str(p), "sha256_16": fingerprint(p)} for name, p in files.items() if p and Path(p).exists()},
        "models": {"dense": cfg.dense.model_id + "@" + cfg.dense.revision if cfg.dense.enabled else None,
                   "rerank": cfg.rerank.model_id + "@" + cfg.rerank.revision if cfg.rerank.enabled else None},
        "window_version": cfg.window.version,
        "message_builder": "official reactor/client.py" if official() is not None else "mirror",
        "seeds": "none (deterministic pipeline)",
        "environment": {"python": sys.version.split()[0], "platform": platform.platform(), "machine": platform.machine()},
    }
    if extra:
        m.update(extra)
    return m


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)
