Vou verificar a estrutura exata do projeto para te dar os comandos corretos:

Ran command: `cd /home/gustavo-saraiva/Documents/FIREWARE/LABS/cmtechmap-workspace/applications && ls -la docker-compose*.yml && echo "---" && head -5 docker-compose.yml && echo "---SERVICES---" && grep "^\s*[a-z_-]*:" docker-compose.yml | head -20 && echo "---FRONTEND---" && cat cm-techmap-frontend/package.json | grep -A2 '"scripts"' | head -5 && echo "---BACKEND PORT---" && grep -r "port\|PORT\|uvicorn" docker-compose.yml | head -5 && echo "---ENV FILES---" && ls -la .env* 2>/dev/null; ls -la cm-techmap-backend/.env* 2>/dev/null; ls -la cm-techmap-frontend/.env* 2>/dev/null`
Ran command: `cd /home/gustavo-saraiva/Documents/FIREWARE/LABS/cmtechmap-workspace/applications && grep -E "ports:" -A1 docker-compose.yml | grep -E "^\s*-" | head -15 && echo "---BACKEND CMD---" && grep -A2 "backend:" docker-compose.yml | head -5 && echo "---API PORT---" && grep "3000\|8000\|8080\|5173" docker-compose.yml | head -5 && echo "---FRONTEND CONFIG---" && cat cm-techmap-frontend/vite.config.ts 2>/dev/null | head -20`
Ran command: `cd /home/gustavo-saraiva/Documents/FIREWARE/LABS/cmtechmap-workspace/applications && grep -A5 "backend:" docker-compose.yml | head -10 && echo "---BACKEND COMMAND---" && grep "command:" docker-compose.yml | head -5 && echo "---AUTH CONFIG---" && grep -i "admin\|password\|user\|login\|secret\|DEFAULT" .env | grep -iv "postgres\|redis\|minio\|celery\|keycloak_admin" | head -10 && echo "---KEYCLOAK---" && grep -i "KEYCLOAK\|KC_" .env | head -8 && echo "---DEFAULT USER---" && grep -i "DEFAULT_ADMIN\|INITIAL_ADMIN\|SUPERUSER\|first_admin" .env cm-techmap-backend/app/core/config.py 2>/dev/null | head -5`
Ran command: `cd /home/gustavo-saraiva/Documents/FIREWARE/LABS/cmtechmap-workspace/applications && grep -i "KEYCLOAK_ADMIN\|KC_BOOTSTRAP\|ADMIN_USER\|ADMIN_PASS\|DEFAULT_ADMIN\|FIRST_SUPERUSER" .env docker-compose.yml 2>/dev/null | head -10 && echo "---LOGIN ROUTE---" && grep -rn "login\|register\|/auth" cm-techmap-frontend/src/pages/LoginPage.tsx | head -10 && echo "---AUTH ENDPOINT---" && grep -rn "login\|token\|/auth" cm-techmap-backend/app/api/v1/auth.py 2>/dev/null | head -10`
Ran command: `cd /home/gustavo-saraiva/Documents/FIREWARE/LABS/cmtechmap-workspace/applications && grep -A30 "def login" cm-techmap-backend/app/api/v1/auth.py | head -35 && echo "---REGISTER---" && grep -A20 "def register" cm-techmap-backend/app/api/v1/auth.py | head -25`

Perfeito, tenho todas as informações. Aqui estão as instruções completas:

---

## 🚀 Como rodar o CM TechMap

### Passo 1 — Subir todos os containers (Backend + Infra)

```bash
cd /home/gustavo-saraiva/Documents/FIREWARE/LABS/cmtechmap-workspace/applications
docker compose up -d
```

Isso sobe **15 containers**:

| Container | Porta Externa | Função |
|---|---|---|
| `cm-postgres` | `15432` | Banco de dados PostGIS |
| `cm-redis` | `16379` | Cache e message broker |
| `cm-minio` | `19000` | Storage S3 (imagens, ortomosaicos) |
| `cm-keycloak` | `18080` | Autenticação (SSO/IAM) |
| `cm-backend` | `8000` | API FastAPI |
| `cm-celery-worker` | — | Processamento assíncrono |
| `cm-celery-beat` | — | Tarefas agendadas |
| `cm-flower` | `5555` | Monitor do Celery |
| `cm-grafana` | `13000` | Dashboard de métricas |
| `cm-titiler` | `18888` | Servidor de tiles raster |

> **Aguarde ~60 segundos** após o `docker compose up -d` para o Keycloak ficar pronto (é o container mais lento).

Verifique que todos subiram:
```bash
docker compose ps
```

### Passo 2 — Subir o Frontend (Vite dev server)

Em um **terminal separado**:

```bash
cd /home/gustavo-saraiva/Documents/FIREWARE/LABS/cmtechmap-workspace/applications/cm-techmap-frontend
npm run dev
```

O frontend roda em: **http://localhost:5173**

> O Vite já está configurado com proxy: todas as chamadas `/api/*` são redirecionadas automaticamente para o backend em `localhost:8000`.

---

## 🔐 Como fazer login

### Opção 1 — Criar uma conta pela tela de cadastro

1. Acesse **http://localhost:5173/login**
2. Clique na aba **"Cadastrar"**
3. Preencha: Nome, Email, Senha (mínimo 8 caracteres)
4. Clique em **"Criar Conta"**
5. Após criação, volte à aba **"Entrar"** e faça login com o email/senha cadastrados

> O registro cria um usuário no Keycloak com papel `viewer`. A autenticação usa o fluxo **Resource Owner Password Credentials** contra o Keycloak.

### Opção 2 — Criar usuário via Keycloak Admin Console

1. Acesse **http://localhost:18080** (Console do Keycloak)
2. Login com:
   - **Usuário:** `admin`
   - **Senha:** `admin_dev_2026`
3. Selecione o realm **`cm-techmap`**
4. Vá em **Users → Add user**
5. Crie o usuário, depois vá na aba **Credentials** e defina a senha
6. Volte ao frontend e faça login com essas credenciais

---

## Resumo rápido

```bash
# Terminal 1 — Infra + Backend
cd /home/gustavo-saraiva/Documents/FIREWARE/LABS/cmtechmap-workspace/applications
docker compose up -d

# Terminal 2 — Frontend
cd /home/gustavo-saraiva/Documents/FIREWARE/LABS/cmtechmap-workspace/applications/cm-techmap-frontend
npm run dev
```

**Acessar:** http://localhost:5173/login → Aba "Cadastrar" → Criar conta → Login.