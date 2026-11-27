"""Offline paired replay: same interpreter/data/config, only _feature differs."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import platform
import shutil
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import yaml

import analyze
import backtest
from config import HERE


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class BufferedJournal:
    def __init__(self):
        self.rows = {}
        self.events = []

    def row(self, name, **row):
        self.rows.setdefault(name, []).append(row)

    def event(self, kind, message="", **extra):
        self.events.append(dict(kind=kind, message=message, **extra))

    def save(self, folder):
        for name, rows in self.rows.items():
            backtest._write_rows(folder / f"{name}.csv", rows)
        (folder / "events.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in self.events),
            encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", default=str(HERE / "generated_reslults"))
    parser.add_argument("--out", default=str(HERE / "generated_reslults/endpoint_revalidation"))
    args = parser.parse_args()
    archive = Path(args.archive).resolve()
    out = Path(args.out).resolve() / ("paired_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    out.mkdir(parents=True)
    legacy_path = archive / "backtest_20261126_072703_062052_fx_base/code_snapshot/analyze.py"
    spec = importlib.util.spec_from_file_location("legacy_endpoint_reference", legacy_path)
    legacy = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = legacy
    spec.loader.exec_module(legacy)

    class ContiguousFilter(analyze.FXShockFilter):
        _feature = legacy.FXShockFilter._feature

    snapshot = out / "code_snapshot"
    snapshot.mkdir()
    for source in ("revalidate_endpoints.py", "analyze.py", "backtest.py", "config.py"):
        shutil.copy2(HERE / source, snapshot / source)
    shutil.copy2(legacy_path, snapshot / "legacy_analyze.py")
    results, cache = [], {}
    original_filter = backtest.FXShockFilter
    try:
        for previous in sorted(archive.glob("backtest_*")):
            saved = json.loads((previous / "metrics.json").read_text(encoding="utf-8"))
            config = yaml.safe_load((previous / "run_config.yaml").read_text(encoding="utf-8"))["resolved_config"]
            data_name = Path(saved["input_data_directory"]).name
            data_dir = archive / data_name
            if data_name not in cache:
                cache[data_name] = {name: backtest.load_bars(data_dir / f"{name}.csv")
                                    for name in ("usdkrw", "eur", "jpy")}
            data = cache[data_name]
            lower = backtest._date_bound(config["data"].get("start"))
            upper = backtest._date_bound(config["data"].get("end"), end=True)
            bars = [b for b in data["usdkrw"] if (lower is None or b["start"] >= lower)
                    and (upper is None or b["start"] < upper)]
            hashes = {name: digest(data_dir / f"{name}.csv") for name in data}
            old_meta = json.loads((previous / "run_meta.json").read_text(encoding="utf-8"))
            assert all(hashes[n] == old_meta["data_inputs"][n]["sha256"] for n in hashes), "Input changed"
            pair = {"original_run": previous.name, "period": str(bars[0]["start"].date()),
                    "variant": saved["variant"], "archived": saved, "input_sha256": hashes}
            for mode, filter_class in (("before", ContiguousFilter), ("after", original_filter)):
                backtest.FXShockFilter = filter_class
                journal = BufferedJournal()
                folder = out / previous.name / mode
                folder.mkdir(parents=True)
                metrics = backtest.run_backtest(bars, config, SimpleNamespace(journal=journal),
                                               eur_bars=data["eur"], jpy_bars=data["jpy"])
                journal.save(folder)
                (folder / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
                (folder / "run_config.yaml").write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
                pair[mode] = metrics
                print(f"{saved['variant']} {pair['period']} {mode}: {metrics['n_trades']} trades, {metrics['net_pnl_krw']}", flush=True)
            results.append(pair)
    finally:
        backtest.FXShockFilter = original_filter
    metadata = {"python": sys.version, "platform": platform.platform(), "input_root": str(archive),
                "method": "same current backtest/Engine; before overrides only _feature with archived method",
                "actual_orders_sent": 0, "code_sha256": {f.name: digest(f) for f in snapshot.iterdir()},
                "pairs": results}
    (out / "comparison.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 연속 중간봉 제한 제거: 동일 환경 전후 비교", "",
             f"Python: `{platform.python_version()}`. 동일 데이터·설정·엔진에서 `_feature`만 바꾼 14쌍의 재생이다.",
             "기존 보관 파일은 변경하지 않았다. before/after 폴더에 거래·신호·FX 진단과 지표를 저장했다.", "",
             "| 기간 | 설정 | 거래 전→후 | 손익 전→후(원) | 손익 차이 | 자료 차단 전→후 | 방향 차단 전→후 |",
             "| --- | --- | ---: | ---: | ---: | ---: | ---: |"]
    for r in results:
        a,b=r["before"],r["after"]
        lines.append(f"| {r['period']} | [{r['variant']}]({r['original_run']}/after/metrics.json) | "
                     f"{a['n_trades']}→{b['n_trades']} | {a['net_pnl_krw']:,.0f}→{b['net_pnl_krw']:,.0f} | "
                     f"{b['net_pnl_krw']-a['net_pnl_krw']:+,.0f} | {a['fx_data_blocked_signals']}→{b['fx_data_blocked_signals']} | "
                     f"{a['fx_direction_blocked_signals']}→{b['fx_direction_blocked_signals']} |")
    lines += ["", "## 과거 보관 결과와 동일 환경 before의 차이", "",
              "아래 차이는 이번 제한 제거 효과가 아니다. 원자료 SHA-256은 기존 실행과 일치하며, 과거 실행 환경 차이의 원인은 별도 확인이 필요하다.", "",
              "| 기간 | 설정 | 보관 손익 | before 손익 |", "| --- | --- | ---: | ---: |"]
    for r in results:
        if r["archived"]["net_pnl_krw"] != r["before"]["net_pnl_krw"]:
            lines.append(f"| {r['period']} | {r['variant']} | {r['archived']['net_pnl_krw']:,.0f} | {r['before']['net_pnl_krw']:,.0f} |")
    lines += ["", "양 끝 봉의 가격·거래량, 정확한 시간 간격, 월물 일치, 신선도·동시성 조건은 유지한다. 가격을 임의로 채우지 않았다.",
              "수수료 0원·체결당 1틱(비용 후보만 2틱)의 과거 봉 재생이며 실제 본 실행을 다시 수행한 결과가 아니다."]
    (out / "summary.md").write_text("\n".join(lines)+"\n", encoding="utf-8")
    print(out / "summary.md", flush=True)


if __name__ == "__main__":
    main()
