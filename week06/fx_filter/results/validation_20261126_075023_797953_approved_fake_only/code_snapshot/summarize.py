"""실행별 YAML 설정, 재현 명령, 분석 산출물을 summary.md로 연결합니다."""
from __future__ import annotations

import csv
import json
import math
import os
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

_SECRET_KEYS = {"app_key", "app_secret", "secret_key", "client_secret", "access_token", "refresh_token", "authorization", "account_no", "account_number", "password", "access_key", "api_key"}
_LABELS = {"trades": "완료 거래 수", "trade_count": "완료 거래 수", "n_trades": "완료 거래 수", "net_pl": "순손익(원)", "net_pnl": "순손익(원)", "gross_pl": "비용 전 손익(원)", "gross_pnl": "비용 전 손익(원)", "fees": "수수료(원)", "fee": "수수료(원)", "win_rate": "승률", "profit_factor": "Profit factor", "max_drawdown": "최대낙폭", "max_drawdown_krw": "최대낙폭(원)", "max_drawdown_pct": "최대낙폭(%)", "signals": "기존 진입 신호 수", "signal_count": "기존 진입 신호 수", "fx_blocked": "해외 통화 충격 차단 수", "fx_data_blocked": "해외 자료 부재 차단 수", "data_blocked": "자료 부재 차단 수", "avg_trade": "거래당 순손익(원)", "start": "분석 시작", "end": "분석 종료", "status": "완료 상태", "variant": "실행 설정명"}
_LABELS.update({"n_bars": "분봉 수", "n_days": "거래일 수", "n_signals": "기존 진입 신호 수",
                "gross_pnl_krw": "비용 전 손익(원)", "fees_krw": "수수료(원)", "net_pnl_krw": "순손익(원)",
                "initial_equity_krw": "초기 계좌자산(원)", "max_drawdown_fraction": "최대낙폭 비율(소수)",
                "average_trade_krw": "거래당 순손익(원)", "worst_day_krw": "최악 일손익(원)",
                "worst_trade_krw": "최악 거래 손익(원)", "fx_valid_signals": "해외 자료 판단 가능 신호 수",
                "fx_valid_days": "해외 자료 판단 가능 거래일 수", "fx_coverage_fraction": "해외 자료 판단 가능 비율(소수)",
                "fx_blocked_signals": "해외 필터 차단 총수(자료 부족 포함)",
                "fx_data_blocked_signals": "해외 자료 부족 차단 수", "fx_direction_blocked_signals": "해외 충격 역행 차단 수"})


def _clean(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): ("[REDACTED]" if str(k).lower() in _SECRET_KEYS else _clean(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        value = re.sub(r"(?i)Bearer\s+[^\s\"']+", "Bearer [REDACTED]", value)
        return re.sub(r"(?i)(access_key|access_token|app_secret|app_key)=([^&\s]+)", r"\1=[REDACTED]", value)
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


def _yaml(value: Any) -> str:
    return yaml.safe_dump(_clean(value), allow_unicode=True, sort_keys=False, default_flow_style=False).rstrip()


def _cell(value: Any) -> str:
    value = _clean(value)
    if value is None:
        return "미집계"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float):
        return f"{value:,.6g}" if math.isfinite(value) else str(value)
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, (list, dict)):
        value = json.dumps(value, ensure_ascii=False, default=str)
    return str(value).replace("|", "\\|").replace("\n", "<br>")


def _metrics_rows(metrics: Mapping[str, Any], prefix: str = "") -> list[tuple[str, Any]]:
    rows: list[tuple[str, Any]] = []
    for key, value in metrics.items():
        name = f"{prefix}.{key}" if prefix else str(key)
        rows.extend(_metrics_rows(value, name)) if isinstance(value, Mapping) else rows.append((name, value))
    return rows


def _relative(out: Path, item: Any) -> tuple[str, str]:
    if isinstance(item, Mapping):
        item = item.get("path", item.get("file", ""))
    path = Path(item)
    target = path if path.is_absolute() else out / path
    try:
        link = target.resolve().relative_to(out.resolve()).as_posix()
    except ValueError:
        try:
            link = Path(os.path.relpath(target.resolve(), out.resolve())).as_posix()
        except ValueError:
            link = target.resolve().as_posix()
    return target.name, link


