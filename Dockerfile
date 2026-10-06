FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV NOBITEX_API_ENV=testnet
ENV NOBITEX_TEST_MODE=true

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY nobitex_bot.py .

CMD ["python", "nobitex_bot.py"]
