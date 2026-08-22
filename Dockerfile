FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium

COPY app.py .
COPY templates ./templates

EXPOSE 8000

CMD ["gunicorn", "-b", "0.0.0.0:8000", "-w", "1", "--threads", "16", "--timeout", "300", "app:app"]
