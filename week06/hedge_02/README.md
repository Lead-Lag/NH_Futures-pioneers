# 미국달러 선물 볼린저 복귀 전략 + 해외 FX 충격 필터

week02의 평균회귀 전략에 **유로·엔 선물에서 달러가 함께 강해지거나 약해질 때, 그 흐름에 반대되는 국내 달러 선물 진입을 보류하는 조건**을 추가했다. 예를 들어 두 해외 가격이 모두 달러 강세를 나타내면 국내 달러 선물의 매도 진입을 막는다. 이 조건이 손실을 줄이는지 과거 데이터로 비교한 뒤, 같은 설정을 NH 모의계좌에서 실행했다.

**추가한 조건이 수익을 개선한다는 증거는 아직 부족하다.** 모든 백테스트가 손실이었고, 본 실행에 사용한 설정에서 해외 달러 움직임을 이유로 진입을 막은 것은 10월 백테스트 1건·11월 백테스트 0건이었다. 2026-11-27 모의계좌에서는 완료 거래 14건에 수수료 0원 기준 +6,000원을 기록했지만, 그날도 해외 달러 움직임 때문에 막은 진입은 없었다. 따라서 이 하루의 이익을 추가 조건의 효과로 해석할 수 없었다. 설정별 비교는 2절, 본 실행 설정의 선택 이유는 3절, 실제 실행 결과는 4절에 설명한다.

**재검증 변경:** 11.27 금요일, 추가적인 확인을 위해 중간 분봉의 연속 체결 조건을 제거했다. 아래 2~4절은 원래 실행 기록을 보존한 설명이고, 현재 구현은 두 끝점 종가를 사용한다. 수정 전후 비교는 이 문서의 8절에 정리했다.

유로·엔은 관측 대상으로만 사용한다. 실제 주문 대상은 국내 미국달러 선물 1계약이며, 해외 포지션을 함께 보유하는 헤지는 아니다.

## 1. 실험 목적과 기존 전략

볼린저밴드 밖으로 벗어났던 가격이 안으로 복귀하면 평균회귀 진입을 검토한다. 그러나 달러가 여러 통화에 대해 동시에 강해지거나 약해지는 상황에서는 역방향 진입이 불리할 수 있다는 가설을 세웠다. 이를 검토하기 위해 기존 진입·청산 규칙을 유지하고 FX 진입 제한만 추가했다.

| 기존 규칙 | 설정·행동 |
| --- | --- |
| 볼린저밴드 | 종가 20봉, 표준편차 배수 2 |
| 매수 진입 | 직전 종가가 하단 밖, 이번 완료봉 종가가 하단 안으로 복귀 |
| 매도 진입 | 직전 종가가 상단 밖, 이번 완료봉 종가가 상단 안으로 복귀 |
| 최소 밴드폭 | 8틱 |
| 익절 | 완료봉 종가가 중심선에 도달 |
| 손절 | 진입 체결가에서 불리하게 5틱; 본 실행은 현재가로 판단 |
| 보유·재진입 | 최대 30봉, 손절 후 3봉 대기 |
| 거래 제한 | 1계약, 일 최대 진입 20회, 일 손실 제한 30,000원 |
| 운영 시간(KST) | 신규 진입 09:05~15:15, 15:30 종료·잔여 포지션 정리 |

손절 5틱과 일 손실 제한은 보장된 손실 상한이 아니다. 슬리피지·응답 지연으로 실제 손실이 초과할 수 있으며, 일 손실 제한은 신규 진입을 제한하는 규칙이다.

### 1.1 왜 유로·엔 선물을 관측하는가

선정 논리는 **원/달러 자체의 움직임만으로 판단하는 평균회귀 전략에, 서로 다른 두 통화에 대한 달러 움직임을 추가로 확인한다**는 것이다. 다만 아래는 현재 구현을 설명하는 설계 근거다. 다른 통화 후보와 비교해 유로·엔 조합이 최적임을 입증한 선정 실험은 없다.

