# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

FROM node:22.20.0-alpine3.22@sha256:dbcedd8aeab47fbc0f4dd4bffa55b7c3c729a707875968d467aaaea42d6225af AS build

WORKDIR /app

COPY web/package.json web/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci

COPY web/ ./
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.30.4-alpine3.24-slim@sha256:b1d850535f815f18e9dd12ca7e9f4d36de36a49e246ccd8ef64289b0d06bde49

COPY --chown=101:101 web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build --chown=101:101 /app/dist /usr/share/nginx/html

USER 101:101

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
    CMD ["wget", "-qO", "/dev/null", "http://127.0.0.1:8080/health"]
