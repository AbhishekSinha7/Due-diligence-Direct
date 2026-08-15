# Container image for the DueDiligence Direct fleet control plane on Cloud Run.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    FLEET_STATE_DIR=/var/fleet \
    FLEET_TELEMETRY_DIR=/var/fleet/telemetry \
    DUE_DILIGENCE_RUNS_DIR=/var/fleet/runs

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY sample_data_room ./sample_data_room
COPY sample_data_room_hostile ./sample_data_room_hostile
COPY demo_scenarios.json ./

RUN mkdir -p /var/fleet/telemetry /var/fleet/runs /app/data_room

# Cloud Run injects PORT; default to 8080 for local container runs.
ENV PORT=8080
EXPOSE 8080

CMD exec uvicorn service:app --host 0.0.0.0 --port ${PORT} --workers 1
