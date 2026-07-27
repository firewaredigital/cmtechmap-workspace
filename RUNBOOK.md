# CM TECHMAP — Runbook de Operação

Guia operacional da produção. Para arquitetura e funcionalidades, veja
`cm-techmap-docs/`.

---

## 1. Topologia de produção

```
navegador ──HTTPS──> Vercel (cm-techmap-frontend.vercel.app)
                       │  rewrites server-side: /api/*, /realms/*, /resources/*
                       ▼
                     VM OCI 143.47.116.9 :80  (nginx)
                       ├── backend (FastAPI, 1 worker)
                       ├── celery-worker + celery-beat
                       ├── postgres 16 + PostGIS   (/data/postgres)
                       ├── redis  (broker Celery)
                       ├── minio  (/data/minio)
                       ├── keycloak 26
                       └── titiler + martin (tiles)
```

- **VM**: Oracle E2.1.Micro, 1 GB RAM + 2 GB swap, Always Free, IP fixo.
- **Diretório**: `/opt/cm-techmap` — **espelho por rsync, não é clone git**.
  `git pull` lá falha com *"not a git repository"*.
- **Segredos da VM**: `/opt/cm-techmap/.env.oci`. **Todo** comando
  `docker compose` precisa de `--env-file .env.oci`, senão a interpolação
  aborta com `MINIO_ROOT_PASSWORD must be set`.
- **Portas públicas**: apenas 22 (SSH) e 80 (HTTP). Postgres, Redis, MinIO,
  Keycloak e os servidores de tiles escutam só em `127.0.0.1`.

---

## 2. Deploy

### Automático (recomendado)
Push na `main` dispara o workflow `.github/workflows/ci-cd.yml`, que roda
lint + testes e então executa os mesmos scripts abaixo. Requer os secrets:
`PROD_SSH_KEY`, `PROD_HOST`, `PROD_USER`, `VERCEL_TOKEN`.

### Manual
```bash
cd applications
./scripts/deploy-vm.sh        # backend  (~5 min; --skip-build p/ só reiniciar)
./scripts/deploy-vercel.sh    # frontend (~30 s)
```

> **Por que o build usa `nohup` + polling**: compilar a imagem consome toda a
> RAM e derruba a sessão SSH. Um build em foreground morre junto com a conexão
> e a **imagem antiga continua no ar sem erro nenhum**.

> **Por que a Vercel precisa de script**: o webhook do GitHub cria o deploy em
> estado `BLOCKED` (o autor dos commits não é a identidade conectada à conta).
> O script cria o deploy via API com o token do dono, que não passa por essa
> checagem. Correção definitiva: conectar a conta GitHub `GustavoSaraivap` em
> *Vercel → Account Settings → Authentication*.

---

## 3. Diagnóstico rápido

```bash
# Saúde externa
curl -s https://cm-techmap-frontend.vercel.app/api/v1/health   # esperado: 200
curl -s http://143.47.116.9/api/v1/health                      # backend direto

# Estado na VM
ssh -i ~/.ssh/oci_cm_techmap_rsa ubuntu@143.47.116.9
docker ps --format '{{.Names}} {{.Status}}'    # 10 containers, todos healthy
free -m                                        # ~450/956 MB é normal
df -h /                                        # disco
docker logs --tail 100 cmo-backend

# Dependências profundas (DB, Redis, MinIO, Keycloak)
curl -s http://127.0.0.1/api/v1/health/ready | python3 -m json.tool
```

### Sintomas conhecidos

| Sintoma | Causa provável | Ação |
|---|---|---|
| Site carrega, API dá 502 | Vercel apontando p/ host morto | Conferir `rewrites` no `vercel.json`; devem apontar p/ `143.47.116.9` |
| Login 503 logo após deploy | Keycloak recriado | **Esperar até 20 min.** Ver "Custo de recriar o Keycloak" abaixo |
| Login lento na 1ª chamada | JIT frio (~14 s) | Normal; `KEYCLOAK_TIMEOUT=45` cobre. Chamadas seguintes ficam <1 s |
| 401 em toda API autenticada | `iss` do token fora do conjunto aceito | Conferir `KEYCLOAK_EXTERNAL_URL`/`KEYCLOAK_EXTRA_ISSUERS` no `.env.oci` |
| Upload 504 | Timeout do gateway | Rotas `/api/v1/(assets\|uploads)/` têm 1800 s; conferir nginx |
| HTTP 429 | Rate limit (login 5r/s, API 30r/s) | Esperado sob rajada; ajustar zonas em `nginx.oci-micro.conf` |
| Container reiniciando em loop | OOM ou healthcheck falhando | `docker stats`, `docker logs`; ver limites em `deploy.resources` |

### Custo de recriar o Keycloak

Medido em 27/07/2026: recriar `cmo-keycloak` derruba a autenticação por
**~20 minutos** — o container detecta mudança de configuração e refaz a
augmentation do Quarkus (**409 s** medidos), depois ainda precisa aquecer o
JIT (1ª chamada ~14 s, seguintes <1 s). Enquanto isso todo login responde
`503 Serviço de autenticação indisponível`.