| 선정 관점 | 이번 설계에서의 이유 | 입증되지 않은 부분 |
| --- | --- | --- |
| 원/달러 외부의 관측치 | 원/달러가 밴드 밖으로 움직였을 때 해외 통화에서도 같은 달러 방향의 움직임이 나타나는지 확인 | 해외 움직임이 원/달러를 선행하거나 이후 수익을 예측한다는 증거는 아님 |
| 한 통화가 아닌 두 통화 | 유로만 또는 엔만 움직인 경우를 공통 달러 충격으로 분류하지 않고, 두 통화가 동시에 조건을 충족할 때만 차단 | 두 통화의 동시 움직임도 달러 요인 때문이라고 인과적으로 확정할 수 없음 |
| 방향을 동일하게 해석 | 코드에서 두 선물을 모두 통화 1단위당 달러 가격으로 정규화하고 `-log(현재/이전)`을 사용하므로 양수를 달러 강세로 통일할 수 있음 | 다른 달러 표시 통화 선물도 가능한 구조이므로 유로·엔만의 독점적 장점은 아님 |
| 같은 수집·실행 환경 | 이번에 NH API로 6EZ26·6JZ26의 과거 분봉을 확보하고 본 실행에서 실시간 수신한 기록이 있어 같은 처리 흐름에 연결 가능 | 수집 가능성이 모든 시간대의 충분한 체결 빈도와 자료 품질을 보장하지 않음 |

예를 들어 국내 달러 선물 가격이 볼린저 상단에서 안으로 복귀하면 기존 전략은 매도를 검토한다. 이때 유로·엔 선물 가격이 모두 하락해 달러 강세 방향의 큰 움직임을 나타내면, 단순한 국내 가격 되돌림으로 보고 매도하는 것을 보류한다. 반대로 두 해외 가격이 함께 상승해 달러 약세를 나타내면 국내 달러 매수를 보류한다. **이것은 예측 모델이나 헤지 비율 추정이 아니라, 기존 진입에 붙인 확인 조건이다.**

두 통화를 모두 요구하는 방식은 한 통화만의 움직임을 제외하는 대신 신호를 줄이고 자료 요건을 강화한다. 실제로 11/27 본 실행의 FX 단계 차단 8건 중 7건은 엔의 최신 유효 변화율이 오래된 경우, 1건은 유로·엔 종료시각 불일치였다. 따라서 “엔은 관측에 적합하다”는 가정도 연속봉 충족률과 함께 다시 평가해야 한다. 유로·엔이 한국 거래시간에 항상 충분히 활발하다는 근거로 선정한 것은 아니다.

### 1.2 선정 근거를 더 검증하려면

현재 자료는 **이 조합을 시험할 이유**를 설명하지만, **다른 조합보다 선택해야 할 이유**까지 증명하지 못한다. 다음 비교가 남아 있다.

1. 같은 기간에 유로 단독·엔 단독·두 통화 동시 조건을 비교하고, 각 구성에 맞는 자료 요건 대조군을 둔다.
2. 국내 신호 시점의 연속봉 충족률·지연·종료시각 일치율을 통화별로 계산한다. 원자료 총 봉 수보다 실제 판단 시점의 가용성이 중요하다.
3. 다른 통화나 달러 지표를 후보로 추가한다면 사전에 후보를 고정하고, 동일한 가용성·비용 조건으로 새 기간에서 평가한다.
4. 단순 가격 상관관계뿐 아니라 차단한 진입의 이후 손익, 놓친 이익, 손실 감소와 최대낙폭을 확인한다. 동시 상관만으로 선행성이나 수익성을 주장하지 않는다.

## 2. 백테스트: 자료와 비교 방법

### 2.1 사용 자료

| 구간 | 국내 선물 | 국내 1분봉 | 유로·엔 선물 입력 봉 수 | 용도 |
| --- | --- | ---: | --- | --- |
| 2026-10-20~10-30, 9거래일 | A756B000 | 3,593 | 유로 57,209 / 엔 58,387 | 기존 구간 재현, 후보 비교 |
| 2026-11-10~11-25, 12거래일 | A756C000 | 4,838 | 유로 75,403 / 엔 74,903 | 기본 후보를 추가 기간에 적용 |

유로는 6EZ26, 엔은 6JZ26이다. 해외 입력에는 통계 보정용 선행 기간도 포함되어 국내 평가 기간보다 길다. 10월 해외 자료는 8/19부터, 11월 해외 자료는 9/9부터 확보했다.

10월 국내 만기 계약의 NH 과거 차트 요청이 빈 결과여서 week02에 보관된 같은 계약의 실제 분봉을 사용했다. 다른 월물 가격으로 대체하지 않았다. 10/29 국내 자료는 13:52까지로 불완전하며 누락 분을 임의로 채우지 않았다.

수집 원자료와 출처 기록은 `generated_reslults/data_20261126_021718_739961_fetch-nh/`(10월), `generated_reslults/data_20261126_072510_885775_fetch-nh/`(11월)에 보관한다.

### 2.2 체결 가정과 대조군

백테스트와 본 실행은 [analyze.py](analyze.py)의 같은 `Engine`과 `FXShockFilter`를 사용한다. 백테스트는 신호 이후 다음 봉 시가에 불리한 슬리피지를 적용하고, 봉 안 가격 경로를 알 수 없을 때 손절을 우선 처리한다. 기본 슬리피지는 **체결당 1틱**, 비용 스트레스는 **체결당 2틱**이다. 수수료는 0원으로, 실측 수수료를 반영한 손익이 아니다. MDD는 1분봉 종가 평가곡선 기준이다.

