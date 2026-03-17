"""자체 반성 엔진 — 매일 장 마감 후 성과를 분석하고 파라미터를 자동 조정한다.

Claude Haiku API를 활용한 지능형 반성:
1. 최근 매매 성과 메트릭을 분석한다.
2. Claude Haiku에게 메트릭, 현재 설정, 최근 반성 이력을 보내 조정 방향을 받는다.
3. Claude가 반환한 parameter_changes를 검증 후 적용한다.
4. API 호출 실패 시 규칙 기반 조정으로 폴백한다.

규칙 기반 폴백:
1. 승률 < 30% → 스코어 기준 강화 (SCORE_THRESHOLD 상향)
2. 평균손실 >> 평균이익 → 트레일링 스탑 강화
3. 현금 비중 > 70% → 최대 보유 수 확대 또는 기준 완화
4. Profit Factor < 1 → 감성 가중치 축소, 신뢰도 가중치 확대
5. 연속 손실일 ≥ 3 → 최대 보유 수 축소
6. 승률 > 60% + PF > 1.5 → 보유 수 확대
7. 보유 기간 짧음(평균 < 2일) → 트레일링 스탑 완화
"""

import json
import logging
from datetime import datetime, timedelta

import anthropic

from config import CLAUDE_API_KEY
from db import get_connection

logger = logging.getLogger(__name__)

CLAUDE_MODEL = "claude-haiku-4-5-20251001"

# stock-crawler 튜닝 가능 파라미터 (이름: (min, max))
TUNABLE_PARAMS = {
    "TRAILING_STOP_PCT":      (-7.0, -1.5),     # 트레일링 스탑 % (음수)
    "STOP_LOSS_PCT":          (-10.0, -3.0),     # 절대 손절 % (음수)
    "SCORE_THRESHOLD":        (0.4, 0.8),        # 매수 스코어 기준
    "WEIGHT_MENTION_FREQ":    (0.05, 0.5),       # 언급빈도 가중치
    "WEIGHT_POSITIVE_RATIO":  (0.1, 0.6),        # 긍정비율 가중치
    "WEIGHT_CONFIDENCE":      (0.05, 0.4),       # 신뢰도 가중치
    "SCORE_LOOKBACK_DAYS":    (1, 7),            # 스코어 참조 기간 (일)
    "MAX_HOLDINGS":           (3, 7),            # 최대 보유 종목 수
    "MAX_DAILY_BUYS":         (1, 4),            # 일일 매수 한도
    "MIN_CASH_RATIO":         (0.15, 0.40),      # 최소 현금 비율
}


