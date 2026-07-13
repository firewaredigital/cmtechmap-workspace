## Sim, é possível. Veja como:

### O que o Free Tier da Oracle Cloud oferece (para sempre):

```
VM ARM Ampere A1 (Always Free)
├── Até 4 OCPUs (equivalente a ~4 vCPUs)
├── Até 24GB RAM
├── 200GB Block Storage (2x 100GB)
└── 10TB/mês tráfego de saída
```

### Consumo estimado da sua stack via Docker Compose:

| Serviço | RAM estimada | CPU estimada |
|---------|-------------|-------------|
| FastAPI (backend) | ~256MB | Baixo |
| PostgreSQL | ~512MB | Baixo |
| Redis | ~128MB | Mínimo |
| MinIO | ~256MB | Baixo |
| Keycloak | ~512MB | Médio |
| Titiler | ~512MB | Médio |
| NodeODM | ~2-4GB (pico) | **Alto** (durante processamento) |
| Frontend build (Nginx) | ~64MB | Mínimo |
| Documentação (Nginx) | ~32MB | Mínimo |
| **TOTAL** | **~4-6GB** | 2-3 OCPUs |

### Cabe na VM gratuita?

| Recurso | Disponível (free) | Necessário | Sobra |
|---------|-------------------|-----------|-------|
| **RAM** | 24GB | ~6GB (pico) | ✅ **18GB livres** |
| **CPU** | 4 OCPUs | ~3 OCPUs (pico) | ✅ Folga |
| **Disco** | 200GB | ~50-80GB | ✅ Confortável |
| **Rede** | 10TB/mês | ~10-50GB/mês | ✅ Sobra muito |

### Resposta: **Sim, sobra recurso.** 

Sua stack inteira consome no máximo 6GB de RAM em pico (quando NodeODM está processando). A VM gratuita tem 24GB — ou seja, **4x mais do que precisa**.

### O que ficaria na VM:

```
Oracle Cloud VM (ARM A1 - 4 OCPU / 24GB RAM)
│
├── Docker Compose
│   ├── nginx (serve frontend build + docs)
│   ├── fastapi (API backend)
│   ├── postgres (banco de dados)
│   ├── redis (cache)
│   ├── minio (object storage)
│   ├── keycloak (autenticação)
│   ├── titiler (tile server)
│   └── nodeodm (processamento fotogramétrico)
│
├── Certbot/Let's Encrypt (SSL gratuito)
└── IP público fixo (gratuito na OCI)
```

### Único ponto de atenção:

- **Arquitetura ARM (aarch64):** Todas as imagens Docker precisam ter build ARM ou ser multi-arch. PostgreSQL, Redis, Nginx, MinIO, Keycloak e FastAPI têm imagens ARM oficiais. **NodeODM** pode precisar de build customizado para ARM — caso contrário, você pode usar uma das 2 VMs AMD gratuitas (1GB RAM) separada para builds ou usar QEMU emulation.

- **Alternativa:** Dividir em 1 VM ARM (24GB) para tudo + 1 VM AMD (1GB) como reverse proxy/nginx.

### Em resumo:

> **Sim, cabe tudo no free tier com folga.** A Oracle Cloud é a única que dá 24GB de RAM de graça permanentemente, e sua stack inteira precisa de no máximo 6GB. É viável para produção real.