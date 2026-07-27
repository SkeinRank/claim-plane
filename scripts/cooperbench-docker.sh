#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
IMAGE="${CLAIM_PLANE_RESEARCH_IMAGE:-claim-plane-cooperbench:0.6.0}"
STATE_INPUT="${CLAIM_PLANE_RESEARCH_STATE:-${ROOT}/.claim-plane/docker-research}"
DOCKERFILE="${ROOT}/experiments/cooperbench/docker/Dockerfile"

usage() {
  cat <<'USAGE'
Usage:
  ./scripts/cooperbench-docker.sh build
  ./scripts/cooperbench-docker.sh environment
  ./scripts/cooperbench-docker.sh info
  ./scripts/cooperbench-docker.sh prepare /path/to/CooperBench
  ./scripts/cooperbench-docker.sh reproduce /path/to/CooperBench [extra arguments]
  ./scripts/cooperbench-docker.sh shell [/path/to/CooperBench]

The reproduce command reads OPENROUTER_API_KEY from the host environment and passes it
only to the container process. Research artifacts, repository caches, and worktrees are
persisted under .claim-plane/docker-research unless CLAIM_PLANE_RESEARCH_STATE is set.
USAGE
}

require_docker() {
  command -v docker >/dev/null 2>&1 || {
    echo "error: docker is required" >&2
    exit 1
  }
}

canonical_dir() {
  local input="$1"
  [ -d "$input" ] || {
    echo "error: directory not found: $input" >&2
    exit 1
  }
  (cd "$input" && pwd -P)
}

state_dir() {
  mkdir -p "$STATE_INPUT"
  canonical_dir "$STATE_INPUT"
}

common_mounts() {
  local cooperbench="$1"
  local state="$2"
  printf '%s\n' \
    "--mount" "type=bind,src=${cooperbench},dst=/data/cooperbench,readonly" \
    "--mount" "type=bind,src=${state},dst=/state"
}

run_study_command() {
  local cooperbench="$1"
  shift
  local state
  state="$(state_dir)"
  local -a mounts
  while IFS= read -r line; do mounts+=("$line"); done < <(common_mounts "$cooperbench" "$state")

  docker run --rm --init \
    "${mounts[@]}" \
    "$IMAGE" \
    "$@" \
    --cooperbench /data/cooperbench \
    --artifacts /state/artifacts \
    --repo-cache /state/repos \
    --workspace /state/worktrees
}

main() {
  require_docker
  local command="${1:-}"
  case "$command" in
    build)
      local git_commit
      git_commit="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || printf unknown)"
      docker build --pull \
        --build-arg "RESEARCH_UID=$(id -u)" \
        --build-arg "RESEARCH_GID=$(id -g)" \
        --build-arg "CLAIM_PLANE_GIT_COMMIT=${git_commit}" \
        -f "$DOCKERFILE" \
        -t "$IMAGE" \
        "$ROOT"
      ;;
    environment)
      docker run --rm "$IMAGE" environment
      ;;
    info)
      docker run --rm "$IMAGE" paper6 info
      ;;
    prepare)
      [ "$#" -eq 2 ] || { usage >&2; exit 2; }
      run_study_command "$(canonical_dir "$2")" paper6 prepare
      ;;
    reproduce)
      [ "$#" -ge 2 ] || { usage >&2; exit 2; }
      [ -n "${OPENROUTER_API_KEY:-}" ] || {
        echo "error: OPENROUTER_API_KEY is not set" >&2
        exit 1
      }
      local cooperbench
      cooperbench="$(canonical_dir "$2")"
      shift 2
      local state
      state="$(state_dir)"
      local -a mounts
      while IFS= read -r line; do mounts+=("$line"); done < <(common_mounts "$cooperbench" "$state")
      docker run --rm --init \
        -e OPENROUTER_API_KEY \
        "${mounts[@]}" \
        "$IMAGE" \
        reproduce-paper \
        --cooperbench /data/cooperbench \
        --artifacts /state/artifacts \
        --repo-cache /state/repos \
        --workspace /state/worktrees \
        "$@"
      ;;
    shell)
      local -a mounts=()
      if [ "$#" -ge 2 ]; then
        local cooperbench state
        cooperbench="$(canonical_dir "$2")"
        state="$(state_dir)"
        while IFS= read -r line; do mounts+=("$line"); done < <(common_mounts "$cooperbench" "$state")
      fi
      docker run --rm -it --init \
        "${mounts[@]}" \
        --entrypoint /bin/bash \
        "$IMAGE"
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
}

main "$@"