진입을 막는 이유는 두 가지이며, 이 문서에서는 다음 용어로 구분한다.

- **방향 차단:** 유로·엔이 함께 나타내는 달러 강세·약세와 반대되는 국내 진입을 막는 것. 이 조건을 적용하는 부분을 **방향 필터**라고 부른다.
- **자료 차단:** 판단에 필요한 가격이나 과거 통계가 없거나, 가격이 오래됐거나, 두 통화의 관측 시각이 맞지 않아 진입을 막는 것. 달러 강세·약세를 판단해서 막은 경우와 구분한다.

두 효과를 분리하기 위해 다음 세 가지를 비교했다. `fx_base`는 본 실행에 사용한 기본 설정의 이름이며, 별도 종류의 필터를 뜻하지 않는다.

| 구성 | 해외 자료 부족 시 | 공통 달러 충격에 역행하는 진입 |
| --- | --- | --- |
| `baseline` | 해외 자료 때문에 차단하지 않음 | 차단하지 않음 |
| `baseline_common_coverage` | 차단 | 차단하지 않음 |
| `fx_base` | 차단 | 차단 |

`baseline`과 `fx_base`만 비교하면 자료가 없는 시간에 거래하지 않은 효과까지 방향 필터의 성과로 오인할 수 있다. 따라서 **방향 필터의 추가 효과는 `baseline_common_coverage`와 비교**한다. 3분·10분 후보에도 각각 같은 자료 요건의 대조군을 두었다.

### 2.3 10월 결과: 후보별 비교

표의 `방향`과 `자료`는 각각 위에서 정의한 방향 차단과 자료 차단의 신호 수다. 거래 수 차이와 일대일로 대응하지는 않는다. 진입을 막으면 후속 포지션·쿨다운·손실 제한 경로도 바뀌기 때문이다.

| 설정 | 변경 목적 | 거래 | 손익(원) | MDD(원) | 방향 | 자료 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| baseline | 기존 전략 | 112 | -221,000 | 228,000 | 0 | 0 |
| baseline_common_coverage | 자료 요건만 적용 | 104 | -182,000 | 204,000 | 0 | 27 |
| fx_base | 5분·20일·2σ | 103 | -176,000 | 198,000 | 1 | 27 |
| fx_horizon3 | 관측 길이 3분 | 107 | -203,000 | 213,000 | 0 | 11 |
| fx_horizon10 | 관측 길이 10분 | 94 | -182,000 | 194,000 | 0 | 41 |
| fx_threshold1p5 | 충격 기준 1.5σ | 97 | -183,000 | 205,000 | 2 | 25 |
| fx_threshold2p5 | 충격 기준 2.5σ | 104 | -182,000 | 204,000 | 0 | 27 |
| local_trend | 국내 MA60 기울기 필터, 해외 자료 요건 유지 | 71 | -159,000 | 180,000 | 0 | 21 |
| baseline_coverage_horizon3 | 3분 자료 대조군 | 107 | -203,000 | 213,000 | 0 | 11 |
| baseline_coverage_horizon10 | 10분 자료 대조군 | 94 | -182,000 | 194,000 | 0 | 41 |
| fx_cost2 | 기본 FX 후보, 체결당 2틱 비용 | 66 | -258,000 | 261,000 | 1 | 6 |

`baseline → 자료 대조군`의 개선은 39,000원, `자료 대조군 → fx_base`의 추가 개선은 6,000원이다. 후자의 방향 차단은 10/29 10:15 매수 1건으로, 유로 z=-3.038497·엔 z=-2.102904였다. 이는 과거 데이터 재생 결과이며 당시 실제 거래에서 손실을 회피했다는 뜻은 아니다.

3분·10분 후보는 각 대조군과 거래 수·손익이 같고 방향 차단이 없다. 1.5σ는 기본 후보보다 손실이 컸고, 2.5σ는 방향 차단이 없었다. `local_trend`는 총손실이 가장 작지만 71건으로 거래가 줄었고 거래당 평균 손익은 약 -2,239원으로, `fx_base`의 약 -1,709원보다 나쁘다. 총손실만 보고 우월한 전략으로 선정하지 않았다.

### 2.4 11월 결과: 기본 후보 추가 확인

| 설정 | 거래 | 손익(원) | MDD(원) | 방향 차단 | 자료 차단 |
| --- | ---: | ---: | ---: | ---: | ---: |
| baseline | 134 | -308,000 | 316,000 | 0 | 0 |
| baseline_common_coverage | 122 | -268,000 | 276,000 | 0 | 34 |
| fx_base | 122 | -268,000 | 276,000 | 0 | 34 |