def run_reflection() -> dict | None:
    """일일 반성 사이클.

    1차: Claude Haiku API로 지능형 반성 시도
    2차: API 실패 시 규칙 기반 폴백

    Returns:
        {"metrics": {...}, "changes": {...}, "reasoning": str} or None
    """
    today = datetime.now().strftime("%Y-%m-%d")
    logger.info("=== 반성의 시간 시작 (%s) ===", today)

    # 초기 안전장치: 누적 매도 10건 미만이면 무조건 스킵
    conn = get_connection()
    try:
        total_sells = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE trade_type='SELL'"
        ).fetchone()[0]
    finally:
        conn.close()
    if total_sells < 10:
        logger.info("반성 스킵: 누적 매도 %d건 < 10건 (데이터 부족)", total_sells)
        return None

    # 조정 빈도 제한: 청산 20건+ 또는 7일+ 일 때만 실행
    recent = _get_reflections(limit=1)
    if recent:
        last_date = recent[0].get("date", "")
        try:
            conn = get_connection()
            sells_since = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE trade_type='SELL' AND traded_at > ?",
                (last_date,)
            ).fetchone()[0]
            conn.close()
            days_since = (datetime.now() - datetime.strptime(last_date, "%Y-%m-%d")).days
            if sells_since < 20 and days_since < 7:
                logger.info("반성 스킵: 최근 반성 이후 청산 %d건 (<20), %d일 (<7일)",
                            sells_since, days_since)
                return None
        except Exception:
            logger.debug("반성 게이트 체크 실패, 계속 진행")

    # 1. 성과 분석
    metrics = _analyze_performance()

    if metrics["total_sells"] == 0 and metrics["total_buys"] == 0:
        logger.info("최근 거래 없음, 반성 스킵")
        _save_reflection(
            date=today, metrics=metrics,
            parameter_changes="", reasoning="거래 없음",
        )
        return None

    logger.info(
        "성과 분석: 매도 %d건 | 승률 %.1f%% | 평균이익 %+.0f원 | 평균손실 %+.0f원 | PF %.2f | 순손익 %+d원 | 총수익률 %+.2f%%",
        metrics["total_sells"], metrics["win_rate"],
        metrics["avg_profit"], metrics["avg_loss"],
        metrics["profit_factor"], metrics["total_pnl"],
        metrics["total_return"],
    )

    # 2. 연속 손실일 체크
    consecutive_loss_days = _check_consecutive_losses()

    # 3. Claude API로 지능형 반성 시도, 실패 시 규칙 기반 폴백
    changes = None
    reasoning = ""
    code_suggestions = ""
    source = ""

    try:
        claude_result = _call_claude_for_reflection(metrics, consecutive_loss_days)
        if claude_result is not None:
            severity = claude_result.get("severity", "none")
            reasoning = claude_result.get("reasoning", "")
            code_suggestions = claude_result.get("code_suggestions", "")
            source = "claude"

            if severity == "none":
                logger.info("[Claude] 조정 불필요 (severity=none): %s", reasoning)
                changes = {}
            else:
                changes = claude_result.get("parameter_changes", {})
                logger.info(
                    "[Claude] severity=%s, 파라미터 %d개 조정 제안: %s",
                    severity, len(changes), reasoning,
                )
            if code_suggestions:
                logger.info("[Claude] 코드 개선 추천: %s", code_suggestions)
        else:
            raise ValueError("Claude 반환값 None")
    except Exception as e:
        logger.warning("[Claude] API 실패, 규칙 기반 폴백 사용: %s", e)
        changes, reasoning = _decide_adjustments(metrics, consecutive_loss_days)
        source = "rules"

    # 4. 파라미터 적용
    if changes:
        for param, new_val in changes.items():
            _save_dynamic_config(param, new_val, source=f"reflection_{source}")
            logger.info("파라미터 조정 [%s]: %s → %s", source, param, new_val)
        logger.info("총 %d개 파라미터 조정 완료 (source=%s)", len(changes), source)
    else:
        logger.info("파라미터 조정 없음 (source=%s)", source)

    # 5. 반성 로그 저장
    reasoning_with_source = f"[{source}] {reasoning}"
    _save_reflection(
        date=today, metrics=metrics,
        parameter_changes=json.dumps(changes, ensure_ascii=False) if changes else "",
        reasoning=reasoning_with_source,
    )

    # 6. 텔레그램 알림
    _notify_reflection(metrics, changes, reasoning_with_source, code_suggestions)

    logger.info("=== 반성 완료 ===\n%s", reasoning_with_source)

    return {
        "metrics": metrics,
        "changes": changes,
        "reasoning": reasoning_with_source,
        "code_suggestions": code_suggestions,
    }


