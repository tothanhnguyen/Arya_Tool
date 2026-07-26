FROM python:3.12-slim

WORKDIR /app

# Cai dependency truoc de tan dung layer cache
COPY pyproject.toml ./
COPY laplace ./laplace

RUN pip install --no-cache-dir .

# Thu muc data cho SQLite (mount volume tu docker-compose)
RUN mkdir -p /app/data

EXPOSE 8000

CMD ["python", "-m", "laplace"]
