ARG NODE_IMAGE=node:20.19.4-bookworm-slim@sha256:6db5e436948af8f0244488a1f658c2c8e55a3ae51ca2e1686ed042be8f25f70a
ARG BASE_IMAGE=python@sha256:9bed8554e926c07c6f908841d5ee88c33e8df9236b191526bbce81a9062ab43a

FROM ${NODE_IMAGE} AS node-runtime
FROM ${BASE_IMAGE}

ARG PYTHON_VERSION=3.12.13
ARG UV_VERSION=0.11.29
ARG NODE_VERSION=20.19.4
ARG SCIP_PYTHON_VERSION=0.6.6
ARG RESEARCH_UID=1000
ARG RESEARCH_GID=1000
ARG CLAIM_PLANE_GIT_COMMIT=unknown

ENV PYTHONHASHSEED=0 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=UTC \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    CLAIM_PLANE_RESEARCH_ENVIRONMENT=cooperbench-linux-v3 \
    CLAIM_PLANE_RESEARCH_BASE_IMAGE=python@sha256:9bed8554e926c07c6f908841d5ee88c33e8df9236b191526bbce81a9062ab43a \
    CLAIM_PLANE_RESEARCH_PYTHON_VERSION=${PYTHON_VERSION} \
    CLAIM_PLANE_RESEARCH_UV_VERSION=${UV_VERSION} \
    CLAIM_PLANE_RESEARCH_NODE_VERSION=${NODE_VERSION} \
    CLAIM_PLANE_RESEARCH_SCIP_PYTHON_VERSION=${SCIP_PYTHON_VERSION} \
    CLAIM_PLANE_RESEARCH_GIT_COMMIT=${CLAIM_PLANE_GIT_COMMIT}

COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules

RUN ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && test "$(node --version)" = "v${NODE_VERSION}" \
    && npm install --global --omit=dev "@sourcegraph/scip-python@${SCIP_PYTHON_VERSION}" \
    && command -v scip-python >/dev/null \
    && scip-python --version >/tmp/scip-python.version \
    && grep -F "${SCIP_PYTHON_VERSION}" /tmp/scip-python.version >/dev/null \
    && rm -f /tmp/scip-python.version

RUN test "$(python -c 'import platform; print(platform.python_version())')" = "${PYTHON_VERSION}" \
    && python -m pip install --no-cache-dir \
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