자료 대조군과 FX 후보가 동일한 결과였다. 기존 전략 대비 40,000원 개선을 방향 필터의 효과로 설명할 수 없다. 추가 기간에 후보를 적용했지만, 충분한 독립 검증이나 수익성 입증으로 보지 않는다.

## 3. 본 실행 후보의 선정 근거

**`fx_base`는 수익 최적값이 아니라 사전에 정의한 모의 관찰용 후보를 유지한 것이다.** [당시 결정 기록](generated_reslults/review_20261126_075617_155978_implementation_and_parameter_decision/parameter_decision.yaml)도 `filter_effectiveness_not_established`로 명시한다.

| 항목 | 선택값 | 선정 근거와 한계 |
| --- | --- | --- |
| 기존 진입·청산 | week02 규칙 유지 | 필터 추가 효과를 구분하기 위해 기존 전략을 동시에 재최적화하지 않음 |
| FX 관측 길이 | 5분 | 단기 공통 움직임을 관찰하는 사전 후보. 3·10분 비교로 우월성이 입증되지는 않음 |
| 표준화 기준 | 이전 20개 관측일, 같은 KST 30분 구간 | 당일 정보를 통계 기준에서 제외하고 시간대별 변동성 차이를 고려하려는 설계. 20일·30분 자체의 최적화는 하지 않음 |
| 충격 임계값 | ±2σ | 1.5·2·2.5 비교. 기본 후보의 경제적 차단이 1건뿐이므로 최적 임계값으로 확정하지 않음 |
| 공통 방향 | 유로·엔 모두 같은 달러 방향 | 한 통화만의 움직임과 공통 달러 움직임을 구별하려는 가설. 효과는 미확인 |
| 최소 움직임 | 각 2틱 | 작은 가격 변동을 충격으로 분류하지 않기 위한 관측 조건 |
| 자료 신선도 | 완료봉 종료 후 90초 이내, 두 통화 종료시각 일치 | 오래되거나 비동기적인 관측으로 신규 진입을 판단하지 않기 위한 운영 조건 |
| 수량·비용 | 모의 1계약, 수수료 0원 기록 | 실행 동작을 관찰하는 범위. 실제 수수료·슬리피지의 추가 검증 필요 |

## 4. 본 실행: 실시간 처리와 관찰 결과

### 4.1 실행 흐름

```text
과거 유로·엔 CSV → 이전 관측일의 평균·표준편차 계산
WebSocket 체결 → 완료된 1분봉 생성
  → 국내 볼린저 복귀 신호 및 기존 위험 제한 확인
  → 실시간 유로·엔 5분 변화율을 과거 기준으로 표준화
  → 진입 허용 시 국내 모의계좌 REST 주문
  → O1 체결 통보·REST 대조 → 엔진 포지션과 거래 기록 갱신
```

`--history-dir`는 과거 기준 자료의 위치다. 현재 움직임은 WebSocket으로 수신하며, 필터는 완료된 1분봉 기준으로 판단한다. REST는 초기 분봉 준비·연결 복구·주문·잔고 대조에 사용한다. 실행 시 `--trade`가 없으면 주문을 보내지 않는다.

### 4.2 실행별 결과를 분리해서 보기

| 실행 | 확인된 결과 | 해석 |
| --- | --- | --- |
| [11/26 08:40 실행](generated_reslults/live_20261126_084055_708716_fx_base/run_meta.json) | 완료 거래 0건, `requires_manual_reconciliation`, 최종 계좌 포지션 미확인 | 정상 실행·정산 성공으로 집계하지 않음 |
| [11/27 08:45 본 실행](generated_reslults/live_20261127_084558_564960_fx_base/trades.csv) | 완료 거래 14건, 이익 6건·손실 8건, 합계 +6,000원 | 원시 거래 기록 합산; 수수료 0원 가정 |
| [11/27 15:30 재실행](generated_reslults/live_20261127_153011_611277_fx_base/run_meta.json) | `completed`, 거래 0건, 최종 계좌 포지션 0 | 15:30 종료 조건 이후 실행 |
| [11/27 15:31 재실행](generated_reslults/live_20261127_153124_458467_fx_base/run_meta.json) | `completed`, 거래 0건, 최종 계좌 포지션 0 | 별도 실행이며 아침 실행의 종료 기록을 대체하지 않음 |

