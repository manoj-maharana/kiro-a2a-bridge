# kiro-a2a-bridge `serve` in a container.
#
#   docker build -t kiro-a2a-bridge .
#   docker run --rm -p 8765:8765 kiro-a2a-bridge serve --agent demo --host 0.0.0.0
#
# For a real agent, build your own image FROM this one and install the agent
# CLI (for example kiro-cli) plus its sign-in, then mount your code at /workspace.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install ".[all]" && useradd --create-home --uid 1000 agent && mkdir /workspace && chown agent /workspace

USER agent
WORKDIR /workspace
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=4)"
ENTRYPOINT ["kiro-a2a"]
CMD ["serve", "--host", "0.0.0.0", "--workdir", "/workspace"]
