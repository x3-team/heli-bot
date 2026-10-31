# Reverse proxy (existing server)

Heli bot does **not** open new host ports. The `api` service joins your existing reverse-proxy Docker network under the hostname **`heli-bot-api`** (port `8000` inside the network).

Add a **new subdomain** in the proxy you already run (Caddy, nginx, Traefik, etc.). Example shapes only — use the subdomain and TLS settings Vitalii chooses:

## Caddy (snippet)

```caddyfile
# api.example.com {
#   reverse_proxy heli-bot-api:8000
# }
```

## nginx (snippet)

```nginx
# server {
#   server_name api.example.com;
#   location / {
#     proxy_pass http://heli-bot-api:8000;
#     proxy_set_header Host $host;
#     proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
#     proxy_set_header X-Forwarded-Proto $scheme;
#   }
# }
```

Health check path: `GET /health`.
