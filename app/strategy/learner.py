"""
AI 학습 엔진

매매 결과를 기반으로 가중치를 자동 조정하고,
감성분석 정확도를 추적하며, 자체 분류 규칙을 학습한다.
"""

import logging
import re
import json
from datetime import datetime, timedelta
from collections import defaultdict

from db import get_connection
from config import (
    WEIGHT_MENTION_FREQ, WEIGHT_POSITIVE_RATIO, WEIGHT_CONFIDENCE,
    SCORE_THRESHOLD,
)

logger = logging.getLogger(__name__)

# ── 1. Outcome Tracking ──

def record_prediction(stock_code: str, stock_name: str, news_ids: list[int],
                      sentiment: str, confidence: float, score: float, price: float):
    """매수 시점의 예측 기록"""
    conn = get_connection()
    try:
        for nid in news_ids:
            conn.execute(
                """INSERT INTO prediction_outcomes
                   (stock_code, stock_name, news_id, predicted_sentiment,
                    confidence, score_at_buy, price_at_prediction)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (stock_code, stock_name, nid, sentiment, confidence, score, price)
            )
        conn.commit()
    finally:
        conn.close()


def evaluate_predictions():
    """미평가 예측의 실제 결과를 채움 (1일/3일/5일 후 주가 확인)"""
    from strategy.trader import get_current_price
    conn = get_connection()
    try:
        conn.row_factory = __import__('sqlite3').Row
        # 생성 후 1일 이상 지난 미평가 예측
        rows = conn.execute(
            """SELECT id, stock_code, price_at_prediction, created_at,
                      price_after_1d, price_after_3d, price_after_5d
               FROM prediction_outcomes
               WHERE evaluated_at IS NULL
                 AND created_at <= datetime('now', 'localtime', '-1 day')
               LIMIT 50"""
        ).fetchall()

        now = datetime.now()
        for row in rows:
            row = dict(row)
            created = datetime.strptime(row['created_at'][:19], "%Y-%m-%d %H:%M:%S")
            days_passed = (now - created).days
            base_price = row['price_at_prediction']
            if not base_price or base_price <= 0:
                continue

            current = get_current_price(row['stock_code'])
            if current is None:
                continue

            updates = {}
            # Fill in timeframe returns
            if days_passed >= 1 and row['price_after_1d'] is None:
                updates['price_after_1d'] = current
                updates['actual_return_1d'] = round((current - base_price) / base_price * 100, 2)
            if days_passed >= 3 and row['price_after_3d'] is None:
                updates['price_after_3d'] = current
                updates['actual_return_3d'] = round((current - base_price) / base_price * 100, 2)
            if days_passed >= 5 and row['price_after_5d'] is None:
                updates['price_after_5d'] = current
                updates['actual_return_5d'] = round((current - base_price) / base_price * 100, 2)

            if updates:
                set_clause = ", ".join(f"{k}=?" for k in updates)
                values = list(updates.values())

                # Mark as evaluated if 5 days passed
                if days_passed >= 5:
                    set_clause += ", evaluated_at=?, was_correct=?"
                    values.append(now.strftime("%Y-%m-%d %H:%M:%S"))
                    # positive prediction correct if 3-day return > 0
                    ret_3d = updates.get('actual_return_3d', row.get('actual_return_3d'))
                    values.append(1 if ret_3d and ret_3d > 0 else 0)

                values.append(row['id'])
                conn.execute(f"UPDATE prediction_outcomes SET {set_clause} WHERE id=?", values)

        conn.commit()
        logger.info("예측 평가 완료: %d건 처리", len(rows))
    except Exception:
        logger.exception("예측 평가 실패")
    finally:
        conn.close()


# ── 2. Sentiment Accuracy ──

def get_sentiment_accuracy(days: int = 30) -> dict:
    """감성분석 정확도 계산"""
    conn = get_connection()
    try:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = conn.execute(
            """SELECT predicted_sentiment, was_correct
               FROM prediction_outcomes
               WHERE was_correct IS NOT NULL AND created_at >= ?""",
            (cutoff,)
        ).fetchall()

        if not rows:
            return {"total": 0, "accuracy": 0, "positive_precision": 0, "negative_precision": 0}

        total = len(rows)
        correct = sum(1 for r in rows if r[1] == 1)

        pos_total = sum(1 for r in rows if r[0] == 'positive')
        pos_correct = sum(1 for r in rows if r[0] == 'positive' and r[1] == 1)
        neg_total = sum(1 for r in rows if r[0] == 'negative')
        neg_correct = sum(1 for r in rows if r[0] == 'negative' and r[1] == 1)

        return {
            "total": total,
            "accuracy": round(correct / total * 100, 1) if total else 0,
            "positive_precision": round(pos_correct / pos_total * 100, 1) if pos_total else 0,
            "negative_precision": round(neg_correct / neg_total * 100, 1) if neg_total else 0,
            "correct": correct,
        }
    finally:
        conn.close()


# ── 3. Weight Auto-Adjustment ──

def get_current_weights() -> dict:
    """현재 가중치 조회 (dynamic_config → ai_weight_history → config 기본값)"""
    # 1순위: reflection 엔진의 dynamic_config
    try:
        from config import get_param
        w_mf = get_param("WEIGHT_MENTION_FREQ")
        w_pr = get_param("WEIGHT_POSITIVE_RATIO")
        w_cf = get_param("WEIGHT_CONFIDENCE")
        st = get_param("SCORE_THRESHOLD")
        # dynamic_config에 실제 값이 있는지 확인
        conn = get_connection()
        dc_rows = conn.execute(
            "SELECT param_name FROM dynamic_config WHERE param_name IN ('WEIGHT_MENTION_FREQ','WEIGHT_POSITIVE_RATIO','WEIGHT_CONFIDENCE')"
        ).fetchall()
        conn.close()
        if dc_rows:
            return {
                "w_mention_freq": w_mf or WEIGHT_MENTION_FREQ,
                "w_positive_ratio": w_pr or WEIGHT_POSITIVE_RATIO,
                "w_confidence": w_cf or WEIGHT_CONFIDENCE,
                "score_threshold": st or SCORE_THRESHOLD,
            }
    except Exception:
        pass

    # 2순위: ai_weight_history
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT w_mention_freq, w_positive_ratio, w_confidence, score_threshold "
            "FROM ai_weight_history ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            return {
                "w_mention_freq": row[0],
                "w_positive_ratio": row[1],
                "w_confidence": row[2],
                "score_threshold": row[3],
            }
        return {
            "w_mention_freq": WEIGHT_MENTION_FREQ,
            "w_positive_ratio": WEIGHT_POSITIVE_RATIO,
            "w_confidence": WEIGHT_CONFIDENCE,
            "score_threshold": SCORE_THRESHOLD,
        }
    finally:
        conn.close()


def adjust_weights():
    """매매 결과 기반 가중치 분석 (반성 엔진에 데이터 제공만, 직접 조정하지 않음).

    Note: 실제 가중치 조정은 strategy/reflection.py가 담당합니다.
    이 함수는 성과 데이터를 ai_weight_history에 기록하여 반성 엔진의 참고 자료로 활용합니다.
    """
    conn = get_connection()
    try:
        # 최근 기록 확인 (7일 이내면 스킵)
        last = conn.execute(
            "SELECT date FROM ai_weight_history ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if last:
            last_date = datetime.strptime(last[0], "%Y-%m-%d")
            if (datetime.now() - last_date).days < 7:
                logger.info("가중치 기록: 최근 7일 내 기록됨, 스킵")
                return

        # 최근 30일 매도 기록 분석
        conn.row_factory = __import__('sqlite3').Row
        trades = conn.execute(
            """SELECT t.stock_code, t.pnl, t.score, t.traded_at
               FROM trades t
               WHERE t.trade_type = 'SELL' AND t.traded_at >= datetime('now', 'localtime', '-30 days')"""
        ).fetchall()

        if len(trades) < 3:
            logger.info("가중치 기록: 매매 데이터 부족 (%d건)", len(trades))
            return

        wins = [dict(t) for t in trades if t['pnl'] and t['pnl'] > 0]
        losses = [dict(t) for t in trades if t['pnl'] and t['pnl'] <= 0]
        total = len(wins) + len(losses)
        win_rate = len(wins) / total * 100 if total else 50

        accuracy = get_sentiment_accuracy(30)
        sent_acc = accuracy['accuracy']

        # 현재 가중치 (반성 엔진 dynamic_config 또는 config 기본값)
        current = get_current_weights()

        # 기록만 (조정하지 않음)
        now = datetime.now()
        conn.execute(
            """INSERT INTO ai_weight_history
               (date, w_mention_freq, w_positive_ratio, w_confidence,
                score_threshold, sentiment_accuracy, win_rate, avg_return, adjustment_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (now.strftime("%Y-%m-%d"),
             current['w_mention_freq'], current['w_positive_ratio'], current['w_confidence'],
             current['score_threshold'], sent_acc, win_rate,
             sum(t['pnl'] for t in trades if t['pnl']) / total if total else 0,
             f"기록만 (조정은 reflection 담당, 승률={win_rate:.0f}%, 감성정확도={sent_acc:.0f}%)")
        )
        conn.commit()
        logger.info("가중치 기록 완료: 승률=%.0f%%, 감성정확도=%.0f%%", win_rate, sent_acc)
    except Exception:
        logger.exception("가중치 기록 실패")
    finally:
        conn.close()


# ── 4. Local Sentiment Classification ──

def learn_sentiment_rules():
    """확인된 예측 결과에서 키워드-감성 규칙 학습"""
    conn = get_connection()
    try:
        # 평가 완료된 예측의 뉴스 제목 분석
        rows = conn.execute(
            """SELECT n.title, po.predicted_sentiment, po.was_correct
               FROM prediction_outcomes po
               JOIN news n ON po.news_id = n.id
               WHERE po.was_correct IS NOT NULL"""
        ).fetchall()

        if len(rows) < 10:
            return

        # 키워드별 감성 통계
        keyword_stats = defaultdict(lambda: {"positive": 0, "negative": 0, "neutral": 0, "correct": 0, "total": 0})

        # 주요 키워드 추출 (2글자 이상 한글 단어)
        for title, sentiment, correct in rows:
            words = re.findall(r'[가-힣]{2,}', title or '')
            for word in words:
                if word in ('대한', '에서', '으로', '이번', '올해', '지난', '관련', '통해'):
                    continue  # 불용어 스킵
                stats = keyword_stats[word]
                stats[sentiment] += 1
                stats['total'] += 1
                if correct:
                    stats['correct'] += 1

        # 강한 패턴 규칙 저장 (20회 이상, 70% 이상 한 감성에 집중)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for keyword, stats in keyword_stats.items():
            if stats['total'] < 20:
                continue
            for sent in ['positive', 'negative']:
                ratio = stats[sent] / stats['total']
                if ratio >= 0.7:
                    accuracy = stats['correct'] / stats['total'] if stats['total'] else 0
                    # UPSERT
                    conn.execute(
                        """INSERT INTO local_sentiment_rules
                           (keyword, pattern, predicted_sentiment, confidence,
                            correct_count, total_count, accuracy, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(keyword, pattern) DO UPDATE SET
                               predicted_sentiment = excluded.predicted_sentiment,
                               confidence = excluded.confidence,
                               correct_count = excluded.correct_count,
                               total_count = excluded.total_count,
                               accuracy = excluded.accuracy,
                               updated_at = excluded.updated_at""",
                        (keyword, 'keyword_match', sent, round(ratio, 2),
                         stats['correct'], stats['total'], round(accuracy, 2), now)
                    )

        conn.commit()
        logger.info("감성 규칙 학습 완료: %d개 키워드 분석", len(keyword_stats))
    except Exception:
        logger.exception("감성 규칙 학습 실패")
    finally:
        conn.close()


def classify_locally(title: str, description: str = "") -> tuple[str, float] | None:
    """로컬 규칙으로 감성 분류 시도. 성공 시 (sentiment, confidence), 불확실하면 None"""
    conn = get_connection()
    try:
        rules = conn.execute(
            """SELECT keyword, predicted_sentiment, confidence, accuracy
               FROM local_sentiment_rules
               WHERE is_active = 1 AND accuracy >= 0.7 AND total_count >= 20
               ORDER BY accuracy DESC"""
        ).fetchall()

        if not rules:
            return None

        text = f"{title} {description}"
        matches = defaultdict(lambda: {"score": 0, "count": 0})

        for keyword, sentiment, conf, accuracy in rules:
            if keyword in text:
                matches[sentiment]["score"] += conf * accuracy
                matches[sentiment]["count"] += 1

        if not matches:
            return None

        # 가장 강한 매칭
        best = max(matches.items(), key=lambda x: x[1]["score"])
        sentiment = best[0]
        avg_score = best[1]["score"] / best[1]["count"] if best[1]["count"] else 0

        # 확신도가 높을 때만 로컬 분류
        if avg_score >= 0.6 and best[1]["count"] >= 2:
            return (sentiment, round(avg_score, 2))

        return None
    except Exception:
        logger.exception("로컬 분류 실패")
        return None
    finally:
        conn.close()


# ── 5. Learning Metrics Snapshot ──

def record_daily_metrics():
    """일별 학습 지표 기록"""
    conn = get_connection()
    try:
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")

        accuracy = get_sentiment_accuracy(30)

        # 로컬 분류율 계산
        local_rules = conn.execute(
            "SELECT COUNT(*) FROM local_sentiment_rules WHERE is_active = 1 AND accuracy >= 0.7"
        ).fetchone()[0]

        # Gemini 절약 추정 (로컬 규칙 수 * 평균 매칭률)
        total_news = conn.execute(
            "SELECT COUNT(*) FROM news WHERE analyzed_at >= ?",
            ((now - timedelta(days=7)).strftime("%Y-%m-%d"),)
        ).fetchone()[0]

        conn.execute(
            """INSERT INTO ai_learning_metrics
               (date, sentiment_accuracy, positive_precision, negative_precision,
                total_predictions, correct_predictions, local_classification_rate,
                gemini_calls_saved)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(date) DO UPDATE SET
                   sentiment_accuracy = excluded.sentiment_accuracy,
                   positive_precision = excluded.positive_precision,
                   negative_precision = excluded.negative_precision,
                   total_predictions = excluded.total_predictions,
                   correct_predictions = excluded.correct_predictions,
                   local_classification_rate = excluded.local_classification_rate,
                   gemini_calls_saved = excluded.gemini_calls_saved""",
            (today, accuracy['accuracy'], accuracy['positive_precision'],
             accuracy['negative_precision'], accuracy['total'], accuracy.get('correct', 0),
             round(local_rules / max(total_news, 1) * 100, 1), local_rules)
        )
        conn.commit()
        logger.info("학습 지표 기록 완료: 정확도=%.1f%%", accuracy['accuracy'])
    except Exception:
        logger.exception("학습 지표 기록 실패")
    finally:
        conn.close()


# ── 6. All-in-One Learning Cycle ──

def run_learning_cycle():
    """전체 학습 사이클 실행 (스케줄러에서 호출)"""
    logger.info("=== AI 학습 사이클 시작 ===")
    evaluate_predictions()
    learn_sentiment_rules()
    adjust_weights()
    record_daily_metrics()
    logger.info("=== AI 학습 사이클 완료 ===")


def get_learning_dashboard_data() -> dict:
    """대시보드용 학습 데이터 종합"""
    conn = get_connection()
    try:
        conn.row_factory = __import__('sqlite3').Row

        # 최근 학습 지표
        metrics_rows = conn.execute(
            "SELECT * FROM ai_learning_metrics ORDER BY date DESC LIMIT 30"
        ).fetchall()
        metrics = [dict(r) for r in metrics_rows]

        # 가중치 변경 이력
        weight_rows = conn.execute(
            "SELECT * FROM ai_weight_history ORDER BY id DESC LIMIT 20"
        ).fetchall()
        weights = [dict(r) for r in weight_rows]

        # 현재 가중치
        current_weights = get_current_weights()

        # 감성 정확도
        accuracy_30d = get_sentiment_accuracy(30)
        accuracy_7d = get_sentiment_accuracy(7)

        # 활성 로컬 규칙 수
        local_rules = conn.execute(
            "SELECT COUNT(*) FROM local_sentiment_rules WHERE is_active = 1 AND accuracy >= 0.7"
        ).fetchone()[0]

        total_rules = conn.execute(
            "SELECT COUNT(*) FROM local_sentiment_rules"
        ).fetchone()[0]

        # 예측 통계
        pred_stats = conn.execute(
            """SELECT
                   COUNT(*) as total,
                   COUNT(CASE WHEN was_correct = 1 THEN 1 END) as correct,
                   COUNT(CASE WHEN was_correct IS NOT NULL THEN 1 END) as evaluated
               FROM prediction_outcomes"""
        ).fetchone()

        # 데이터 수집 일수
        first_news = conn.execute("SELECT MIN(collected_at) FROM news").fetchone()
        data_days = 0
        if first_news and first_news[0]:
            start = datetime.strptime(first_news[0][:10], "%Y-%m-%d")
            data_days = (datetime.now() - start).days + 1

        # 매매 건수 (ML 도입 판단용)
        trade_stats = conn.execute(
            """SELECT COUNT(*) as total,
                      COUNT(CASE WHEN pnl > 0 THEN 1 END) as wins
               FROM trades WHERE trade_type = 'SELL'"""
        ).fetchone()
        total_trades = trade_stats[0] if trade_stats else 0
        trade_win_rate = round(trade_stats[1] / total_trades * 100, 1) if total_trades > 0 else 0

        return {
            "current_weights": current_weights,
            "accuracy_30d": accuracy_30d,
            "accuracy_7d": accuracy_7d,
            "weight_history": weights,
            "metrics_history": metrics,
            "local_rules_active": local_rules,
            "local_rules_total": total_rules,
            "predictions_total": pred_stats[0] if pred_stats else 0,
            "predictions_correct": pred_stats[1] if pred_stats else 0,
            "predictions_evaluated": pred_stats[2] if pred_stats else 0,
            "data_days": data_days,
            "total_trades": total_trades,
            "trade_win_rate": trade_win_rate,
        }
    except Exception:
        logger.exception("학습 대시보드 데이터 조회 실패")
        return {
            "current_weights": get_current_weights(),
            "accuracy_30d": {"total": 0, "accuracy": 0, "positive_precision": 0, "negative_precision": 0},
            "accuracy_7d": {"total": 0, "accuracy": 0, "positive_precision": 0, "negative_precision": 0},
            "weight_history": [],
            "metrics_history": [],
            "local_rules_active": 0,
            "local_rules_total": 0,
            "predictions_total": 0,
            "predictions_correct": 0,
            "predictions_evaluated": 0,
            "data_days": 0,
            "total_trades": 0,
            "trade_win_rate": 0,
        }
    finally:
        conn.close()
