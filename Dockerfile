FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py google_integration.py infra.py worker.py team.py ./
COPY templates ./templates
COPY static ./static
RUN mkdir -p /app/data && useradd -m -u 10001 appuser && chown -R appuser:appuser /app/data
USER appuser
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
