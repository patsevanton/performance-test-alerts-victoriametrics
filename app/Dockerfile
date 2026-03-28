FROM golang:1.22-bullseye AS builder

WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -o /app/golden-signal-app ./main.go

FROM gcr.io/distroless/static-debian11
COPY --from=builder /app/golden-signal-app /app/golden-signal-app
EXPOSE 8080
ENTRYPOINT ["/app/golden-signal-app"]