def _call_claude_for_reflection(metrics: dict, consecutive_loss_days: int) -> dict | None:
    """Claude Haiku API를 호출하여 지능형 파라미터 조정을 받는다."""
    if not CLAUDE_API_KEY:
        logger.warning("CLAUDE_API_KEY 미설정, Claude 반성 스킵")
        return None

    # 현재 파라미터 값
    current_params = _get_all_dynamic_config()

    # 최근 5일 반성 이력
    recent_reflections = _get_reflections(limit=5)
    reflection_history = []
    for r in recent_reflections:
        reflection_history.append({
            "date": r.get("date", ""),
            "total_sells": r.get("total_sells", 0),
            "win_rate": r.get("win_rate", 0),
            "profit_factor": r.get("profit_factor", 0),
            "total_pnl": r.get("total_pnl", 0),
            "parameter_changes": r.get("parameter_changes", ""),
            "reasoning": r.get("reasoning", ""),
        })

    # 튜닝 가능 파라미터 범위
    param_bounds = {
        name: {"min": bounds[0], "max": bounds[1]}
        for name, bounds in TUNABLE_PARAMS.items()
    }

    prompt = f"""당신은 한국 주식 뉴스 기반 자동투자 시스템의 자체 반성 엔진입니다.
시스템의 매매 성과를 분석하고, 파라미터 조정을 제안해주세요.

## 시스템 구조
- 뉴스/공시/텔레그램 채널 수집 → Claude 감성분석 → 종목 스코어링 → 매수/매도
- 스코어 = 언급빈도 * W1 + 긍정비율 * W2 + 신뢰도 * W3
- 매도: 트레일링 스탑(고점 대비) + 절대 손절(매입가 대비)
- 리밸런싱: 평일 16:30 장 마감 후

## 최근 매매 성과
- 총 매수: {metrics['total_buys']}건, 총 매도: {metrics['total_sells']}건
- 승률: {metrics['win_rate']:.1f}% ({metrics['win_count']}승 {metrics['loss_count']}패)
- 평균 이익: {metrics['avg_profit']:+,.0f}원 / 평균 손실: {metrics['avg_loss']:+,.0f}원
- Profit Factor: {metrics['profit_factor']:.2f}
- 순 실현 손익: {metrics['total_pnl']:+,}원
- 총 수익률: {metrics['total_return']:+.2f}%
- 현재 보유: {metrics['current_holdings']}종목, 현금 비중: {metrics['cash_ratio']:.1f}%
- 평균 보유 기간: {metrics['avg_holding_days']:.1f}일
- 연속 손실일: {consecutive_loss_days}일

## 현재 파라미터 값
{json.dumps(current_params, indent=2, ensure_ascii=False)}

## 조정 가능한 파라미터와 범위
{json.dumps(param_bounds, indent=2, ensure_ascii=False)}

## 최근 5일 반성 이력
{json.dumps(reflection_history, indent=2, ensure_ascii=False)}

## 지시사항
1. 성과가 양호하면 (승률 > 50%, PF > 1.2) severity를 "none"으로 반환하세요.
2. 변경이 필요하면 severity를 "minor" (1-2개) 또는 "major" (3개+)로 설정하세요.
3. 보수적으로, 작은 점진적 변경만 제안하세요. 급격한 변경은 금지입니다.
4. 모든 값은 반드시 해당 파라미터의 min/max 범위 내여야 합니다.
5. 가중치(W1+W2+W3)의 합이 1.0이 되도록 유지하세요.
6. 최근 반성 이력에서 이미 같은 방향으로 조정한 파라미터는 추가 조정을 자제하세요.
7. reasoning과 code_suggestions는 반드시 한국어로 작성하세요.
8. code_suggestions: 파라미터로 해결할 수 없는 구조적 개선점이 있다면 간결하게 추천하세요.

반드시 아래 JSON 형식만 반환하세요 (다른 텍스트 없이):
{{
  "parameter_changes": {{"파라미터명": 새값, ...}},
  "reasoning": "파라미터 조정 사유 한국어 설명",
  "severity": "none" | "minor" | "major",
  "code_suggestions": "코드 수준 개선 추천 (없으면 빈 문자열)"
}}"""

    logger.info("[Claude] 반성 API 호출 시작 (model=%s)", CLAUDE_MODEL)

    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    raw_text = response.content[0].text.strip()
    logger.info("[Claude] 응답 수신 (길이=%d, tokens: in=%d out=%d)",
                len(raw_text), response.usage.input_tokens, response.usage.output_tokens)

    # JSON 블록 추출
    if raw_text.startswith("```"):
        lines = raw_text.split("\n")
        json_lines = []
        inside = False
        for line in lines:
            if line.strip().startswith("```") and not inside:
                inside = True
                continue
            elif line.strip().startswith("```") and inside:
                break
            elif inside:
                json_lines.append(line)
        raw_text = "\n".join(json_lines)

    result = json.loads(raw_text)

    # 검증: severity
    severity = result.get("severity", "none")
    if severity not in ("none", "minor", "major"):
        logger.warning("[Claude] 잘못된 severity=%s, none으로 처리", severity)
        result["severity"] = "none"
        result["parameter_changes"] = {}

    # 검증: parameter_changes 범위 체크
    validated_changes = {}
    for param, value in result.get("parameter_changes", {}).items():
        if param not in TUNABLE_PARAMS:
            logger.warning("[Claude] 알 수 없는 파라미터 무시: %s", param)
            continue
        lo, hi = TUNABLE_PARAMS[param]
        clamped = max(lo, min(hi, float(value)))
        if clamped != float(value):
            logger.warning(
                "[Claude] %s 값 범위 초과 보정: %.2f → %.2f (범위: %.2f~%.2f)",
                param, float(value), clamped, lo, hi,
            )
        validated_changes[param] = round(clamped, 3)

    # 가중치 합 = 1.0 검증
    w_keys = ["WEIGHT_MENTION_FREQ", "WEIGHT_POSITIVE_RATIO", "WEIGHT_CONFIDENCE"]
    if any(k in validated_changes for k in w_keys):
        # 현재 값에 변경사항 적용
        w_vals = {}
        for k in w_keys:
            if k in validated_changes:
                w_vals[k] = validated_changes[k]
            elif k in current_params:
                w_vals[k] = current_params[k]
            else:
                w_vals[k] = TUNABLE_PARAMS[k][0]  # fallback
        total = sum(w_vals.values())
        if abs(total - 1.0) > 0.01:
            # 정규화
            for k in w_keys:
                if k in validated_changes:
                    validated_changes[k] = round(w_vals[k] / total, 3)
            logger.info("[Claude] 가중치 합 %.3f → 1.0 정규화 적용", total)

    result["parameter_changes"] = validated_changes

    logger.info(
        "[Claude] 반성 결과: severity=%s, 변경=%d개, 사유=%s",
        result.get("severity"), len(validated_changes), result.get("reasoning", ""),
    )
    return result