def _artifacts(out: Path, artifacts: Iterable[Any] | None) -> list[str]:
    parts = ["## 3. 분석 결과와 산출물", ""]
    images: list[str] = []
    seen: set[str] = set()
    for item in artifacts or []:
        if not item:
            continue
        label, link = _relative(out, item)
        if link in seen:
            continue
        seen.add(link)
        if label.lower().endswith((".png", ".jpg", ".jpeg", ".svg")):
            images.extend([f"### {label}", "", f"![{label}](<{link}>)", ""])
        else:
            parts.extend([f"- [{label}](<{link}>)", ""])
    parts.extend(images)
    if not images:
        parts.extend(["차트가 없습니다. 입력 자료가 없거나 차트 생성이 완료되지 않은 실행인지 확인하십시오.", ""])
    names = ["run_config.yaml", "strategy_config.yaml", "metrics.json", "bars.csv", "signals.csv", "fx_diagnostics.csv", "trades.csv", "equity.csv", "orders.csv", "fills.csv", "api_calls.csv", "events.jsonl", "data_manifest.json", "run_meta.json"]
    present = [name for name in names if (out / name).exists()]
    if present:
        parts.extend(["### 원본 기록", ""])
        parts.extend(f"- [{name}](<{name}>)" for name in present)
        parts.append("")
    return parts


def _comparison_table(out: Path, records: list[dict[str, Any]]) -> list[str]:
    keys = [key for key in ("variant", "status", "n_trades", "trades", "net_pnl_krw", "net_pl", "max_drawdown_krw", "fx_valid_signals", "fx_direction_blocked_signals", "fx_data_blocked_signals") if any(key in row for row in records)]
    if not records or not keys:
        return []
    lines = ["### 설정별 비교", "", "| " + " | ".join(_cell(_LABELS.get(key, key)) for key in keys) + " | 설정·분석 연결 |", "| " + " | ".join("---" for _ in keys) + " | --- |"]
    for row in records:
        summary = row.get("summary")
        if not summary and row.get("run_directory"):
            summary = Path(row["run_directory"]) / "summary.md"
        if summary:
            _, link = _relative(out, summary)
            link_cell = f"[개별 summary](<{link}>)"
        else:
            link_cell = "미기록"
        lines.append("| " + " | ".join(_cell(row.get(key)) for key in keys) + " | " + link_cell + " |")
    return lines + [""]