11/27 본 실행의 익절은 6건, 손절은 8건이며 승률은 약 42.9%다. [FX 진단 기록](generated_reslults/live_20261127_084558_564960_fx_base/fx_diagnostics.csv)은 25건 중 `NO_COMMON_SHOCK` 15건·`UNAVAILABLE` 10건이었다. 기존 진입 조건을 통과한 후 FX 단계에서 실제 차단된 것은 8건이며 모두 자료/피드 가용성에 따른 차단이다. **공통 달러 강세·약세 판정에 따른 차단은 없었다.**

본 실행에서 WebSocket 시세·주문·체결 기록은 확인되지만, 이것만으로 모든 복구·예외 경로가 검증된 것은 아니다. 11/27 주 실행은 마지막 기록이 15:29:49에 끊기고 `run_meta.json`이 `running`으로 남아 종료 보고서가 없다. 최초 중단 원인은 기록만으로 확정할 수 없다. 이후 재실행에서 잔고 0이 확인됐다는 사실과 주 실행의 정상 종료 여부를 구분한다.

백테스트의 평가 날짜는 10월·11/10~25이고 본 실행은 11/27이므로, 손익 차이를 곧바로 체결 품질 차이로 해석하지 않는다. 같은 날짜·같은 입력 자료의 재생 비교가 추가로 필요하다.

## 5. 추가한 필터의 소스 코드

아래는 현재 소스의 핵심 부분을 발췌한 것이다. 생략된 주변 로직을 포함한 원문은 [analyze.py](analyze.py)와 [run_live.py](run_live.py)에 있다.

### 5.1 기존 엔진에 진입 필터 연결

`Engine.on_bar_close()`에서 기존 제한을 먼저 계산하고, FX 필터 결과를 신규 진입 허용 여부에 반영한다. 차단된 신호는 이후에 지연 주문하지 않는다. 기존 포지션의 익절·손절 규칙은 유지한다.

```python
        blocked = self._entry_block(end, width_ticks, side, reward_ticks, slope)
        eligible_before_fx = not blocked
        fx_details: dict[str, Any] = {}
        if self.entry_filter is not None:
            allowed, fx_reason, fx_details = self.entry_filter(end, side)
            fx_details = dict(fx_details)
            fx_details.update({"fx_allowed": int(allowed), "fx_reason": fx_reason,
                               "eligible_before_fx": int(eligible_before_fx),
                               "filtered_by_fx": int(eligible_before_fx and not allowed)})
            if eligible_before_fx and not allowed:
                blocked = fx_reason
            if hasattr(self.j, "row"):
                self.j.row("fx_diagnostics", ts=end.isoformat(), side=side, **fx_details)
```

### 5.2 현재 움직임과 과거 기준

`FXShockFilter._feature()`는 현재 완료봉과 정확히 5분 전 완료봉의 두 종가로 변화율을 계산한다. 재검증 수정에서 **중간 4개 분봉의 존재·거래량 조건을 제거**했다. 양 끝 봉의 가격·거래량·월물 일치와 수신 가능 시각은 계속 확인하며, 없는 가격을 보간하거나 이전 값으로 채우지 않는다. 위 2~4절의 기존 결과는 수정 전 연속 6봉 조건으로 실행한 기록이다. 유로·엔 가격이 통화 1단위당 달러이므로 부호를 반전하여 양수가 달러 강세를 나타내게 한다.

```python
        for offset in (0, self.horizon):
            row = bars.get(start - timedelta(minutes=offset))
            if row is None or row["close"] <= 0:
                return None
```

```python
        return {
            "change": -math.log(current["close"] / segment[-1]["close"]),
            "move": abs(current["close"] - segment[-1]["close"]),
            "end": start + timedelta(minutes=1),
            "available_at": max(availability) if availability else None,
            "symbol": current.get("symbol", ""),
        }
```

`_calibration()`은 오늘을 제외하고 같은 시간대의 과거 관측치만 고른다. 아래는 선택 조건 부분이다.

```python
        for end in self._ends[name]:
            if end.date() >= current.date():
                break
            if (end.hour * 60 + end.minute) // self.bucket != bucket_id:
                continue
            f = features[end]
            # A historical observation with known late availability is not made
            # retroactively available to an earlier decision.
            if f.get("available_at") and f["available_at"] > current:
                continue
            candidates.setdefault(end.date(), []).append(f["change"])
        days = sorted(candidates)[-self.lookback:]
        values = [value for day in days for value in candidates[day]]
```

최소 20개 관측일·100개 표본과 표준편차 하한을 확인한 뒤 평균·표본 표준편차를 계산한다. `evaluate()`에서 현재 변화율을 다음과 같이 표준화한다.

```python
            mu, sigma, n_days, n_samples = calibration
            z = (feature["change"] - mu) / sigma
```

### 5.3 자료 차단과 방향 차단의 구현

