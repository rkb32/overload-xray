FROM python:3.13-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY demo ./demo
COPY xray ./xray
COPY results ./results

# Nothing here needs root. A compromised process should not own the container.
RUN useradd --system --uid 10001 --no-create-home app
USER app

# --host 0.0.0.0: inside a container 127.0.0.1 is visible only to the container itself,
# so the server must listen on every interface for other containers (or the host) to reach it.
CMD ["uvicorn", "demo.service_b:app", "--host", "0.0.0.0", "--port", "8001"]
