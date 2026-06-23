FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

# Drop privileges.
RUN useradd --uid 65532 --create-home --shell /usr/sbin/nologin nonroot
USER 65532

# Standalone = single active replica (no peering CRD required). For HA, enable
# Kopf peering and run multiple replicas.
ENTRYPOINT ["kopf", "run", "--standalone", "--all-namespaces", "-m", "tfy_netpol_operator.operator"]
