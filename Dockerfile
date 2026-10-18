# Linux image for MCP registries and inspection (e.g. Glama): the server starts and lists its tools.
# Driving ETABS itself needs Windows with ETABS 22+, where the server is installed with pip or the .mcpb bundle.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install .

CMD ["etabs-mcp"]
