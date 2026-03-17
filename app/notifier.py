"""
텔레그램 봇 알림 모듈

1. 매수/매도 알림 — 매매 발생 시 즉시 푸시
2. 일일 리포트 — 리밸런싱 완료 후 요약
"""

import logging
import requests

from config import TG_BOT_TOKEN, TG_CHAT_ID

logger = logging.getLogger(__name__)

API_URL = "https://api.telegram.org/bot{token}/sendMessage"
TIMEOUT = 10


def _send(text: str, parse_mode: str = "HTML"):
    """텔레그램 메시지 전송"""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return

    try:
        resp = requests.post(
            API_URL.format(token=TG_BOT_TOKEN),
            json={
                "chat_id": TG_CHAT_ID,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            },
            timeout=TIMEOUT,
        )
        if not resp.json().get("ok"):
            logger.warning("텔레그램 전송 실패: %s", resp.text)
    except Exception:
        logger.exception("텔레그램 전송 에러")


def notify_buy(stock_name: str, stock_code: str, quantity: int,
               price: float, total_amount: float, score: float):
    """매수 알림"""
    text = (
        f"<b>📈 매수</b>\n"
        f"종목: {stock_name} ({stock_code})\n"
        f"수량: {quantity}주 @ {price:,.0f}원\n"
        f"금액: {total_amount:,.0f}원\n"
        f"스코어: {score:.2f}"
    )
    _send(text)


def notify_sell(stock_name: str, stock_code: str, quantity: int,
                price: float, pnl: float, reason: str):
    """매도 알림"""
    emoji = "📉" if pnl < 0 else "📊"
    sign = "+" if pnl >= 0 else ""
    text = (
        f"<b>{emoji} 매도</b>\n"
        f"종목: {stock_name} ({stock_code})\n"
        f"수량: {quantity}주 @ {price:,.0f}원\n"
        f"손익: {sign}{pnl:,.0f}원\n"
        f"사유: {reason}"
    )
    _send(text)


def notify_daily_report(total_value: float, cash: float, holdings_value: float,
                        total_return: float, daily_return: float,
                        holdings: list, wins: int, losses: int,
                        news_count: int = 0, tg_count: int = 0):
    """일일 리포트"""
    sign = "+" if total_return >= 0 else ""
    d_sign = "+" if daily_return >= 0 else ""

    lines = [
        f"<b>📋 일일 리포트</b>",
        f"",
        f"💰 총자산: {total_value:,.0f}원 ({sign}{total_return:.1f}%)",
        f"  현금: {cash:,.0f}원 / 주식: {holdings_value:,.0f}원",
        f"  일간: {d_sign}{daily_return:.1f}%",
        f"",
    ]

    if holdings:
        lines.append(f"<b>📌 보유종목 ({len(holdings)})</b>")
        for h in holdings:
            pnl_pct = h.get("pnl_pct", 0)
            p_sign = "+" if pnl_pct >= 0 else ""
            lines.append(f"  {h['stock_name']}: {p_sign}{pnl_pct:.1f}%")
        lines.append("")

    win_rate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0
    lines.append(f"📊 승률: {win_rate:.0f}% ({wins}승 {losses}패)")

    if news_count or tg_count:
        lines.append(f"📰 오늘 수집: 뉴스 {news_count}건 / 텔레그램 {tg_count}건")

    _send("\n".join(lines))
