FROM python:3.11-slim

WORKDIR /app

# 시스템 패키지 설치 (tzdata로 타임존 지원)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# 의존성 설치
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 앱 코드 복사
COPY app/ .

# 데이터/로그 디렉토리 생성
RUN mkdir -p /data /logs

CMD ["python", "main.py"]
