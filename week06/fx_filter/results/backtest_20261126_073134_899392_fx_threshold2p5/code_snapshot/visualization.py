"""캔들·진입/청산·FX 차단·계좌 경로를 실행 폴더의 PNG로 저장합니다.

스크린샷이나 실제로 존재하지 않는 해외 헤지 체결을 생성하지 않습니다.
"""
from __future__ import annotations

import csv
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

KST = timezone(timedelta(hours=9))
COLORS = {"up": "#188678", "down": "#cf5c5c", "long": "#086c58", "short": "#b93c46", "tp": "#df9c24", "stop": "#bd3548", "time": "#5c687b", "fx": "#7753aa", "missing": "#9aa4b2"}


def _rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def _number(value: Any) -> float | None:
    if value in (None, "", "None", "nan", "NaN"):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _first(row: Mapping[str, Any], names: Iterable[str]) -> Any:
    return next((row[name] for name in names if row.get(name) not in (None, "")), None)


def _time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        result = value
    elif value:
        try:
            result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return result.replace(tzinfo=KST) if result.tzinfo is None else result.astimezone(KST)


def _plot_modules():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    from matplotlib.patches import Rectangle
    fonts = {font.name for font in font_manager.fontManager.ttflist}
    plt.rcParams.update({"font.family": "Malgun Gothic" if "Malgun Gothic" in fonts else "DejaVu Sans", "axes.unicode_minus": False, "figure.dpi": 120, "savefig.dpi": 150, "axes.spines.top": False, "axes.spines.right": False})
    return plt, mdates, Rectangle


def _price(row: Mapping[str, Any], names: Iterable[str]) -> float | None:
    return _number(_first(row, names))


def _fx_kind(row: Mapping[str, Any]) -> str | None:
    if str(row.get("taken", "0")).lower() in {"1", "true", "yes"}:
        return None
    reason = str(_first(row, ("blocked_by", "block_reason", "reason")) or "")
    lowered = reason.lower()
    if any(term in lowered for term in ("fx_data", "fx_missing", "fx_stale", "fx_unavailable")) or ("해외" in reason and any(term in reason for term in ("부족", "없", "오래", "미수신", "자료"))):
        return "missing"
    if "fx" in lowered or "해외" in reason or "통화 충격" in reason:
        return "fx"
    return None


def _score(row: Mapping[str, Any], currency: str) -> float | None:
    return _price(row, (f"{currency}_z", f"fx_{currency}_z", f"z_{currency}", f"{currency}_score", f"fx_z_{currency}"))


def _annotate_trades(ax: Any, trades: list[dict[str, Any]], date: Any, mdates: Any, annotations: int, config: Mapping[str, Any], window: tuple[datetime, datetime]) -> None:
    counts: dict[str, int] = {}
    for row in trades:
        entry = _time(_first(row, ("entry_time", "entry_ts")))
        exit_t = _time(_first(row, ("exit_time", "exit_ts")))
        entry_p = _price(row, ("entry_price", "entry"))
        exit_p = _price(row, ("exit_price", "exit"))
        is_long = str(row.get("direction", row.get("side", ""))).upper() in {"LONG", "BUY", "1", "B"}
        if entry and entry.date() == date and window[0] <= entry <= window[1] and entry_p is not None:
            label = "Entry long" if is_long else "Entry short"
            ax.scatter(mdates.date2num(entry), entry_p, marker="^" if is_long else "v", s=72, color=COLORS["long" if is_long else "short"], edgecolors="white", linewidths=.7, zorder=7, label=label if not counts.get(label) else "_nolegend_")
            counts[label] = counts.get(label, 0) + 1
            if sum(counts.values()) <= annotations:
                ax.annotate(f"#{row.get('trade_no', '')} {'L' if is_long else 'S'}", (mdates.date2num(entry), entry_p), xytext=(0, -15 if is_long else 10), textcoords="offset points", fontsize=7, ha="center", color=COLORS["long" if is_long else "short"])
        if exit_t and exit_t.date() == date and window[0] <= exit_t <= window[1] and exit_p is not None:
            reason = str(row.get("exit_reason", "EXIT")).upper()
            if "STOP" in reason:
                category, marker, color = "Stop loss", "X", COLORS["stop"]
            elif "TP" in reason or "PROFIT" in reason:
                category, marker, color = "Midline exit (TP label)", "*", COLORS["tp"]
            elif "TIME" in reason:
                category, marker, color = "Time exit", "D", COLORS["time"]
            else:
                category, marker, color = "EOD / forced exit", "s", COLORS["time"]
            ax.scatter(mdates.date2num(exit_t), exit_p, marker=marker, s=80 if marker == "*" else 48, color=color, edgecolors="white", linewidths=.6, zorder=8, label=category if not counts.get(category) else "_nolegend_")
            counts[category] = counts.get(category, 0) + 1
            if entry and entry.date() == date and entry_p is not None:
                pnl = _price(row, ("net_pl", "net_pnl")) or 0
                ax.plot([mdates.date2num(entry), mdates.date2num(exit_t)], [entry_p, exit_p], color=COLORS["up" if pnl >= 0 else "down"], alpha=.42, lw=1, zorder=4)
                strategy = config.get("strategy", {})
                if strategy.get("stop_mode", "fixed") == "fixed":
                    stop_distance = float(strategy.get("stop_ticks", 5)) * float(strategy.get("tick_size", .1))
                    stop_price = entry_p - stop_distance if is_long else entry_p + stop_distance
                    label = "Planned stop (not guaranteed fill)"
                    ax.hlines(stop_price, mdates.date2num(entry), mdates.date2num(exit_t), color=COLORS["stop"], linestyle=":", lw=.7, alpha=.55, label=label if not counts.get(label) else "_nolegend_")
                    counts[label] = counts.get(label, 0) + 1