def _analyze_performance() -> dict:
    """전체 매매 성과 메트릭 산출."""
    from config import INITIAL_CAPITAL

    conn = get_connection()
    try:
        conn.row_factory = __import__('sqlite3').Row

        # 전체 매매 기록
        buys = conn.execute(
            "SELECT * FROM trades WHERE trade_type='BUY' ORDER BY traded_at"
        ).fetchall()
        sells = conn.execute(
            "SELECT * FROM trades WHERE trade_type='SELL' ORDER BY traded_at"
        ).fetchall()

        wins = [dict(s) for s in sells if (s['pnl'] or 0) > 0]
        losses = [dict(s) for s in sells if (s['pnl'] or 0) <= 0]

        total_sells = len(sells)
        win_count = len(wins)
        loss_count = len(losses)
        win_rate = win_count / total_sells * 100 if total_sells > 0 else 0

        total_profit = sum(s['pnl'] for s in wins) if wins else 0
        total_loss_amt = abs(sum(s['pnl'] for s in losses)) if losses else 0
        avg_profit = total_profit / win_count if win_count > 0 else 0
        avg_loss = -(total_loss_amt / loss_count) if loss_count > 0 else 0
        profit_factor = total_profit / total_loss_amt if total_loss_amt > 0 else (
            float('inf') if total_profit > 0 else 0
        )
        total_pnl = sum((s['pnl'] or 0) for s in sells)

        # 현재 포트폴리오
        holdings = conn.execute("SELECT * FROM holdings").fetchall()
        current_holdings = len(holdings)

        # 현금
        buy_total = conn.execute(
            "SELECT COALESCE(SUM(total_amount), 0) FROM trades WHERE trade_type='BUY'"
        ).fetchone()[0]
        sell_total = conn.execute(
            "SELECT COALESCE(SUM(total_amount), 0) FROM trades WHERE trade_type='SELL'"
        ).fetchone()[0]
        cash = INITIAL_CAPITAL - buy_total + sell_total
        holdings_value = sum(
            (h['current_price'] or h['avg_price']) * h['quantity'] for h in holdings
        )
        total_value = cash + holdings_value
        cash_ratio = cash / total_value * 100 if total_value > 0 else 100
        total_return = (total_value - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100

        # 평균 보유 기간
        avg_holding_days = 0
        if sells:
            holding_days = []
            for s in sells:
                # 매도 종목의 매수일 찾기
                buy_match = conn.execute(
                    "SELECT traded_at FROM trades WHERE stock_code=? AND trade_type='BUY' ORDER BY traded_at DESC LIMIT 1",
                    (s['stock_code'],)
                ).fetchone()
                if buy_match:
                    try:
                        buy_dt = datetime.strptime(buy_match[0][:10], "%Y-%m-%d")
                        sell_dt = datetime.strptime(s['traded_at'][:10], "%Y-%m-%d")
                        holding_days.append((sell_dt - buy_dt).days)
                    except Exception:
                        pass
            avg_holding_days = sum(holding_days) / len(holding_days) if holding_days else 0

        # 최근 일별 성과
        recent_perf = conn.execute(
            "SELECT date, total_return FROM daily_performance ORDER BY date DESC LIMIT 7"
        ).fetchall()

        return {
            "total_buys": len(buys),
            "total_sells": total_sells,
            "win_count": win_count,
            "loss_count": loss_count,
            "win_rate": round(win_rate, 1),
            "avg_profit": round(avg_profit),
            "avg_loss": round(avg_loss),
            "profit_factor": round(profit_factor, 2),
            "total_pnl": round(total_pnl),
            "total_return": round(total_return, 2),
            "current_holdings": current_holdings,
            "cash_ratio": round(cash_ratio, 1),
            "avg_holding_days": round(avg_holding_days, 1),
            "recent_daily_returns": [
                {"date": r[0], "return": r[1]} for r in recent_perf
            ],
        }
    finally:
        conn.close()


def _check_consecutive_losses() -> int:
    """최근 연속 손실일 수."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT daily_return FROM daily_performance ORDER BY date DESC LIMIT 10"
        ).fetchall()
        consecutive = 0
        for r in rows:
            if (r[0] or 0) < 0:
                consecutive += 1
            else:
                break
        return consecutive
    finally:
        conn.close()


def _decide_adjustments(metrics: dict, consecutive_loss_days: int) -> tuple[dict, str]:
    """성과 기반 파라미터 조정 (규칙 기반 폴백)."""
    changes: dict[str, float] = {}
    reasons: list[str] = []
    current = _get_all_dynamic_config()

    def cur(name: str):
        if name in current:
            return current[name]
        from config import (
            TRAILING_STOP_PCT, STOP_LOSS_PCT, SCORE_THRESHOLD,
            WEIGHT_MENTION_FREQ, WEIGHT_POSITIVE_RATIO, WEIGHT_CONFIDENCE,
            SCORE_LOOKBACK_DAYS, MAX_HOLDINGS,
        )
        defaults = {
            "TRAILING_STOP_PCT": TRAILING_STOP_PCT,
            "STOP_LOSS_PCT": STOP_LOSS_PCT,
            "SCORE_THRESHOLD": SCORE_THRESHOLD,
            "WEIGHT_MENTION_FREQ": WEIGHT_MENTION_FREQ,
            "WEIGHT_POSITIVE_RATIO": WEIGHT_POSITIVE_RATIO,
            "WEIGHT_CONFIDENCE": WEIGHT_CONFIDENCE,
            "SCORE_LOOKBACK_DAYS": SCORE_LOOKBACK_DAYS,
            "MAX_HOLDINGS": MAX_HOLDINGS,
            "MAX_DAILY_BUYS": 2,
            "MIN_CASH_RATIO": 0.3,
        }
        return defaults.get(name, 0)

    def clamp(name: str, value: float) -> float:
        lo, hi = TUNABLE_PARAMS.get(name, (value, value))
        return round(max(lo, min(hi, value)), 3)

    # ── Rule 1: 승률 < 30% → 스코어 기준 강화 ──
    if metrics["win_rate"] < 30 and metrics["total_sells"] >= 3:
        old = cur("SCORE_THRESHOLD")
        new = clamp("SCORE_THRESHOLD", old + 0.05)
        if new != old:
            changes["SCORE_THRESHOLD"] = new
            reasons.append(f"[진입 강화] 승률 {metrics['win_rate']}% < 30% → SCORE_THRESHOLD {old:.2f} → {new:.2f}")

    # ── Rule 2: 평균손실 >> 평균이익 → 트레일링 스탑 타이트 ──
    if (metrics["avg_loss"] and metrics["avg_profit"]
            and abs(metrics["avg_loss"]) > abs(metrics["avg_profit"]) * 1.5
            and metrics["total_sells"] >= 3):
        old = cur("TRAILING_STOP_PCT")
        new = clamp("TRAILING_STOP_PCT", old + 0.5)  # less negative = tighter
        if new != old:
            changes["TRAILING_STOP_PCT"] = new
            reasons.append(
                f"[손절 강화] 평균손실 {metrics['avg_loss']:+.0f} >> 평균이익 {metrics['avg_profit']:+.0f}"
                f" → TRAILING_STOP {old:.1f} → {new:.1f}"
            )

    # ── Rule 3: 현금 비중 > 70% → 기준 완화 ──
    if metrics["cash_ratio"] > 70 and metrics["total_sells"] >= 2:
        old = cur("SCORE_THRESHOLD")
        new = clamp("SCORE_THRESHOLD", old - 0.03)
        if new != old and "SCORE_THRESHOLD" not in changes:
            changes["SCORE_THRESHOLD"] = new
            reasons.append(f"[유휴 현금] 현금 비중 {metrics['cash_ratio']:.0f}% > 70% → SCORE_THRESHOLD {old:.2f} → {new:.2f}")

    # ── Rule 4: PF < 1 → 긍정비율 가중치 축소, 신뢰도 강화 ──
    if metrics["profit_factor"] < 1.0 and metrics["total_sells"] >= 3:
        old_pr = cur("WEIGHT_POSITIVE_RATIO")
        old_cf = cur("WEIGHT_CONFIDENCE")
        new_pr = clamp("WEIGHT_POSITIVE_RATIO", old_pr - 0.05)
        new_cf = clamp("WEIGHT_CONFIDENCE", old_cf + 0.03)
        if new_pr != old_pr:
            changes["WEIGHT_POSITIVE_RATIO"] = new_pr
        if new_cf != old_cf:
            changes["WEIGHT_CONFIDENCE"] = new_cf
        # 정규화
        w_mf = cur("WEIGHT_MENTION_FREQ")
        total_w = w_mf + new_pr + new_cf
        if abs(total_w - 1.0) > 0.01:
            changes["WEIGHT_MENTION_FREQ"] = round(w_mf / total_w, 3)
            changes["WEIGHT_POSITIVE_RATIO"] = round(new_pr / total_w, 3)
            changes["WEIGHT_CONFIDENCE"] = round(new_cf / total_w, 3)
        reasons.append(f"[PF 저조] PF {metrics['profit_factor']:.2f} < 1.0 → 긍정비율↓ 신뢰도↑")

    # ── Rule 5: 연속 손실 ≥ 3일 → 보유 수 축소 ──
    if consecutive_loss_days >= 3:
        old = cur("MAX_HOLDINGS")
        new = clamp("MAX_HOLDINGS", old - 1)
        if new != old:
            changes["MAX_HOLDINGS"] = int(new)
            reasons.append(f"[방어] 연속 {consecutive_loss_days}일 손실 → MAX_HOLDINGS {int(old)} → {int(new)}")

    # ── Rule 6: 좋은 성과 → 보유 수 확대 ──
    if (metrics["win_rate"] > 60
            and metrics["profit_factor"] > 1.5
            and metrics["total_sells"] >= 5):
        old = cur("MAX_HOLDINGS")
        new = clamp("MAX_HOLDINGS", old + 1)
        if new != old and "MAX_HOLDINGS" not in changes:
            changes["MAX_HOLDINGS"] = int(new)
            reasons.append(
                f"[확장] 승률 {metrics['win_rate']}% PF {metrics['profit_factor']}"
                f" → MAX_HOLDINGS {int(old)} → {int(new)}"
            )

    # ── Rule 7: 보유 기간 짧음 → 트레일링 스탑 완화 ──
    if metrics["avg_holding_days"] < 2 and metrics["total_sells"] >= 3:
        old = cur("TRAILING_STOP_PCT")
        new = clamp("TRAILING_STOP_PCT", old - 0.5)  # more negative = looser
        if new != old and "TRAILING_STOP_PCT" not in changes:
            changes["TRAILING_STOP_PCT"] = new
            reasons.append(
                f"[스탑 완화] 평균 보유 {metrics['avg_holding_days']:.1f}일 < 2일"
                f" → TRAILING_STOP {old:.1f} → {new:.1f}"
            )

    reasoning = "\n".join(reasons) if reasons else "현재 파라미터 유지 (조정 불필요)"
    return changes, reasoning


# ── DB 헬퍼 함수 ──

def _get_all_dynamic_config() -> dict:
    """동적 설정값 전체 조회."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT param_name, param_value FROM dynamic_config WHERE is_active = 1"
        ).fetchall()
        return {r[0]: float(r[1]) for r in rows}
    except Exception:
        return {}
    finally:
        conn.close()