자료 누락·오래된 봉·서로 다른 종료시각·통계 표본 부족은 `FX_DATA_UNAVAILABLE`로 기록한다. 이를 ‘충격 없음’으로 대체하지 않는다.

```python
        if missing:
            reason = "FX_DATA_UNAVAILABLE: " + "; ".join(missing)
            if self.enabled or self.require_coverage:
                return False, reason, details
            details["fx_state"] = "DISABLED_DATA_UNAVAILABLE"
            return True, "", details
```

유로·엔 모두 최소 움직임과 같은 달러 방향의 z점수 조건을 만족해야 공통 달러 강세·약세로 분류하고, 이에 역행하는 진입에 방향 차단을 적용한다.

```python
        strong = weak = True
        for name, (feature, z) in observed.items():
            tick = float(self.config.get(f"{name}_tick_size", 0))
            enough_move = feature["move"] >= tick * self.min_move_ticks
            strong = strong and enough_move and feature["change"] > 0 and z >= self.threshold
            weak = weak and enough_move and feature["change"] < 0 and z <= -self.threshold
        state = "USD_STRONG" if strong else "USD_WEAK" if weak else "NO_COMMON_SHOCK"
        details["fx_state"] = state
        if self.enabled and state == "USD_STRONG" and side == SELL:
            return False, "FX_USD_STRONG: overseas common move blocks USD/KRW short", details
        if self.enabled and state == "USD_WEAK" and side == BUY:
            return False, "FX_USD_WEAK: overseas common move blocks USD/KRW long", details
        return True, "", details
```

### 5.4 실시간 피드 상태 확인

`run_live.py`는 피드 준비·복구 상태를 확인한 뒤 실제 판단 시각으로 필터를 평가한다. 완료된 해외 봉은 실행 루프에서 `self.gate.add_bar(name, bar)`로 전달한다.

```python
    def entry_filter(self, signal_end, side):
        if self.stop_entries or self.repair_needed or not self.feeds or not all(feed.healthy for feed in self.feeds):
            return False, "LIVE_FEED_NOT_READY", {"fx_valid": 0, "fx_state": "UNAVAILABLE"}
        # The exchange bar end and actual decision/receipt time are distinct.
        allowed, reason, details = self.gate.evaluate(datetime.now(KST), side)
        details["signal_bar_end"] = signal_end.isoformat()
        return allowed, reason, details
```

## 6. 파일 구성과 결과 읽는 법

| 파일 | 역할 |
| --- | --- |
| `strategy_config.yaml` / `config.py` | 공통 `base`에 `variants`의 변경값을 병합하여 실행 설정 결정 |
| `analyze.py` | 전략 엔진·위험 제한·FX 필터; 백테스트와 본 실행에서 공유 |
| `backtest.py` | 과거 봉 재생 및 모의 체결 |
| `revalidate_endpoints.py` | 같은 환경·원자료·저장 설정에서 중간봉 제한 전후를 쌍으로 재생하고 비교 기록 생성 |
| `run_live.py` | 실시간 봉 생성 연결·주문 실행·계좌 대조·종료 처리 |
| `websocket.py` / `nh_client.py` | 실시간 시세·체결 통보 / REST 인증·조회·주문 |
| `data.py` | 분봉 수집·정규화·출처 기록 |
| `runtime.py` | 실행 폴더·설정·소스 스냅샷·로그 보관; 현재 콘솔에는 상시 로그를 출력하지 않음 |
| `summarize.py` / `visualization.py` | 보고서·차트 작성 |
| `validate.py` / `tests/` | 가짜 응답 기반 동작 검증 |

제출 문서는 이 README 하나로 구성한다. 원시 결과는 `metrics.json → trades.csv → signals.csv / fx_diagnostics.csv → orders.csv / fills.csv → events.jsonl` 순서로 확인한다. 종료 보고서가 없는 실행은 원시 CSV와 종료 기록 누락을 함께 본다.

현재 보관 폴더 이름은 **`generated_reslults/`**다(실제 디렉터리 철자 그대로). 저장된 명령·메타데이터의 과거 `results/` 경로는 실행 당시 기록이므로 수정하지 않았다. 현재 YAML 기본 출력은 여전히 `results/`다. 아래 재현 명령은 이동한 자료 경로와 `--out`을 명시한다.

`report_refresh.json`의 `calculation_replayed: false`는 보고서·차트만 갱신했다는 뜻이다. 새 백테스트 실행으로 세지 않는다. `code_snapshot`은 당시 미커밋 코드까지 보관하며 현재 소스와 다를 수 있다.

## 7. 재현 명령과 검증 범위

