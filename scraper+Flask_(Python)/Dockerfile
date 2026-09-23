FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app
RUN addgroup --system webharbor && adduser --system --ingroup webharbor webharbor

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install --with-deps chromium && chmod -R a+rX /ms-playwright

COPY --chown=webharbor:webharbor . .
RUN mkdir -p /app/data && chown -R webharbor:webharbor /app

USER webharbor
EXPOSE 8000
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "4", "--timeout", "60", "App:app"]
