# Reverse proxy (existing server)

Heli bot listens on **`127.0.0.1:8001`** only (user systemd). Point **`https://bot-api.getheli.ru`** (or your chosen subdomain) at that upstream — do not use port **8000** (used by other services on the shared host).

Add a **new subdomain** in the proxy you already run (Caddy, nginx, Traefik, etc.). Example shapes only — use the subdomain and TLS settings Vitalii chooses:

## Caddy (snippet)

```caddyfile
# api.example.com {
#   reverse_proxy 127.0.0.1:8001
# }
```

## nginx (snippet)

```nginx
# server {
#   server_name api.example.com;
#   location / {
#     proxy_pass http://127.0.0.1:8001;
#     proxy_set_header Host $host;
#     proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
#     proxy_set_header X-Forwarded-Proto $scheme;
#   }
# }
```

Health check path: `GET /health`.