def _daily_candles(out: Path, config: Mapping[str, Any], bars: list[dict[str, Any]], trades: list[dict[str, Any]], signals: list[dict[str, Any]], diagnostics: list[dict[str, Any]]) -> list[Path]:
    plt, mdates, Rectangle = _plot_modules()
    minutes = int(config.get("strategy", {}).get("bar_minutes", 1))
    annotation_limit = int(config.get("reporting", {}).get("max_trade_annotations", 20))
    grouped: dict[Any, list[tuple[datetime, dict[str, Any]]]] = {}
    for row in bars:
        start = _time(_first(row, ("start", "bar_start", "timestamp", "ts")))
        if start is not None and all(_number(row.get(key)) is not None for key in ("open", "high", "low", "close")):
            grouped.setdefault(start.date(), []).append((start + timedelta(minutes=minutes), row))
    generated = []
    page_size = max(30, int(config.get("reporting", {}).get("chart_bars_per_page", 180)))
    pages = []
    for date, day_items in sorted(grouped.items()):
        day_items.sort(key=lambda item: item[0])
        count = math.ceil(len(day_items) / page_size)
        pages.extend((date, page + 1, count, day_items[page * page_size:(page + 1) * page_size]) for page in range(count))
    for date, page, count, items in pages:
        window = (items[0][0] - timedelta(minutes=minutes), items[-1][0] + timedelta(minutes=minutes))
        diag = [row for row in diagnostics if (ts := _time(_first(row, ("ts", "timestamp", "bar_end", "start")))) and window[0] <= ts <= window[1]]
        score_rows = diag or [row for row in signals if (ts := _time(row.get("ts"))) and window[0] <= ts <= window[1]]
        has_scores = any(_score(row, "eur") is not None or _score(row, "jpy") is not None for row in score_rows)
        if has_scores:
            fig, (ax, volume_ax, fx_ax) = plt.subplots(3, 1, figsize=(16, 10), sharex=True, gridspec_kw={"height_ratios": [5, 1, 1.6]}, layout="constrained")
        else:
            fig, (ax, volume_ax) = plt.subplots(2, 1, figsize=(16, 8), sharex=True, gridspec_kw={"height_ratios": [5, 1]}, layout="constrained")
            fx_ax = None
        xs = [mdates.date2num(ts) for ts, _ in items]
        candle_width = .65 * minutes / 1440
        lows = [float(row["low"]) for _, row in items]
        highs = [float(row["high"]) for _, row in items]
        body_floor = max((max(highs) - min(lows)) * .0006, .001)
        for x, (_, row) in zip(xs, items):
            op, hi, lo, close = (float(row[key]) for key in ("open", "high", "low", "close"))
            color = COLORS["up" if close >= op else "down"]
            ax.vlines(x, lo, hi, color=color, linewidth=.8, zorder=2)
            ax.add_patch(Rectangle((x - candle_width / 2, min(op, close)), candle_width, max(abs(close - op), body_floor), facecolor=color, edgecolor=color, linewidth=.5, zorder=3))
            volume = _number(row.get("volume"))
            if volume is not None:
                volume_ax.bar(x, volume, width=candle_width, color=color, alpha=.6)
        for column, label, color, style in (("mid", "BB midline", "#405a80", "-"), ("upper", "BB upper", "#9caec4", "--"), ("lower", "BB lower", "#9caec4", "--")):
            values = [_number(row.get(column)) for _, row in items]
            if any(value is not None for value in values):
                ax.plot(xs, [float("nan") if value is None else value for value in values], label=label, color=color, lw=1, linestyle=style, zorder=1)
        ypad = max(max(highs) - min(lows), .1) * .035
        _annotate_trades(ax, trades, date, mdates, annotation_limit, config, window)
        blocked_counts = {"fx": 0, "missing": 0}
        for row in signals:
            ts = _time(_first(row, ("ts", "timestamp")))
            kind = _fx_kind(row)
            price = _price(row, ("close", "price", "ref_price"))
            if not ts or not window[0] <= ts <= window[1] or kind is None or price is None:
                continue
            label = "FX filter blocked (no hedge order)" if kind == "fx" else "FX data unavailable: entry blocked"
            ax.scatter(mdates.date2num(ts), price, marker="x" if kind == "fx" else "|", s=58, color=COLORS[kind], linewidths=1.5, zorder=6, label=label if blocked_counts[kind] == 0 else "_nolegend_")
            blocked_counts[kind] += 1
        ax.set_title(f"USD/KRW futures · {date.isoformat()} · page {page}/{count} · candle + execution + FX entry filter", loc="left", fontsize=14, weight="bold")
        ax.set_ylabel("KRW per USD")
        ax.set_ylim(min(lows) - 2 * ypad, max(highs) + 2 * ypad)
        handles, labels = ax.get_legend_handles_labels()
        if handles:
            ax.legend(handles, labels, loc="upper left", ncol=3, fontsize=8, framealpha=.92)
        ax.grid(alpha=.13)
        volume_ax.set_ylabel("Volume")
        volume_ax.grid(axis="y", alpha=.12)
        if fx_ax is not None:
            for currency, color, label in (("eur", "#466fbd", "EUR: standardized USD strength"), ("jpy", "#c47b36", "JPY: standardized USD strength")):
                points = []
                for row in score_rows:
                    ts = _time(_first(row, ("ts", "timestamp", "bar_end", "start")))
                    value = _score(row, currency)
                    if ts and value is not None:
                        points.append((mdates.date2num(ts), value))
                points.sort()
                if points:
                    fx_ax.plot([point[0] for point in points], [point[1] for point in points], label=label, color=color, lw=1)
            threshold = _number(config.get("fx_filter", {}).get("threshold", config.get("fx_filter", {}).get("z_threshold", 2))) or 2
            for level in (threshold, -threshold):
                fx_ax.axhline(level, color=COLORS["fx"], linestyle="--", lw=.8)
            fx_ax.axhline(0, color="#9aa4b2", lw=.5)
            fx_ax.set_ylabel("FX score")
            handles, labels = fx_ax.get_legend_handles_labels()
            if handles:
                fx_ax.legend(loc="upper left", ncol=2, fontsize=8)
            fx_ax.grid(alpha=.13)
        bottom = fx_ax if fx_ax is not None else volume_ax
        bottom.xaxis.set_major_locator(mdates.MinuteLocator(byminute=[0, 30], tz=KST))
        bottom.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=KST))
        bottom.set_xlabel("Korea time (UTC+09:00). Candle timestamp = bar close; execution markers = recorded times.")
        bottom.set_xlim(mdates.date2num(window[0]), mdates.date2num(window[1]))
        path = out / (f"candles_{date:%Y%m%d}_p{page:02d}.png" if count > 1 else f"candles_{date:%Y%m%d}.png")
        fig.savefig(path, facecolor="white", bbox_inches="tight")
        plt.close(fig)
        generated.append(path)
    return generated