def write_summary(out: Path, metadata: Mapping[str, Any], config: Mapping[str, Any], metrics: Mapping[str, Any], artifacts: Iterable[Any] | None = None, notes: Iterable[str] | None = None) -> Path:
    """실제 적용 설정을 표시합니다. 실행하지 않은 분석이나 가상 체결은 만들지 않습니다."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    variant = metadata.get("variant", metrics.get("variant", "base"))
    lines = [f"# {metadata.get('kind', 'run')} · {variant}", "", "## 실행 식별", "", "| 항목 | 값 |", "| --- | --- |"]
    for key, label in (("run_id", "실행 ID"), ("started_at", "실행 시작(시간대 포함)"), ("ended_at", "실행 종료(시간대 포함)"), ("status", "상태"), ("variant", "variant"), ("config_hash", "설정 SHA-256")):
        if key in metadata:
            lines.append(f"| {label} | {_cell(metadata[key])} |")
    command = metadata.get("command", metadata.get("command_line", "미기록"))
    lines.extend(["", "## 1. PowerShell 명령어", ""])
    if metadata.get("reconstructed_command", True):
        lines.extend(["아래 명령은 실행 인자에서 재구성한 재현용 명령입니다. PowerShell 입력 이력을 직접 읽은 값은 아닙니다.", ""])
    lines.extend(["```powershell", str(_clean(command)), "```", "", "## 2. 이번 실행에 실제 적용된 YAML 설정", "", "설정 원본의 `base`와 선택한 `variant`를 병합한 결과입니다. 원본이 이후 바뀌어도 이 실행의 스냅샷은 유지합니다.", "", "```yaml", _yaml(config), "```", ""])
    for name in ("run_config.yaml", "strategy_config.yaml"):
        if (out / name).exists():
            lines.append(f"- [{name}](<{name}>)")
    if metadata.get("git") is not None:
        lines.extend(["", "### 소스 상태", "", "```yaml", _yaml(metadata["git"]), "```", ""])
    lines.extend(["", "### 분석 지표", "", "| 지표 | 값 |", "| --- | --- |"])
    rows = _metrics_rows({key: value for key, value in metrics.items() if key != "variants"})
    if rows:
        lines.extend(f"| {_cell(_LABELS.get(key, key))} | {_cell(value)} |" for key, value in rows)
    else:
        lines.append("| 분석 결과 | 미집계: 실패·중단·자료 부족 여부를 확인하십시오. |")
    lines.append("")
    if isinstance(metrics.get("variants"), list):
        lines.extend(["이 비교 실행의 YAML은 공통 base 설정입니다. 각 행의 실제 적용 설정은 개별 summary와 run_config.yaml을 확인하십시오.", ""])
        lines.extend(_comparison_table(out, metrics["variants"]))
    if metrics.get("fx_valid_signals") == 0:
        lines.extend(["**해외 통화 충격을 판단할 수 있는 신호가 0건입니다.** 자료 부족으로 거래하지 않은 결과를 경제적 필터가 손실을 회피한 증거로 해석하지 마십시오.", ""])
    lines.extend(_artifacts(out, artifacts))
    lines.extend(["## 해석 범위", "", "- 해외 유로·엔 상품은 관측용입니다. 이 전략은 해외 헤지 주문을 보내지 않습니다.", "- `FX filter blocked`는 해외 통화 충격에 역행하는 진입 보류입니다. 실제 헤지 체결 표시가 아닙니다.", "- 충격 차단과 해외 자료 미수신·부족 차단을 분리해 해석해야 합니다.", "- 손실 한도·대기시간·보유 상태를 포함해 전체 엔진을 다시 실행한 결과로 비교해야 합니다.", "- 본 실행 손익과 백테스트 가정 체결은 별개의 결과입니다.", "- 별도 자동 테스트는 사용자 추가 승인 후 가짜 응답과 네트워크 차단 환경에서만 실행합니다. validation 실행 결과와 구분하십시오.", ""])
    strategy = config.get("strategy", {})
    if isinstance(strategy, Mapping) and strategy.get("fee_per_contract") == 0:
        lines.extend(["**수수료 설정이 0원입니다.** 수수료를 포함한 실제 비용 후 성과로 일반화하지 마십시오.", ""])
    sources = metadata.get("data_sources", metadata.get("data"))
    if sources:
        lines.extend(["### 입력 자료", "", "```yaml", _yaml(sources), "```", ""])
    metadata_notes = metadata.get("notes", []) or []
    if isinstance(metadata_notes, str):
        metadata_notes = [metadata_notes]
    combined = list(metadata_notes) + list(notes or [])
    if combined:
        lines.extend(["### 실행 메모", ""])
        lines.extend(f"- {_cell(note)}" for note in dict.fromkeys(str(n) for n in combined))
        lines.append("")
    path = out / "summary.md"
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path


def _read_rows(rows: Any) -> list[dict[str, Any]]:
    if isinstance(rows, (str, Path)):
        with Path(rows).open(encoding="utf-8-sig", newline="") as stream:
            return list(csv.DictReader(stream))
    return rows.to_dict("records") if hasattr(rows, "to_dict") else [dict(row) for row in rows]


def write_batch_summary(out: Path, rows: Any, metadata: Mapping[str, Any], source_config: Mapping[str, Any], artifacts: Iterable[Any] | None = None) -> Path:
    """개별 실행 링크를 모으며, 최고 수익 설정을 자동 확정하지 않습니다."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    records = _read_rows(rows)
    preferred = ["variant", "status", "trades", "trade_count", "n_trades", "net_pl", "net_pnl", "net_pnl_krw", "max_drawdown_krw", "max_drawdown", "fx_valid_signals", "fx_direction_blocked_signals", "fx_data_blocked_signals", "summary", "run_directory"]
    keys = [key for key in preferred if any(key in row for row in records)] or list(dict.fromkeys(key for row in records for key in row))
    lines = ["# 백테스트 variants 비교", "", "## 1. PowerShell 명령어", "", "실행 인자에서 재구성한 재현용 명령입니다.", "", "```powershell", str(_clean(metadata.get("command", metadata.get("command_line", "미기록")))), "```", "", f"- 실행 시작: {_cell(metadata.get('started_at'))}", "", "## 2. YAML 원본: base와 variants", "", "```yaml", _yaml(source_config), "```", "", "## 3. 분석 결과", ""]
    if records and keys:
        lines.extend(["| " + " | ".join(_cell(k) for k in keys) + " |", "| " + " | ".join("---" for _ in keys) + " |"])
        for row in records:
            cells = []
            for key in keys:
                value = row.get(key)
                if key in {"summary", "run_directory"} and value:
                    _, link = _relative(out, Path(value) / "summary.md" if key == "run_directory" else value)
                    cells.append(f"[summary](<{link}>)")
                else:
                    cells.append(_cell(value))
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")
    else:
        lines.extend(["완료된 비교 결과가 없습니다. 각 실행의 실패·차단 사유를 확인하십시오.", ""])
    lines.extend(_artifacts(out, artifacts))
    lines.extend(["## 설정 선택에 대한 제한", "", "- 이 표는 관측 결과 비교입니다. 과거 수익이 가장 큰 설정을 본 실행 최적값으로 자동 채택하지 않습니다.", "- 자료 범위가 다른 실행은 동일 표본으로 보지 않습니다. 공통 자료 범위 대조군을 확인하십시오.", "- 해외 필터의 추가 정보는 기존 추세 필터·거래 수 감소·비용 민감도와 함께 평가해야 합니다.", "- 개별 summary에서 설정 스냅샷·차단 사유·계좌 경로·거래 기록을 확인하십시오.", ""])
    path = out / "summary.md"
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path
