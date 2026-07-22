FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 소스코드 및 프롬프트 파일 복사
COPY main.py .
COPY prompt.txt .   <-- 이 줄이 반드시 있어야 합니다!

ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