def _save_dynamic_config(param_name: str, value: float, source: str = "reflection"):
    """동적 설정값 저장 (UPSERT)."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO dynamic_config (param_name, param_value, updated_by, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(param_name) DO UPDATE SET
                   param_value = excluded.param_value,
                   updated_by = excluded.updated_by,
                   updated_at = excluded.updated_at""",
            (param_name, value, source, now),
        )
        conn.commit()
    finally:
        conn.close()


def _save_reflection(date: str, metrics: dict, parameter_changes: str, reasoning: str):
    """반성 로그 저장."""
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO reflection_log
               (date, total_sells, win_rate, avg_profit, avg_loss,
                profit_factor, total_pnl, total_return,
                current_holdings, cash_ratio, avg_holding_days,
                parameter_changes, reasoning)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (date,
             metrics.get("total_sells", 0),
             metrics.get("win_rate", 0),
             metrics.get("avg_profit", 0),
             metrics.get("avg_loss", 0),
             metrics.get("profit_factor", 0),
             metrics.get("total_pnl", 0),
             metrics.get("total_return", 0),
             metrics.get("current_holdings", 0),
             metrics.get("cash_ratio", 0),
             metrics.get("avg_holding_days", 0),
             parameter_changes, reasoning),
        )
        conn.commit()
    finally:
        conn.close()


