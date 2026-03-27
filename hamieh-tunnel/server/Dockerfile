FROM golang:1.23-alpine AS builder

RUN apk add --no-cache git

WORKDIR /src
COPY go/ ./
RUN go mod download
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o /hamieh-relay .

FROM alpine:3.19
RUN apk add --no-cache ca-certificates tzdata
COPY --from=builder /hamieh-relay /usr/local/bin/hamieh-relay

EXPOSE 8443 8444 8445 9100
VOLUME /data/certs

ENTRYPOINT ["hamieh-relay"]
CMD ["--port", "8443", "--cert", "/data/certs/relay_cert.pem", "--key", "/data/certs/relay_key.pem"]
