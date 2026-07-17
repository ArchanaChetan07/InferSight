# InferSight sidecar (+ mock vLLM for the demo stack)
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY infersight/ infersight/
COPY examples/ examples/
RUN pip install --no-cache-dir .
EXPOSE 8020
ENTRYPOINT ["infersight", "run"]
CMD ["--upstream", "http://vllm:8000"]
