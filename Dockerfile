FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
COPY evals ./evals
RUN pip install --no-cache-dir ".[agent]"

RUN useradd --create-home babbel
USER babbel

EXPOSE 8000
CMD ["babbel-api"]
