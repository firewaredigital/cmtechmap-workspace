# CM TECHMAP — Deploy Oracle Cloud Always Free

Guia completo para deploy do backend CM TechMap na Oracle Cloud Infrastructure usando exclusivamente recursos **Always Free** (custo zero).

## Recursos OCI Utilizados

| Recurso | Especificação | Custo |
|---------|--------------|-------|
| VM Ampere A1 (ARM) | 4 OCPUs, 24 GB RAM | **Gratuito** |
| Boot Volume | 50 GB | **Gratuito** |
| Block Volume | 150 GB (dados) | **Gratuito** |
| VCN + Sub-rede | 10.0.0.0/16 | **Gratuito** |
| Internet Gateway | Tráfego 10 TB/mês | **Gratuito** |
| IP Público | Fixo | **Gratuito** |

## Pré-requisitos

1. **Conta Oracle Cloud** — [Criar em oracle.com/cloud/free](https://www.oracle.com/cloud/free/)
2. **OCI CLI** — [Instalar](https://docs.oracle.com/iaas/Content/API/SDKDocs/cliinstall.htm)
3. **Terraform >= 1.5** — [Instalar](https://developer.hashicorp.com/terraform/install)
4. **SSH Key** — `ssh-keygen -t ed25519 -f ~/.ssh/oci_cm_techmap -C "cm-techmap-oci"`

## Configuração Rápida

```bash
# 1. Configurar OCI CLI
oci setup config

# 2. Copiar e preencher terraform.tfvars
cp oci-deploy/terraform/terraform.tfvars.example oci-deploy/terraform/terraform.tfvars
nano oci-deploy/terraform/terraform.tfvars

# 3. Deploy completo (provisiona infra + deploy app)
chmod +x oci-deploy/scripts/*.sh
./oci-deploy/scripts/deploy-oci.sh
```

## Deploy Manual (passo a passo)

### 1. Provisionar Infraestrutura

```bash
cd oci-deploy/terraform
terraform init
terraform plan
terraform apply
# Anote o IP público da saída
```

### 2. Acessar a VM

```bash
ssh -i ~/.ssh/oci_cm_techmap ubuntu@<IP_DA_VM>
```

### 3. Setup na VM

```bash
sudo /opt/cm-techmap/oci-deploy/scripts/setup-oci-vm.sh
```

### 4. Configurar SSL (com domínio)

```bash
# Instalar certbot
sudo apt install certbot
# Obter certificado
sudo certbot certonly --webroot -w /data/certbot -d SEU_DOMINIO
# Copiar certificados
sudo cp /etc/letsencrypt/live/SEU_DOMINIO/* /opt/cm-techmap/docker/nginx/ssl/
# Restart nginx
cd /opt/cm-techmap
docker compose -f docker-compose.oci.yml restart nginx
# Auto-renewal
echo "0 12 * * * root certbot renew --quiet" | sudo tee /etc/cron.d/certbot-renew
```

## Gerenciamento

```bash
# Ver logs
docker compose -f docker-compose.oci.yml logs -f backend

# Status dos serviços
docker compose -f docker-compose.oci.yml ps

# Uso de recursos
docker stats --no-stream

# Restart de um serviço
docker compose -f docker-compose.oci.yml restart backend

# Atualizar deploy
./oci-deploy/scripts/update-oci.sh          # restart
./oci-deploy/scripts/update-oci.sh --rebuild # rebuild completo

# Backup manual
docker compose -f docker-compose.oci.yml exec backup /backup.sh

# Security hardening
sudo /opt/cm-techmap/oci-deploy/scripts/oci-security.sh
```

## Arquitetura

```
Oracle Cloud VM (ARM A1 — 4 OCPU / 24 GB RAM)
│
├── /data/ (Block Volume 150 GB)
│   ├── postgres/     — Dados do banco
│   ├── redis/        — Cache persistente
│   ├── minio/        — Object storage (ortomosaicos, etc)
│   ├── prometheus/   — Métricas
│   ├── grafana/      — Dashboards
│   └── certbot/      — Certificados SSL
│
├── Docker Compose (14 serviços)
│   ├── nginx         — Reverse proxy (HTTPS)
│   ├── backend       — FastAPI (2 workers)
│   ├── postgres      — PostgreSQL + PostGIS
│   ├── pgbouncer     — Connection pooling
│   ├── redis         — Cache + broker
│   ├── minio         — S3 storage
│   ├── keycloak      — Autenticação IAM
│   ├── celery-worker — Tasks assíncronas
│   ├── flower        — Monitor Celery
│   ├── titiler       — Raster tile server
│   ├── martin        — Vector tile server
│   ├── prometheus    — Métricas
│   ├── grafana       — Dashboards
│   └── backup        — Backup automático
│
├── SSL (Let's Encrypt)
└── Firewall (UFW: 22, 80, 443)
```

## Troubleshooting

| Problema | Solução |
|----------|---------|
| VM não cria (out of capacity) | Tente outra região ou aguarde |
| Container unhealthy | `docker logs <container>` para ver erro |
| Disco cheio | `docker system prune -a` e verificar `/data` |
| VM reciclada (idle) | O anti-idle cron deve prevenir isso |
| SSL expirado | `certbot renew` e restart nginx |
