FROM python:3.11-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
COPY .deps/agentscope-2.0.1-py3-none-any.whl /tmp/
COPY pyproject.toml /app/
COPY src /app/src
RUN pip install /tmp/agentscope-2.0.1-py3-none-any.whl && pip install /app
COPY db /app/db
COPY contracts /app/contracts
EXPOSE 8080
CMD ["python", "-m", "eagent_gateway.main"]
