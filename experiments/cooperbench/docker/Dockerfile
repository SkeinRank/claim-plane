ARG BASE_IMAGE=python:3.12.11-bookworm@sha256:9bed8554e926c07c6f908841d5ee88c33e8df9236b191526bbce81a9062ab43a
FROM ${BASE_IMAGE}

ARG UV_VERSION=0.11.29
ARG RESEARCH_UID=1000
ARG RESEARCH_GID=1000
ARG CLAIM_PLANE_GIT_COMMIT=unknown

ENV PYTHONHASHSEED=0 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=UTC \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    CLAIM_PLANE_RESEARCH_ENVIRONMENT=cooperbench-linux-v1 \
    CLAIM_PLANE_RESEARCH_BASE_IMAGE=python:3.12.11-bookworm@sha256:9bed8554e926c07c6f908841d5ee88c33e8df9236b191526bbce81a9062ab43a \
    CLAIM_PLANE_RESEARCH_UV_VERSION=${UV_VERSION} \
    CLAIM_PLANE_RESEARCH_GIT_COMMIT=${CLAIM_PLANE_GIT_COMMIT}

RUN python -m pip install --no-cache-dir \
      "pip==25.1.1" \
      "setuptools==80.9.0" \
      "uv==${UV_VERSION}" \
    && git config --system user.name "claim-plane-benchmark" \
    && git config --system user.email "agent@cooperbench.local"

RUN set -eux; \
    if ! getent group "${RESEARCH_GID}" >/dev/null 2>&1; then \
      groupadd --gid "${RESEARCH_GID}" research; \
    fi; \
    useradd --uid "${RESEARCH_UID}" --gid "${RESEARCH_GID}" --create-home --shell /bin/bash research; \
    mkdir -p /workspace/claim-plane /state; \
    chown -R "${RESEARCH_UID}:${RESEARCH_GID}" /workspace/claim-plane /state

WORKDIR /workspace/claim-plane

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY experiments ./experiments

RUN python -m pip install --no-cache-dir --no-build-isolation .

USER ${RESEARCH_UID}:${RESEARCH_GID}

ENTRYPOINT ["python", "-m", "experiments.cooperbench"]
CMD ["environment"]
