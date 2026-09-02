from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.yaml"


def load_config(path: str | os.PathLike | None = None) -> dict:
    p = Path(path) if path else DEFAULT_CONFIG
    with open(p, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    cfg.setdefault("portal_url", "https://upbhulekh.gov.in/")
    cfg.setdefault("db_path", "data/bhulekh.sqlite")
    cfg.setdefault("output_dir", "output")
    cfg.setdefault("extracts_dir", "output/extracts")
    cfg.setdefault("old_fasli", False)
    cc = cfg.setdefault("concurrency", {})
    cc.setdefault("start", 6); cc.setdefault("max", 24); cc.setdefault("ramp_every_s", 45)
    cc.setdefault("backoff_factor", 0.5); cc.setdefault("min", 2)
    cc.setdefault("village_timeout_s", 40); cc.setdefault("retries", 3)
    for k in ("db_path", "output_dir", "extracts_dir"):
        if not os.path.isabs(cfg[k]):
            cfg[k] = str(ROOT / cfg[k])
    return cfg