`hedge_02`에서 실행한다. 다음은 저장 자료로 백테스트를 다시 돌리는 명령이며 새 실행 폴더가 생성된다.

```powershell
python .\backtest.py --config .\strategy_config.yaml --all-variants --data-dir .\generated_reslults\data_20261126_021718_739961_fetch-nh --start 2026-10-20 --end 2026-10-30 --out .\generated_reslults

python .\backtest.py --config .\strategy_config.yaml --variants baseline baseline_common_coverage fx_base --data-dir .\generated_reslults\data_20261126_072510_885775_fetch-nh --start 2026-11-10 --end 2026-11-25 --out .\generated_reslults
```

2026-11-27 본 실행에 사용한 자료를 현재 위치에서 참조하는 예시는 다음과 같다. 이후 날짜에는 월물과 과거 자료를 갱신해야 한다. FX 필터 활성화 상태의 주문 실행은 이전 자료가 최소 20일분이고 마지막 관측일이 4일 이내인지 검사한다.

```powershell
# 주문 없는 3분 관찰
python .\run_live.py --config .\strategy_config.yaml --variant fx_base --history-dir .\generated_reslults\data_20261126_072510_885775_fetch-nh --run-minutes 3 --out .\generated_reslults

# 국내 모의계좌 주문 활성화
python .\run_live.py --config .\strategy_config.yaml --variant fx_base --history-dir .\generated_reslults\data_20261126_072510_885775_fetch-nh --trade --out .\generated_reslults
```

인증정보는 `NH_APP_KEY` / `NH_APP_SECRET`, 해외 관측용은 `NH_OVS_APP_KEY` / `NH_OVS_APP_SECRET` 환경변수를 사용한다. 같은 종목의 다른 주문과 섞이지 않도록 시작 시 포지션·미체결이 없는지 검사한다. 15:30 이후에는 실행 루프가 종료 조건에 걸린다.

[2026-11-26 마지막 가짜 응답 검증](generated_reslults/validation_20261126_075023_797953_approved_fake_only/validation.txt)은 **64개 통과**다. 불명 주문·취소 직전 체결·중복 통보·재접속·미래 정보 사용 방지·엔 가격 배율·손절 큐 등을 검증했지만, 이 테스트에서 실제 계좌 주문이나 실제 WebSocket 연결을 검증한 것은 아니다. 11/27 본 실행 기록은 그 이후의 별도 운영 관찰이다. README 작성 과정에서 테스트나 매매를 새로 실행하지 않았다.

다음 분석은 ① 11/27 중단과 최종 계좌 대조 기록 확인, ② 같은 날 데이터의 백테스트와 체결 대조, ③ 자료 부족 차단과 경제적 방향 차단 분리, ④ 실제 수수료·슬리피지 반영, ⑤ 후보를 고정한 새 기간 검증 순서로 진행한다.

## 8. 중간 분봉 연속 조건 제거 후 재검증

사용자 요청에 따라 중간 분의 봉 존재·거래량 양수 조건을 제거했다. 현재와 정확히 관측 길이 전의 두 완료봉으로 변화율을 계산하며, **두 끝점 가격이 없는 경우까지 임의로 채운 것은 아니다.** 끝점의 거래량·월물, 90초 신선도, 유로·엔 종료시각 일치, 과거 보정 표본 조건은 유지했다. 같은 변경을 실시간 엔진과 백테스트의 과거 통계 계산에 공통 적용했다.

기존 14개 실험 각각에 대해 같은 Python·입력 파일·설정·체결 모형으로 수정 전과 수정 후를 재생했다. 수정 전에는 보관된 `_feature` 메서드만 연결하고, 나머지 엔진은 동일하게 사용했다. **14쌍, 총 28회**의 비교이며 모든 원자료 SHA-256이 과거 실행 기록과 일치하는지 확인했다. 실제 계좌 주문과 본 실행 재시작은 하지 않았다.

- [68개 오프라인 테스트 통과 기록](generated_reslults/endpoint_revalidation/validation_20261127_171004_764060_approved_fake_only/validation.txt)
- [검증 환경 패키지 버전](generated_reslults/endpoint_revalidation/environment.txt)

### 기본 후보 `fx_base`의 수정 전후

| 평가 기간 | 거래 수 | 자료 차단 | 방향 차단 | 손익(수수료 0원) |
| --- | --- | --- | --- | --- |
| 10/20~30 | 103 → 105 | 27 → 6 | 1 → 1 | -174,000 → -212,000원 |
| 11/10~25 | 122 → 136 | 34 → 16 | 0 → 0 | -268,000 → -299,000원 |

