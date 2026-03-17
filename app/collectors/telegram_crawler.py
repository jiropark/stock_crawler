"""
텔레그램 공개 채널 크롤러 (웹 스크래핑)

- t.me/s/채널명 페이지에서 메시지 수집 (인증 불필요)
- 주식 관련 메시지 필터링 → news 테이블에 저장 (감성분석 파이프라인 자동 연동)
- 채널별 rate limiting
"""

import logging
import re
import time
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup

from config import TG_CHANNELS
from db import insert_news

logger = logging.getLogger(__name__)

# 주식 관련 키워드 (메시지 필터링용)
STOCK_KEYWORDS = [
    # 매매/시장
    "매수", "매도", "상한가", "하한가", "급등", "급락",
    "종목", "주식", "코스피", "코스닥", "시총", "실적",
    "호재", "악재", "테마", "수급", "거래량", "신고가",
    "목표가", "손절", "익절", "추천", "분석", "수익률",
    "배당", "공매도", "기관", "외국인", "개인", "매집",
    # 차트/기술
    "차트", "지지", "저항", "돌파", "눌림", "반등",
    "전망", "리포트", "컨센서스", "어닝", "실적발표",
    # 공시/재무
    "공시", "시가총액", "자사주", "소각", "유상증자", "무상증자",
    "분할", "합병", "인수", "매출", "영업이익", "순이익",
    "PER", "PBR", "ROE", "EPS", "BPS",
    # 섹터/산업
    "반도체", "바이오", "2차전지", "배터리", "AI", "방산",
    "원전", "태양광", "수소", "로봇", "자율주행",
    # 주도주/섹터 분석
    "주도주", "대장주", "섹터", "업종", "ETF",
    "삼성", "SK", "LG", "현대", "카카오", "네이버",
]

CHANNEL_DELAY = 3          # 채널 간 대기 시간(초)
MIN_MESSAGE_LENGTH = 10    # 최소 메시지 길이
CUTOFF_HOURS = 48          # 최근 N시간 메시지만 저장 (주말 대응)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9",
}


def _is_stock_related(text: str) -> bool:
    """메시지가 주식 관련인지 판단"""
    if not text or len(text) < MIN_MESSAGE_LENGTH:
        return False
    return any(kw in text for kw in STOCK_KEYWORDS)


def _parse_channel_page(channel_username: str) -> list[dict]:
    """t.me/s/채널명 페이지를 파싱하여 메시지 목록 반환"""
    url = f"https://t.me/s/{channel_username}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error("채널 페이지 요청 실패 (%s): %s", channel_username, e)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    messages = []

    # 채널 제목 추출
    title_el = soup.select_one(".tgme_channel_info_header_title")
    channel_title = title_el.get_text(strip=True) if title_el else channel_username

    # 메시지 위젯 파싱
    for widget in soup.select(".tgme_widget_message_wrap"):
        msg_el = widget.select_one(".tgme_widget_message_text")
        if not msg_el:
            continue

        text = msg_el.get_text(separator=" ", strip=True)
        if not text:
            continue

        # 메시지 ID 추출
        msg_bubble = widget.select_one(".tgme_widget_message")
        msg_id = ""
        if msg_bubble and msg_bubble.get("data-post"):
            msg_id = msg_bubble["data-post"].split("/")[-1]

        # 날짜 추출
        time_el = widget.select_one("time[datetime]")
        pub_date = ""
        if time_el and time_el.get("datetime"):
            pub_date = time_el["datetime"]

        # 최근 메시지만 필터링
        if pub_date:
            try:
                msg_time = datetime.fromisoformat(pub_date.replace("Z", "+00:00"))
                cutoff = datetime.now(msg_time.tzinfo) - timedelta(hours=CUTOFF_HOURS)
                if msg_time < cutoff:
                    continue
            except (ValueError, TypeError):
                pass

        link = f"https://t.me/{channel_username}/{msg_id}" if msg_id else f"https://t.me/s/{channel_username}#{msg_id}"

        messages.append({
            "title": text[:200],
            "link": link,
            "description": text,
            "source": f"telegram:{channel_title}",
            "pub_date": pub_date,
            "keyword": "텔레그램",
        })

    return messages


def collect_telegram():
    """텔레그램 공개 채널에서 주식 관련 메시지 수집"""
    channels = [ch.strip() for ch in TG_CHANNELS if ch.strip()]
    if not channels:
        logger.warning("텔레그램 채널 목록 비어있음, 건너뜀")
        return

    total_collected = 0

    for channel_username in channels:
        try:
            all_messages = _parse_channel_page(channel_username)
            stock_messages = [m for m in all_messages if _is_stock_related(m["description"])]

            if stock_messages:
                inserted = insert_news(stock_messages)
                total_collected += inserted
                logger.info(
                    "텔레그램 수집: %s / 전체=%d건 / 관련=%d건 / 신규=%d건",
                    channel_username, len(all_messages), len(stock_messages), inserted,
                )
            else:
                logger.debug(
                    "텔레그램: %s / 전체=%d건 / 주식 관련 없음",
                    channel_username, len(all_messages),
                )

            time.sleep(CHANNEL_DELAY)

        except Exception:
            logger.exception("채널 수집 실패: %s", channel_username)

    logger.info("텔레그램 수집 완료: 총 신규 %d건", total_collected)
