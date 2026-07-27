FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# pip install . can ca pyproject lan source (khong tach layer duoc voi setuptools);
# .dockerignore da loai .venv/db/eval_results nen context van nho
COPY pyproject.toml ./
COPY laplace ./laplace

RUN pip install --no-cache-dir .

# Thu muc fallback SQLite (data/) + bao cao markdown (reports/). Khi
# LAPLACE_DB_URL tro den Supabase, volume data chi duoc giu cho rollback.
RUN mkdir -p /app/data /app/reports

EXPOSE 8010

# /openapi.json do FastAPI phuc vu san, KHONG bi chan boi LAPLACE_API_KEY
# -> healthcheck van chay duoc khi API key bat. Dung httpx (da la dependency)
# vi image slim khong co curl/wget.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import httpx, sys; sys.exit(0 if httpx.get('http://localhost:8010/openapi.json', timeout=4).status_code == 200 else 1)"]

CMD ["python", "-m", "laplace"]