**불필요하게 강했던 자료 조건을 완화하는 목적은 달성했지만, 이 표본에서 기본 후보의 손익은 개선되지 않았다.** 10월 손실은 38,000원, 11월 손실은 31,000원 늘었다. 10월 수정 후 자료 대조군은 -219,000원, 방향 필터까지 적용하면 -212,000원으로 추가 차이는 7,000원이다. 11월에는 두 구성이 모두 -299,000원으로 여전히 방향 필터의 추가 효과가 없었다.

과거 통계 표본도 함께 늘어나므로 이번 변경은 기존 신호에 대해 자료 차단만 해제하는 작업과 다르다. z점수, 진입 시점, 일 손실 제한과 후속 거래 경로가 달라질 수 있다. 2틱 비용 후보처럼 거래 수가 줄어드는 경우도 있으므로 ‘제한 완화 = 거래 수 단조 증가’로 해석하지 않는다.

### 14개 설정 전체 수정 전후 비교

아래 수치는 같은 환경에서 재계산한 값이다. 손익 단위는 원이며, 자료·방향 차단은 신호 건수다.

| 기간 | 설정 | 거래 전→후 | 손익 전→후 | 자료 차단 전→후 | 방향 차단 전→후 |
| --- | --- | ---: | ---: | ---: | ---: |
| 10/20~30 | `baseline` | 113→113 | -225,000→-225,000 | 0→0 | 0→0 |
| 10/20~30 | `baseline_common_coverage` | 104→105 | -180,000→-219,000 | 27→6 | 0→0 |
| 10/20~30 | `fx_base` | 103→105 | -174,000→-212,000 | 27→6 | 1→1 |
| 10/20~30 | `fx_horizon3` | 108→110 | -199,000→-208,000 | 14→8 | 0→0 |
| 10/20~30 | `fx_horizon10` | 94→108 | -180,000→-214,000 | 41→9 | 0→0 |
| 10/20~30 | `fx_threshold1p5` | 97→99 | -181,000→-219,000 | 25→4 | 2→2 |
| 10/20~30 | `fx_threshold2p5` | 104→105 | -180,000→-219,000 | 27→6 | 0→0 |
| 10/20~30 | `local_trend` | 71→74 | -159,000→-203,000 | 21→7 | 0→0 |
| 10/20~30 | `baseline_coverage_horizon3` | 108→110 | -199,000→-208,000 | 14→8 | 0→0 |
| 10/20~30 | `baseline_coverage_horizon10` | 94→108 | -180,000→-214,000 | 41→9 | 0→0 |
| 10/20~30 | `fx_cost2` | 66→61 | -256,000→-251,000 | 6→2 | 1→1 |
| 11/10~25 | `baseline` | 134→134 | -308,000→-308,000 | 0→0 | 0→0 |
| 11/10~25 | `baseline_common_coverage` | 122→136 | -268,000→-299,000 | 34→16 | 0→0 |
| 11/10~25 | `fx_base` | 122→136 | -268,000→-299,000 | 34→16 | 0→0 |

### 과거 보관 숫자와의 차이

원래 10월 `fx_base` 보고서는 -176,000원이지만 동일 환경에서 다시 계산한 수정 전은 -174,000원이다. 따라서 위 비교는 과거 표의 숫자를 그대로 빼지 않고 **같은 환경의 before/after**를 사용했다. 필터가 꺼진 `baseline`은 동일 환경 전후 모두 113건·-225,000원으로 같았다.

별도 수치 민감도 확인에서 엔진의 `sum`을 순차 덧셈으로 바꾸자 10월 `baseline`이 과거 보관값인 112건·-221,000원으로 재현됐다. 이는 경계값 비교가 부동소수점 합산 방식에 민감하다는 증거다. 과거 실행 환경 전체를 복원한 것은 아니므로 모든 차이의 원인을 확정하지 않으며, 이번 수정에 수치 계산 방식 변경은 포함하지 않았다. [수치 민감도 확인 기록](generated_reslults/endpoint_revalidation/numeric_probe.json)

재현은 다음 명령으로 가능하다. 이 스크립트는 과거 설정과 원자료를 읽고 새 폴더에 비교 결과를 만든다.

```powershell
.\.venv-revalidation\Scripts\python.exe .\revalidate_endpoints.py
```

비교의 기준 결과는 `paired_…` 폴더다. 별도 일반 백테스트의 `october/`, `november/` 결과도 저장했지만, 전체 14쌍 비교가 끝난 뒤 중복되는 10월 차트 생성 작업은 중단했다. 해당 미완성 보고서는 `REPORT_INCOMPLETE.txt`로 표시했으며, 전체 검증 결과 누락을 뜻하지 않는다. 기존 2026-11-26 백테스트와 11/27 본 실행 기록은 덮어쓰지 않았다.
