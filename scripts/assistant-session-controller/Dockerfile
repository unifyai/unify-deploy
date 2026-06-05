FROM python:3.12-slim

WORKDIR /app

COPY scripts/assistant-session-controller/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app
RUN pip install --no-cache-dir /app

CMD ["kopf", "run", "-m", "communication.assistant_session_controller.controller", "--liveness=http://0.0.0.0:8080/healthz", "--namespace=production", "--standalone"]
