FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY ebag ./ebag
RUN pip install --no-cache-dir . && useradd --uid 10001 --create-home agent && mkdir -p /app/data && chown agent:agent /app/data
COPY config ./config
USER agent
CMD ["ebag", "schedule", "--send"]