def _get_reflections(limit: int = 5) -> list[dict]:
    """최근 반성 로그 조회."""
    conn = get_connection()
    try:
        conn.row_factory = __import__('sqlite3').Row
        rows = conn.execute(
            "SELECT * FROM reflection_log ORDER BY date DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


def _notify_reflection(metrics: dict, changes: dict | None, reasoning: str, code_suggestions: str = ""):
    """반성 결과 텔레그램 알림."""
    try:
        from notifier import _send
        lines = [
            "<b>🔍 반성의 시간</b>",
            "",
            f"📊 매도 {metrics['total_sells']}건 | 승률 {metrics['win_rate']:.0f}%",
            f"💰 순손익: {metrics['total_pnl']:+,}원 | PF: {metrics['profit_factor']:.2f}",
            f"📈 총수익률: {metrics['total_return']:+.2f}%",
            f"💵 현금 비중: {metrics['cash_ratio']:.0f}% | 보유: {metrics['current_holdings']}종목",
        ]

        if changes:
            lines.append("")
            lines.append(f"<b>⚙️ 파라미터 조정 ({len(changes)}건)</b>")
            for param, val in changes.items():
                lines.append(f"  {param}: {val}")

        lines.append("")
        lines.append(f"💭 {reasoning}")

        if code_suggestions:
            lines.append("")
            lines.append(f"💡 {code_suggestions}")

        _send("\n".join(lines))
    except Exception:
        logger.debug("반성 알림 전송 실패")


def get_reflection_dashboard_data() -> dict:
    """대시보드용 반성 데이터."""
    reflections = _get_reflections(limit=10)
    current_params = _get_all_dynamic_config()
    return {
        "reflections": reflections,
        "current_params": current_params,
        "tunable_params": {
            name: {"min": bounds[0], "max": bounds[1]}
            for name, bounds in TUNABLE_PARAMS.items()
        },
    }
