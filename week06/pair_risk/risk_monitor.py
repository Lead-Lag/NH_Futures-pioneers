"""6주차 — 실행 중 감시(모니터링)와 「위험관리 넣기 전·후」 그림자 비교.

ShadowBook  같은 실시간 시세·같은 1분봉을 받아 **주문 없이** 다른 규칙(R0 위험관리 없음 · R1 4주차 · R2 6주차)을
            함께 돌립니다. 체결은 판정 순간의 실시간 1호가(사면 매도호가, 팔면 매수호가) — 시장가 주문과 같은 가정.
            세 규칙이 같은 시세·같은 체결 가정이라 「같은 기간·같은 조건」 비교가 됩니다. 기록: results/<run>/shadow_<이름>/
Monitor     실제 주문 엔진을 지키는 감시 규칙 (설정 strategy_config.json 의 "monitor")
            ① 실시간 끊김 동안 신규 진입 금지              (block_entry_when_ws_down)
            ② 보유 중 시세가 N초 동안 하나도 안 들어오면 청산 (blind_exit_sec — 볼 수 없는 포지션은 들고 있지 않음)
            ③ 한쪽 다리만 체결된 시간이 길면 경고, 반복되면 신규 진입 중지 (max_exposure_ms · exposure_breaches_halt)
            ④ PC−거래소 시각 차이가 크면 경고                (max_clock_offset_sec)
            ⑤ 1분마다 상태 한 줄(보유·평가손익·하루 손익·z·실시간 상태·그림자 손익)  (heartbeat_sec)
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from journal import RunJournal
from pair_engine import BUY, COMPLETE, EXIT_MONITOR, PairEngine, PairIntent, PairParams


class _QuoteFill:
    """그림자 체결: 실시간 1호가로 시장가 체결 가정 (pair_broker.PairOutcome 과 같은 칸)."""

    def __init__(self, it: PairIntent, quotes: dict[str, dict], fallback: dict[str, float]):
        self.status, self.qty, self.avg, self.unwind_pnl = COMPLETE, {}, {}, 0.0
        self.message = "그림자 체결(실시간 1호가)"
        for leg in it.legs:
            q = quotes.get(leg.name) or {}
            px = q.get("ask") if leg.side == BUY else q.get("bid")
            if not px:
                px = q.get("last") or it.ref.get(leg.name) or fallback.get(leg.name, 0.0)
            self.qty[leg.name] = leg.qty
            self.avg[leg.name] = float(px)


class ShadowBook:
    def __init__(self, name: str, params: PairParams, folder: Path) -> None:
        self.name = name
        self.j = RunJournal(Path(folder) / f"shadow_{name}", echo=False)
        self.eng = PairEngine(params, self.j)
        self.last: dict[str, float] = {}

    def _handle(self, it: PairIntent | None, quotes: dict[str, dict], ts: datetime) -> None:
        if it is not None:
            self.eng.on_pair_done(it, _QuoteFill(it, quotes, self.last), ts)

    def on_bar(self, row: dict[str, Any], quotes: dict[str, dict], ts: datetime, stale: bool = False) -> None:
        self.last = {"S": row["close_s"], "H": row["close_h"]}
        if stale:
            self.eng.warmup = True
            self.eng.on_bar_close(row)
            self.eng.warmup = False
            return
        self._handle(self.eng.on_bar_close(row), quotes, ts)

    def on_prices(self, ps: float, ph: float, ts: datetime, quotes: dict[str, dict]) -> None:
        self._handle(self.eng.on_prices(ps, ph, ts), quotes, ts)

    def on_clock(self, ts: datetime, quotes: dict[str, dict]) -> None:
        self._handle(self.eng.on_clock(ts), quotes, ts)

    def finish(self, ts: datetime, quotes: dict[str, dict]) -> None:
        self._handle(self.eng.force_exit(ts, "실행 종료 시 정리"), quotes, ts)

    def pnl(self, ps: float = 0.0, ph: float = 0.0) -> tuple[float, float, int]:
        """(확정 하루 손익, 보유 평가손익, 오늘 포지션 수)."""
        open_ = self.eng.mtm(ps, ph) if (self.eng.direction and ps > 0 and ph > 0) else 0.0
        return self.eng.daily_pnl, open_, self.eng.daily_trades

    def close(self) -> None:
        self.j.close()


class Monitor:
    def __init__(self, cfg: dict[str, Any], journal: Any, ws_enabled: bool) -> None:
        self.blind_exit_sec = float(cfg.get("blind_exit_sec", 30))
        self.max_exposure_ms = float(cfg.get("max_exposure_ms", 3000))
        self.breaches_halt = int(cfg.get("exposure_breaches_halt", 2))
        self.heartbeat_sec = float(cfg.get("heartbeat_sec", 60))
        self.block_ws_down = bool(cfg.get("block_entry_when_ws_down", True)) and ws_enabled
        self.max_clock = float(cfg.get("max_clock_offset_sec", 2.0))
        self.j = journal
        self.last_quote: datetime | None = None
        self.blind_fired = False
        self.breaches = 0
        self.next_beat: datetime | None = None
        self.clock_warned = False
        self.counts = {"entry_blocked_ws": 0, "blind_exit": 0, "exposure_breach": 0, "clock_warn": 0}
        self._was_blocked = False

    # ① 실시간 상태 → 진입 막기
    def entry_gate(self, eng: PairEngine, live: bool) -> None:
        block = self.block_ws_down and not live
        eng.monitor_block = "실시간 끊김(신규 진입 금지)" if block else ""
        if block and not self._was_blocked:
            self.counts["entry_blocked_ws"] += 1
            self.j.event("monitor", "실시간 끊김 — 다시 연결될 때까지 신규 진입 금지(보유 청산 판정은 REST 시세로 계속)")
        elif not block and self._was_blocked:
            self.j.event("monitor", "실시간 복구 — 신규 진입 허용")
        self._was_blocked = block

    # ② 시세 공백 감시
    def saw_quote(self, now: datetime) -> None:
        self.last_quote = now
        if self.blind_fired:
            self.j.event("monitor", "시세 수신 재개")
        self.blind_fired = False

    def blind_exit(self, eng: PairEngine, now: datetime) -> PairIntent | None:
        """보유 중인데 blind_exit_sec 동안 어떤 시세도(실시간·REST) 없으면 청산 Intent 를 한 번 냄."""
        if not eng.direction or eng.pending or self.blind_fired or self.last_quote is None:
            return None
        gap = (now - self.last_quote).total_seconds()
        if gap < self.blind_exit_sec:
            return None
        self.blind_fired = True
        self.counts["blind_exit"] += 1
        self.j.event("monitor", f"보유 중 시세 공백 {gap:.0f}초 ≥ {self.blind_exit_sec:.0f}초 — 감시할 수 없는 포지션 청산")
        return eng.force_exit(now, f"감시 공백 {gap:.0f}초", purpose=EXIT_MONITOR)

    # ③ 한쪽만 체결된 시간
    def after_pair(self, eng: PairEngine, outcome: Any) -> None:
        ms = getattr(outcome, "exposure_ms", "")
        if ms == "" or ms is None:
            return
        if float(ms) > self.max_exposure_ms:
            self.breaches += 1
            self.counts["exposure_breach"] += 1
            self.j.event("monitor", f"페어 {outcome.pair_id} 한쪽만 체결된 시간 {ms}ms > {self.max_exposure_ms:.0f}ms "
                                    f"(오늘 {self.breaches}번째)")
            if self.breaches >= self.breaches_halt and not eng.halted:
                eng.halted = f"한쪽 체결 지연 {self.breaches}회"
                self.j.event("risk", f"한쪽 체결 지연이 {self.breaches}번 — 신규 진입 중지(청산 판정은 계속)")

    # ④·⑤ 상태 한 줄
    def heartbeat(self, now: datetime, eng: PairEngine, ps: float, ph: float, live: bool,
                  shadows: list[ShadowBook], clock_offset: float | None) -> None:
        if self.next_beat and now < self.next_beat:
            return
        self.next_beat = now.replace(microsecond=0) + timedelta(seconds=self.heartbeat_sec)
        if clock_offset is not None and abs(clock_offset) > self.max_clock and not self.clock_warned:
            self.clock_warned = True
            self.counts["clock_warn"] += 1
            self.j.event("monitor", f"PC−거래소 시각 차이 {clock_offset:+.2f}초 > {self.max_clock:.1f}초 — 봉 마감 판정이 밀릴 수 있음")
        pos = "없음"
        if eng.direction:
            pos = (f"스프레드 {'매수' if eng.direction > 0 else '매도'} 삼성 {eng.open_qty['S']}·하이닉스 {eng.open_qty['H']} "
                   f"{eng.held_bars}봉 · 평가 {eng.mtm(ps, ph):,.0f}원 (손절선 −{eng.stop_won():,.0f})")
        z = "—" if eng.last_z is None else f"{eng.last_z:+.2f}"
        sh = " · ".join(f"{s.name.split('_')[0]} {sum(s.pnl(ps, ph)[:2]):,.0f}" for s in shadows)
        self.j.event("watch", f"보유 {pos} | 하루 {eng.daily_pnl:,.0f}원 · 거래 {eng.daily_trades} · 연속손실 {eng.consec_losses} | "
                              f"z {z} | 실시간 {'OK' if live else '끊김'} | 중지 {eng.halted or eng.monitor_block or '없음'} | 그림자 {sh}")
