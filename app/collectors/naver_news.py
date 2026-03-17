"""네이버 검색 API를 이용한 주식 뉴스 수집"""

import re
import logging
import requests
from config import NAVER_CLIENT_ID, NAVER_CLIENT_SECRET, NEWS_KEYWORDS
from db import insert_news

logger = logging.getLogger(__name__)

# 네이버 검색 API 엔드포인트
SEARCH_URL = "https://openapi.naver.com/v1/search/news.json"

# HTML 태그 제거 정규식
HTML_TAG_RE = re.compile(r"<[^>]+>")

# 주식/경제 관련성 필터 키워드 (제목+설명에 하나라도 포함되어야 수집)
STOCK_RELEVANCE_KEYWORDS = {
    "코스피", "코스닥", "주가", "주식", "종목", "증시", "증권",
    "상장", "시가총액", "시총", "배당", "실적", "영업이익", "매출",
    "PER", "PBR", "ETF", "공모주", "IPO", "상한가", "하한가",
    "매수", "매도", "수급", "외국인", "기관", "개인투자자", "개미",
    "반도체", "2차전지", "바이오", "제약", "IT", "자동차",
    "삼성전자", "SK하이닉스", "LG에너지", "현대차", "네이버", "카카오",
    "금리", "환율", "유가", "원달러", "한국은행", "기준금리",
    "어닝", "흑자", "적자", "턴어라운드", "신고가", "급등", "급락",
    "수주", "계약", "인수", "합병", "M&A", "투자", "펀드",
    "코스피200", "나스닥", "S&P", "다우", "니케이",
    "테마주", "수혜주", "대장주", "관련주",
}


def _strip_html(text: str) -> str:
    """HTML 태그 제거 및 엔티티 디코딩"""
    if not text:
        return ""
    cleaned = HTML_TAG_RE.sub("", text)
    # 흔한 HTML 엔티티 처리
    cleaned = cleaned.replace("&amp;", "&")
    cleaned = cleaned.replace("&lt;", "<")
    cleaned = cleaned.replace("&gt;", ">")
    cleaned = cleaned.replace("&quot;", '"')
    cleaned = cleaned.replace("&#039;", "'")
    return cleaned.strip()


def _fetch_news(keyword: str, display: int = 100) -> list[dict]:
    """
    네이버 뉴스 검색 API 호출.
    display: 한 번에 가져올 건수 (최대 100)
    """
    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    params = {
        "query": keyword,
        "display": display,
        "start": 1,
        "sort": "date",  # 최신순 정렬
    }

    try:
        resp = requests.get(SEARCH_URL, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.error("네이버 뉴스 API 호출 실패 (keyword=%s): %s", keyword, e)
        return []

    results = []
    filtered = 0
    for item in data.get("items", []):
        title = _strip_html(item.get("title", ""))
        description = _strip_html(item.get("description", ""))
        text = f"{title} {description}"

        # 주식/경제 관련성 필터: 관련 키워드가 하나도 없으면 스킵
        if not any(kw in text for kw in STOCK_RELEVANCE_KEYWORDS):
            filtered += 1
            continue

        results.append(
            {
                "title": title,
                "link": item.get("originallink") or item.get("link", ""),
                "description": description,
                "source": item.get("link", ""),
                "pub_date": item.get("pubDate", ""),
                "keyword": keyword,
            }
        )

    if filtered:
        logger.info("관련성 필터: keyword='%s' / %d건 제외", keyword, filtered)

    return results


def collect_news():
    """모든 키워드에 대해 뉴스 수집 후 DB 저장"""
    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        logger.warning("네이버 API 키가 설정되지 않았습니다. 뉴스 수집을 건너뜁니다.")
        return

    total_fetched = 0
    total_inserted = 0

    for keyword in NEWS_KEYWORDS:
        items = _fetch_news(keyword)
        fetched = len(items)
        total_fetched += fetched

        if items:
            inserted = insert_news(items)
            total_inserted += inserted
            logger.info(
                "뉴스 수집: keyword='%s' / 조회=%d건 / 신규=%d건",
                keyword,
                fetched,
                inserted,
            )
        else:
            logger.info("뉴스 수집: keyword='%s' / 결과 없음", keyword)

    logger.info(
        "뉴스 수집 완료: 전체 조회=%d건 / 전체 신규=%d건",
        total_fetched,
        total_inserted,
    )
