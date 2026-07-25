# Owned, NON-ROOT rebuild of karugaru/docker_state_exporter (MIT; source vendored
# as main.go). This is the hardening half of the fviolence replacement (security
# audit 2026-07-24): the same Docker-API container-state exporter, but built by
# OUR CI (provenance we control), on a distroless static base, running as non-root
# — versus the upstream image (alpine, root, unsigned, 5-yr-old push). Publishes
# ghcr.io/<org>/docker-state-exporter; the estate re-pins the resulting digest into
# catalog_templates/docker-health-exporter.yaml + services.d.

FROM golang:1.24-alpine AS build
RUN apk add --no-cache git ca-certificates
WORKDIR /src
COPY main.go .
# Pin deps to versions whose API matches the ~2020 source: docker v24 still exposes
# types.ContainerListOptions / types.ContainerJSON (both moved into the container
# package in v25+), and go-kit v0.13 keeps kit/log. -mod=mod lets the build compute
# go.sum in-container — there is no committed go.sum for a single-file vendored program.
ENV CGO_ENABLED=0 GOOS=linux GOFLAGS=-mod=mod
# docker v20.10 is the era-appropriate match for the ~2020 source: it keeps the
# type names it uses (types.ContainerListOptions / types.ContainerJSON, moved into
# the container package in v25+) AND requires the older docker/distribution v2.7.1,
# avoiding v2.8.3's broken reference_deprecated.go (undefined reference.SplitHostname).
RUN go mod init docker-state-exporter \
 && go get github.com/docker/docker@v20.10.27+incompatible \
 && go get github.com/prometheus/client_golang@v1.19.1 \
 && go get github.com/go-kit/kit@v0.13.0 \
 && go mod tidy \
 && go build -trimpath -ldflags="-w -s" -o /out/docker-state-exporter .

FROM gcr.io/distroless/static-debian12:nonroot
LABEL org.opencontainers.image.title="docker-state-exporter" \
      org.opencontainers.image.description="Docker-API container-state exporter (vendored karugaru/docker_state_exporter, MIT), non-root" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/kevinteg/SyrvisCore" \
      org.opencontainers.image.base.name="gcr.io/distroless/static-debian12:nonroot"
COPY --from=build /out/docker-state-exporter /docker-state-exporter
# distroless :nonroot == uid 65532. The exporter only needs outbound TCP to the
# socket-proxy + a listen on :9066 (unprivileged), so non-root is free.
USER nonroot:nonroot
EXPOSE 9066
ENTRYPOINT ["/docker-state-exporter"]
CMD ["-listen-address=:9066"]