Por isso `deploy-vm.sh` sobe **apenas** `backend`, `celery-worker`,
`celery-beat` e `nginx`. Nunca rode `docker compose up -d` sem lista de
serviços em produção — isso recicla Keycloak, Postgres, Redis e MinIO junto.

Se precisar mesmo recriar o Keycloak, avise os usuários e acompanhe:
```bash
docker logs -f cmo-keycloak     # espere "Listening on http://0.0.0.0:8080"
```

---

## 4. Backup e restauração

O backup roda pelo **celery-beat** (`app/celery_app.py: beat_schedule`),
faz `pg_dump` e envia para o bucket `cm-techmap-backups` no MinIO.

```bash
# Disparar backup manual
docker compose --env-file .env.oci -f docker-compose.oci-micro.yml \
  exec -T celery-worker celery -A app.celery_app call app.tasks.maintenance.backup_database

# Conferir se o beat está agendando
docker logs --tail 50 cmo-celery-beat

# Listar backups
docker exec cmo-minio mc ls local/cm-techmap-backups 2>/dev/null || \
  echo "usar console MinIO via túnel: ssh -L 19001:127.0.0.1:19001 ubuntu@143.47.116.9"

# Restaurar
./scripts/restore-backup.sh <arquivo>
```

> ⚠️ `/data/postgres` é bind mount sem snapshot. **Sem os backups do beat não
> há recuperação** de falha de disco da VM.

---

## 5. Acesso a consoles internos

`/docs`, `/redoc`, `/openapi.json` e `/minio/` retornam **404 em produção**
de propósito (não expor superfície de API nem console de storage). Para usar:

```bash
# Swagger
ssh -L 8000:127.0.0.1:8000 -i ~/.ssh/oci_cm_techmap_rsa ubuntu@143.47.116.9
# → http://localhost:8000/docs  (só funciona se APP_ENV != production)

# Console MinIO
ssh -L 19001:127.0.0.1:19001 -i ~/.ssh/oci_cm_techmap_rsa ubuntu@143.47.116.9
# → http://localhost:19001

# Admin Keycloak
ssh -L 18080:127.0.0.1:18080 -i ~/.ssh/oci_cm_techmap_rsa ubuntu@143.47.116.9
# → http://localhost:18080
```

---

## 6. Limites atuais e caminho de escalada

A VM Always Free entrega **1 GB de RAM**. Isso impõe tetos reais:

| Recurso | Hoje | Teto prático |
|---|---|---|
| Workers da API | 1 | 1 (cada worker ~200 MB) |
| Concorrência Celery | 1 | 1 |
| `max_connections` do Postgres | 50 | pools da app fixados em 3+2 |
| Fotogrametria (NodeODM) | **ausente** | precisa de 4+ GB |
| Usuários simultâneos | ~10–20 leitura | limitado por 1 worker |

**Processamento de drone não roda nesta VM.** Com `ALLOW_SIMULATION=false`
(padrão) o pipeline falha explicitamente em vez de fabricar resultados —
antes ele exibia barra de progresso e marcava o voo como concluído sem
gerar nada.

### Para escalar

1. **VM maior** (ex.: OCI A1.Flex 4 vCPU / 24 GB, também Always Free em
   muitas regiões): copiar `/data`, subir `docker-compose.prod.yml` (que já
   inclui NodeODM, PgBouncer, Prometheus, Grafana, Flower) e apontar os
   rewrites da Vercel para o novo IP. Aumentar `--workers` e
   `--concurrency` proporcionalmente à RAM.
2. **Fotogrametria isolada**: subir NodeODM numa instância dedicada e apontar
   `NODEODM_HOST`. É o serviço que mais consome recursos e escala à parte.
3. **TLS**: hoje o tráfego Vercel→VM é HTTP (a Vercel faz proxy server-side,
   então o navegador só fala HTTPS). Para expor a VM diretamente, emitir
   certificado (o `Dockerfile.oci-micro` já traz o certbot) e habilitar o
   bloco `listen 443 ssl` — a referência está em `nginx.prod.conf`.
4. **Kubernetes**: manifests em `k8s/` para quando houver mais de um nó.

---

## 7. Segurança — pendências conhecidas

- **Credenciais demo ativas**: os usuários do realm `cm-techmap` ainda usam
  as senhas do seed (`superadmin@cmtechmap.com.br` / `SuperAdmin@2026`, entre
  outros), que estão **públicas no histórico do git**. Rotacionar pelo admin
  do Keycloak (via túnel SSH, seção 5) antes de divulgar o sistema.
- **Histórico do git**: `.env` foi removido do rastreamento, mas os segredos
  seguem em commits antigos. Limpar exige reescrever o histórico
  (`git filter-repo`) e afeta quem já clonou — decisão do dono do repositório.
- **Boot bloqueado por segredo placeholder**: se `APP_SECRET_KEY` ou
  `KEYCLOAK_CLIENT_SECRET` continuarem com valor `CHANGE-ME` em produção, o
  backend **aborta o boot** de propósito. Gerar com `openssl rand -hex 32`.
