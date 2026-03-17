"""OpenDART API를 이용한 공시 수집"""

import logging
from datetime import datetime

import OpenDartReader
from config import DART_API_KEY
from db import insert_dart_disclosures

logger = logging.getLogger(__name__)

# 주요사항보고서 및 투자판단 관련 공시 필터 키워드
REPORT_KEYWORDS = [
    "주요사항보고서",
    "투자판단",
    "대규모내부거래",
    "합병",
    "분할",
    "주식교환",
    "영업양수도",
    "유상증자",
    "무상증자",
    "전환사채",
    "신주인수권",
    "자기주식",
    "배당",
    "실적",
    "매출",
]


def _matches_filter(report_nm: str) -> bool:
    """보고서명이 관심 키워드에 해당하는지 확인"""
    if not report_nm:
        return False
    return any(kw in report_nm for kw in REPORT_KEYWORDS)


def collect_dart():
    """오늘 날짜 DART 공시 수집 후 DB 저장"""
    if not DART_API_KEY:
        logger.warning("DART API 키가 설정되지 않았습니다. 공시 수집을 건너뜁니다.")
        return

    today = datetime.now().strftime("%Y-%m-%d")

    try:
        dart = OpenDartReader(DART_API_KEY)
        # 오늘 날짜 전체 공시 목록 조회
        df = dart.list(start=today, end=today)
    except Exception as e:
        logger.error("DART 공시 목록 조회 실패: %s", e)
        return

    if df is None or df.empty:
        logger.info("DART 공시: 오늘(%s) 공시 없음", today)
        return

    total = len(df)
    logger.info("DART 공시: 오늘(%s) 전체 %d건 조회", today, total)

    # 주요사항보고서/투자판단 관련 필터링
    filtered_rows = []
    for _, row in df.iterrows():
        report_nm = str(row.get("report_nm", ""))
        if _matches_filter(report_nm):
            filtered_rows.append(
                {
                    "corp_name": str(row.get("corp_name", "")),
                    "corp_code": str(row.get("corp_code", "")),
                    "report_nm": report_nm,
                    "rcept_no": str(row.get("rcept_no", "")),
                    "rcept_dt": str(row.get("rcept_dt", "")),
                }
            )

    if not filtered_rows:
        logger.info("DART 공시: 필터 후 관련 공시 0건")
        return

    inserted = insert_dart_disclosures(filtered_rows)
    logger.info(
        "DART 공시 수집 완료: 필터=%d건 / 신규=%d건",
        len(filtered_rows),
        inserted,
    )
