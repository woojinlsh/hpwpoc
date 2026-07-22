FROM python:3.11-slim

WORKDIR /app

# 필요 라이브러리 설치
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 소스코드 복사
COPY main.py .

# 파이썬 버퍼링 해제 (Coolify 로그 실시간 출력용)
ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