def _equity_plot(out: Path, equity: list[dict[str, Any]], trades: list[dict[str, Any]]) -> Path | None:
    plt, mdates, _ = _plot_modules()
    points = []
    label = "Recorded account equity"
    for row in equity:
        ts = _time(_first(row, ("ts", "timestamp", "time", "bar_end", "start")))
        value = _price(row, ("equity", "equity_krw", "net_liquidation", "balance", "cum_net_pl", "cumulative_pnl", "net_pnl", "pnl"))
        if ts and value is not None:
            points.append((ts, value))
    if equity and _first(equity[0], ("cum_net_pl", "cumulative_pnl", "net_pnl", "pnl")) is not None and _first(equity[0], ("equity", "equity_krw", "net_liquidation", "balance")) is None:
        label = "Recorded cumulative P&L"
    if not points and trades:
        cumulative = 0.
        for row in sorted(trades, key=lambda row: str(row.get("exit_time", ""))):
            ts = _time(_first(row, ("exit_time", "exit_ts")))
            pnl = _price(row, ("net_pl", "net_pnl"))
            if ts and pnl is not None:
                if not points:
                    points.append((_time(_first(row, ("entry_time", "entry_ts"))) or ts, 0.))
                cumulative += pnl
                points.append((ts, cumulative))
        label = "Closed-trade cumulative P&L (unrealized P&L excluded)"
    if not points:
        return None
    points.sort(key=lambda point: point[0])
    xs = [mdates.date2num(point[0]) for point in points]
    values = [point[1] for point in points]
    high = values[0]
    drawdowns = []
    for value in values:
        high = max(high, value)
        drawdowns.append(value - high)
    fig, (ax, dd) = plt.subplots(2, 1, figsize=(14, 6), sharex=True, gridspec_kw={"height_ratios": [2, 1]}, layout="constrained")
    ax.plot(xs, values, color=COLORS["up"], lw=1.5, label=label)
    ax.set_title("Account path and drawdown", loc="left", weight="bold")
    ax.set_ylabel("KRW")
    ax.legend(fontsize=8, loc="upper left")
    ax.grid(alpha=.15)
    dd.fill_between(xs, drawdowns, 0, color=COLORS["stop"], alpha=.22)
    dd.plot(xs, drawdowns, color=COLORS["stop"], lw=.8)
    dd.set_ylabel("Drawdown (KRW)")
    dd.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d\n%H:%M", tz=KST))
    dd.set_xlabel("Korea time (UTC+09:00)")
    dd.grid(alpha=.15)
    path = out / "equity.png"
    fig.savefig(path, facecolor="white", bbox_inches="tight")
    plt.close(fig)
    return path


