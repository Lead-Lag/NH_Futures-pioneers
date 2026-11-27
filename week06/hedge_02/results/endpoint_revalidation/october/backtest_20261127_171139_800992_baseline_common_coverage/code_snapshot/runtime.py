"""Run provenance, durable CSV journals, configuration snapshots and reports."""
from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from config import HERE, config_hash, project_path

KST = timezone(timedelta(hours=9))
SENSITIVE = re.compile(r"token|secret|authorization|access_key|app_key|account|acnt|acc_no", re.I)


def safe_value(value: Any, key: str = "") -> Any:
    if SENSITIVE.search(key) and not key.endswith(("_count", "_calls")) and key != "account_orders_sent":
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): safe_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_value(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "item"):
        try:
            return value.item()
        except (ValueError, TypeError):
            pass
    if isinstance(value, str):
        value = re.sub(r"(?i)(access_key|access_token|session_token)=([^&\s]+)", r"\1=[REDACTED]", value)
        value = re.sub(r"(?i)Bearer\s+[^\s,\"']+", "Bearer [REDACTED]", value)
    return value


def powershell_command(argv: list[str] | None = None) -> str:
    args = ["python", *(sys.argv if argv is None else argv)]
    def quote(s: str) -> str:
        return "'" + s.replace("'", "''") + "'" if re.search(r"\s|['\"`$;&|]", s) else s
    return " ".join(quote(str(a)) for a in args)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def git_provenance() -> dict:
    try:
        def git(*args: str) -> str:
            result = subprocess.run(["git", "-C", str(HERE), *args], capture_output=True,
                                    text=True, encoding="utf-8", errors="replace", timeout=5)
            if result.returncode:
                raise ValueError("git metadata unavailable")
            return result.stdout.strip()
        changes = git("status", "--porcelain", "--untracked-files=no")
        return {"commit": git("rev-parse", "HEAD"), "tracked_dirty": bool(changes),
                "tracked_changes": changes.splitlines(), "note": "Source snapshots include uncommitted implementation files."}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"commit": None, "note": "Git unavailable; use code_snapshot hashes."}


class Journal:
    def __init__(self, out: Path):
        self.out = out
        self.out.mkdir(parents=True, exist_ok=True)
        self.now: datetime | None = None
        self._rows: dict[str, list[dict]] = {}
        self._fields: dict[str, list[str]] = {}
        self._lock = threading.RLock()

    def row(self, name: str, row: dict | None = None, **kwargs) -> None:
        record = safe_value({**(row or {}), **kwargs})
        record = {k: json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (dict, list)) else v
                  for k, v in record.items()}
        with self._lock:
            fields = self._fields.setdefault(name, [])
            new_fields = [k for k in record if k not in fields]
            fields.extend(new_fields)
            records = self._rows.setdefault(name, [])
            records.append(record)
            path = self.out / f"{name}.csv"
            rewrite = bool(new_fields) or not path.exists()
            with path.open("w" if rewrite else "a", encoding="utf-8-sig" if rewrite else "utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
                if rewrite:
                    writer.writeheader()
                    writer.writerows(records)
                else:
                    writer.writerow(record)

    def event(self, kind: str | dict, message: str = "", **kwargs) -> None:
        if isinstance(kind, dict):
            record = dict(kind)
        else:
            record = {"kind": kind, "message": message, **kwargs}
        record.setdefault("ts", (self.now or datetime.now(KST)).isoformat())
        with self._lock:
            with (self.out / "events.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(safe_value(record), ensure_ascii=False, default=str) + "\n")

    def bar(self, row: dict): self.row("bars", row)
    def signal(self, row: dict): self.row("signals", row)
    def order(self, row: dict): self.row("orders", row)
    def fill(self, row: dict): self.row("fills", row)
    def trade(self, row: dict): self.row("trades", row)
    def api(self, row: dict): self.row("api_calls", row)
    def meta(self, row: dict):
        (self.out / "engine_meta.json").write_text(json.dumps(safe_value(row), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    def close(self): pass


class RunContext:
    def __init__(self, kind: str, variant: str, config: dict, source_config: dict,
                 config_path: str | Path, argv=None, out_root=None):
        self.config = config
        self.source_config = source_config
        self.started_at = datetime.now(KST).isoformat(timespec="milliseconds")
        stamp = datetime.now(KST).strftime("%Y%m%d_%H%M%S_%f")
        root = project_path(out_root or config.get("reporting", {}).get("results_dir", "results"))
        self.out = root / f"{kind}_{stamp}_{re.sub(r'[^A-Za-z0-9_.-]', '_', variant)}"
        self.out.mkdir(parents=True, exist_ok=False)
        self.journal = Journal(self.out)
        self.metadata = {"run_id": self.out.name, "kind": kind, "variant": variant,
                         "started_at": self.started_at, "command": powershell_command(argv),
                         "reconstructed_command": True, "config_source": str(Path(config_path).resolve()),
                         "config_hash": config_hash(config), "git": git_provenance(), "status": "running",
                         "tests_executed": False, "actual_hedge_orders": False}
        snapshot = self.out / "code_snapshot"
        snapshot.mkdir()
        hashes = {}
        for path in sorted(HERE.glob("*.py")):
            shutil.copy2(path, snapshot / path.name)
            hashes[path.name] = sha256_file(path)
        self.metadata["code_hashes"] = hashes
        (self.out / "strategy_config.yaml").write_text(yaml.safe_dump(source_config, allow_unicode=True, sort_keys=False), encoding="utf-8")
        self._write_manifest()

    def _write_manifest(self):
        current_hash = config_hash(self.config)
        if self.metadata.get("config_hash") != current_hash:
            self.metadata.setdefault("initial_config_hash", self.metadata.get("config_hash"))
            self.metadata["config_hash"] = current_hash
        snapshot = {"metadata": safe_value(self.metadata), "resolved_config": safe_value(self.config)}
        (self.out / "run_config.yaml").write_text(yaml.safe_dump(snapshot, allow_unicode=True, sort_keys=False), encoding="utf-8")
        (self.out / "run_meta.json").write_text(json.dumps(safe_value(self.metadata), ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    def finish(self, metrics: dict | None = None, status="completed", notes: list | None = None):
        self.metadata.update(status=status, ended_at=datetime.now(KST).isoformat(timespec="milliseconds"))
        self._write_manifest()
        metrics = safe_value(metrics or {})
        (self.out / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        notes = list(notes or [])
        artifacts = []
        try:
            from visualization import render_run
            artifacts = render_run(self.out, self.config)
        except Exception as exc:
            from error import classify_error
            err = classify_error(exc, operation="render_report")
            self.journal.event("REPORT_ARTIFACT_ERROR", str(err))
            notes.append("PNG artifact generation failed: " + str(err))
        from summarize import write_summary
        write_summary(self.out, self.metadata, self.config, metrics, artifacts, notes)
        self.journal.close()


def create_run(kind, variant, config, source_config, config_path, argv=None, out_root=None) -> RunContext:
    return RunContext(kind, variant, config, source_config, config_path, argv, out_root)
