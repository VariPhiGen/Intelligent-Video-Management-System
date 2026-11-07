# MinIO, built from a pullable upstream binary so `./vms build` / `up` needs
# nothing done by hand.
#
# WHY THIS FILE EXISTS. MinIO withdrew its public container images in 2025:
# `minio/minio` refuses pulls on Docker Hub (every tag, verified 2026-09-28),
# and quay.io / registry.min.io / bitnami have nothing either. The upstream
# Dockerfile — which begins `FROM minio/minio:latest` — cannot build.
#
# WHERE THE BINARY COMES FROM. Chainguard publishes MinIO built from the
# official source (github.com/minio/minio, unmodified, AGPL-3.0) as a public,
# anonymously pullable image. We take ONLY the binary out of it. The image
# itself is not usable as-is: it has no curl for the compose healthcheck and
# runs as uid 65532, which the existing minio_data volume is not owned by.
#
# PINNED BY DIGEST, so a rebuild is reproducible and an upstream push cannot
# change what ships. To move to a newer MinIO: pull the tag, take the new
# digest from `docker image inspect cgr.dev/chainguard/minio:latest -f
# '{{index .RepoDigests 0}}'`, and bump MINIO_SOURCE below.
#
# Built by compose (the `build:` stanza on the `minio` service), so a fresh
# host needs no ~/minio checkout and no Go toolchain.
ARG MINIO_SOURCE=cgr.dev/chainguard/minio@sha256:6a1d0b45c8669726bba580ced0bfa4cb9fdeed1ed636dfabd81d1577beb6937b
FROM ${MINIO_SOURCE} AS source

# The binary is statically linked, so any base works; debian-slim is kept for
# curl (the healthcheck calls /minio/health/live) and a shell to debug with.
FROM debian:12-slim

# ca-certificates: TLS trust store, for any outbound call MinIO makes.
# curl: the healthcheck in docker-compose.yml calls /minio/health/live, and
#       debian-slim ships neither curl nor a shell with /dev/tcp.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

COPY --from=source /usr/bin/minio /usr/local/bin/minio
RUN chmod +x /usr/local/bin/minio && /usr/local/bin/minio --version

# The object store itself. Mounted as a named Docker volume by compose; the
# directory here only guarantees the mount point exists.
RUN mkdir -p /data
VOLUME ["/data"]

# 9000 S3 API, 9001 console. Both are published on loopback only by compose.
EXPOSE 9000 9001

# Credentials are supplied at runtime (MINIO_ROOT_USER / MINIO_ROOT_PASSWORD),
# never baked in.
ENTRYPOINT ["/usr/local/bin/minio"]
CMD ["server", "/data", "--console-address", ":9001"]
