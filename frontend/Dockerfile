ARG BUILD_REVISION=unknown
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci --no-audit --no-fund
ARG VITE_API_BASE_URL=/api/v1
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
COPY tsconfig.json vite.config.ts index.html evidence-report.html ./
COPY src ./src
RUN npm run build

FROM nginx:1.27-alpine
ARG BUILD_REVISION
LABEL org.opencontainers.image.revision=$BUILD_REVISION
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