def render_run(out: Path, config: Mapping[str, Any]) -> list[Path]:
    """실제 기록만 시각화합니다. 빈 거래에는 가상 마커를 만들지 않습니다."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    if (out / "variants_results.csv").exists():
        return render_comparison(out / "variants_results.csv", out)
    bars, signals, trades = (_rows(out / name) for name in ("bars.csv", "signals.csv", "trades.csv"))
    artifacts = _daily_candles(out, config, bars, trades, signals, _rows(out / "fx_diagnostics.csv")) if bars else []
    equity = _equity_plot(out, _rows(out / "equity.csv"), trades)
    if equity:
        artifacts.append(equity)
    return artifacts


def render_comparison(rows_or_csv: Any, out: Path) -> list[Path]:
    if isinstance(rows_or_csv, (str, Path)):
        rows = _rows(Path(rows_or_csv))
    elif hasattr(rows_or_csv, "to_dict"):
        rows = rows_or_csv.to_dict("records")
    else:
        rows = [dict(row) for row in rows_or_csv]
    if not rows:
        return []
    plt, _, _ = _plot_modules()
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    names = [str(_first(row, ("variant", "name", "run_id")) or index) for index, row in enumerate(rows)]
    measures = [(("net_pnl_krw", "net_pl", "net_pnl", "net_profit", "total_net_pl"), "Net P&L (KRW)"), (("max_drawdown_krw", "max_drawdown", "mdd"), "Maximum drawdown (KRW if configured)"), (("n_trades", "trades", "trade_count"), "Completed trades")]
    fig, axes = plt.subplots(1, 3, figsize=(17, max(4.5, len(rows) * .38)), layout="constrained")
    for ax, (aliases, title) in zip(axes, measures):
        for index, row in enumerate(rows):
            value = _price(row, aliases)
            if value is None:
                ax.text(0, index, "n/a", va="center", fontsize=8, color="#7b8794")
            else:
                color = COLORS["up"] if title.startswith("Net") and value >= 0 else COLORS["down"] if title.startswith("Net") else "#607e9c"
                ax.barh(index, value, color=color, height=.65)
                ax.annotate(f"{value:,.0f}", (value, index), xytext=(3 if value >= 0 else -3, 0), textcoords="offset points", va="center", ha="left" if value >= 0 else "right", fontsize=8)
        ax.set_yticks(range(len(names)), names, fontsize=8)
        ax.invert_yaxis()
        ax.set_title(title, loc="left", fontsize=11, weight="bold")
        ax.axvline(0, color="#8995a5", lw=.8)
        ax.grid(axis="x", alpha=.15)
        ax.margins(x=.25)
    fig.suptitle("Variants comparison · observed results, not an automatically selected live configuration", fontsize=12)
    path = out / "variant_comparison.png"
    fig.savefig(path, facecolor="white", bbox_inches="tight")
    plt.close(fig)
    return [path]
