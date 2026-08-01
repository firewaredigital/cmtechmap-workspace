# CM TECHMAP — Instalação Local

Roda a plataforma **inteira** na sua máquina — backend, frontend, banco,
autenticação, servidores de mapa e o motor de fotogrametria — acessível por um
endereço próprio no navegador, como `http://cmtechmap.local`.

Para quem usa, é indistinguível de um sistema hospedado na internet. Nada de
`localhost`, nada de porta na URL, nenhuma etapa manual depois da instalação.

---

## 1. Antes de começar

| Requisito | Mínimo | Recomendado |
|---|---|---|
| Sistema | Windows 10/11 ou Ubuntu 20.04+ | — |
| RAM | 8 GB | 16 GB (fotogrametria) |
| Disco livre | 20 GB | 50 GB |
| Docker | Docker Desktop (Win) / Docker Engine + Compose v2 (Linux) | — |

**Windows:** instale o [Docker Desktop](https://www.docker.com/products/docker-desktop/)
e aguarde ficar com o ícone verde antes de prosseguir.

**Ubuntu:**
```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER   # reabra a sessão depois
```

---

## 2. Instalar

### Ubuntu / Linux
```bash
cd applications/deploy/local
sudo ./install.sh
```

### Windows
Abra o PowerShell **como Administrador** (botão direito → *Executar como
administrador*) e rode:
```powershell
cd caminho\para\applications\deploy\local
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

O instalador é necessário como administrador porque registra o domínio no
arquivo de hosts do sistema e usa a porta 80.

**Ao terminar, acesse `http://cmtechmap.local`.** No Windows o navegador abre
sozinho.

### Opções

```bash
sudo ./install.sh --domain minhaprefeitura.local   # outro nome
sudo ./install.sh --port 8090                      # outra porta
sudo ./install.sh --yes                            # sem perguntas
```

---

## 3. O que o instalador faz

1. **Confere pré-requisitos** — Docker rodando, RAM e disco suficientes
2. **Gera segredos aleatórios** — senhas de banco, MinIO e chave da aplicação,
   diferentes em cada instalação (só na primeira vez; reinstalar preserva)
3. **Resolve conflito de porta** — se algo já ocupa a 80 (Apache, IIS, Skype),
   oferece liberar o serviço ou usar outra porta
4. **Registra o domínio** — cria a entrada no arquivo de hosts e **confere se
   resolve mesmo**, em vez de supor
5. **Compila e sobe 13 serviços**
6. **Aplica as migrações do banco**
7. **Valida ponta a ponta** — interface, API, autenticação e rota de callback.
   Só declara sucesso se tudo responder

---

## 4. Atualizar uma instalação existente

Rode o novo instalador **por cima** — em qualquer formato (`.exe`, zip ou
tar.gz). O passo *"Verificando instalação anterior"* cuida de tudo:

1. **Detecta** containers/volumes `cml-*` de qualquer instalação anterior;
2. **Migra os segredos**: se o `.env.local` antigo estiver em outra pasta
   (instalador novo em local diferente), ele é localizado pelo registro do
   Windows ou pela etiqueta do compose nos containers antigos e copiado —
   as senhas dos volumes continuam funcionando;
3. **Para a versão em execução** (`docker stop`, 30 s de tolerância) antes
   de compilar e subir a nova — portas são libertadas, nada conflita;
4. **Preserva os dados**: os volumes nunca são apagados na atualização.

Processamentos em andamento (fotogrametria, análises) são interrompidos —
reprocesse o voo depois da atualização, se for o caso. Se algo impedir a
atualização, o instalador gráfico mostra **o motivo exato** (lido de
`.install-error`), não uma lista genérica de causas.

## 5. Uso diário

### Ubuntu
```bash
./cmtechmap start      # sobe
./cmtechmap stop       # para (dados preservados)
./cmtechmap status     # estado + consumo de recursos
./cmtechmap logs       # acompanha os logs (ou: logs backend)
./cmtechmap backup     # dump do banco + arquivos
./cmtechmap update     # recompila com o código novo e migra
./cmtechmap uninstall  # remove (pergunta antes de apagar dados)
```

### Windows
```powershell
.\cmtechmap.ps1 start      # e os mesmos comandos acima
```

---

## 6. Arquitetura

Um único ponto exposto — o gateway — faz tudo responder sob o mesmo domínio:

```
navegador → http://cmtechmap.local
              │
              ▼
         gateway (nginx)
              ├── /            → frontend   (SPA React compilada)
              ├── /api/        → backend    (FastAPI)
              ├── /realms/     → keycloak   (login OIDC)
              ├── /ws/         → backend    (progresso em tempo real)
              └── /docs        → backend    (documentação da API)

serviços internos (sem porta no host):
  postgres+postgis · redis · minio · keycloak
  celery worker+beat · titiler · martin · nodeodm
```

Só a porta do gateway fica publicada, e por padrão **apenas em `127.0.0.1`**
(nenhuma outra máquina alcança). Para liberar na rede local, mude
`CM_BIND_ADDRESS=0.0.0.0` no `.env.local` e rode `./cmtechmap restart`.

### Fotogrametria roda de verdade aqui

A produção na Oracle usa uma VM de 1 GB, onde o NodeODM não cabe — lá o
processamento de imagens de drone falha explicitamente. **Nesta instalação
local ele está incluído**, então o fluxo completo funciona: upload das fotos →
ortomosaico → modelo de elevação → detecção de edificações.

---

## 7. Isolamento — não quebra nada

Este ambiente é **completamente separado** dos demais:

| | Local | Desenvolvimento | Produção (Oracle) |
|---|---|---|---|
| Arquivo | `deploy/local/docker-compose.local.yml` | `docker-compose.yml` | `docker-compose.oci-micro.yml` |
| Projeto | `cm-techmap-local` | `cm-techmap` | `cm-techmap-oci` |
| Containers | `cml-*` | `cm-*` | `cmo-*` |
| Volumes | `cml-*` | `cm-*` | `/data` na VM |
| Portas | só a do gateway | 1xxxx | 80 na VM |

Subir, parar ou apagar o ambiente local **não afeta** a produção na Oracle nem
o stack de desenvolvimento. Os três podem coexistir na mesma máquina.

---

## 8. Problemas comuns

**"A porta 80 já está em uso"**
Algo já serve HTTP na máquina. O instalador oferece parar o serviço ou usar
outra porta. Para descobrir o culpado:
`sudo ss -tlnp | grep :80` (Linux) ou `netstat -ano | findstr :80` (Windows).

**"O nome cmtechmap.local resolve para outro endereço"**
Domínios `.local` podem ser capturados pelo mDNS (avahi/Bonjour). Reinstale com
outro sufixo: `sudo ./install.sh --domain cmtechmap.interno`.

**Login retorna 503 logo após instalar**
O Keycloak leva alguns minutos para subir na primeira vez (importa o realm e
aquece a JVM). Aguarde e tente de novo; `./cmtechmap status` mostra quando
ficar saudável.

**Fotogrametria falha por memória**
Reduza `NODEODM_MEMORY_LIMIT` no `.env.local` (ex.: `4G`) e rode
`./cmtechmap restart`. No Windows, aumente a memória do WSL2 criando
`%UserProfile%\.wslconfig` com `[wsl2]` e `memory=12GB`.

**Não use domínios `.dev` ou `.app`**
São TLDs reais com HTTPS obrigatório (HSTS pré-carregado): o navegador recusa
HTTP e a instalação fica inacessível. O instalador bloqueia esses sufixos.

---

## 9. Segurança

Os segredos são sorteados na instalação, mas **os usuários de demonstração do
realm continuam com senhas públicas** (`superadmin@cmtechmap.com.br` /
`SuperAdmin@2026`). Numa instalação que vá receber dados reais, troque-as no
console administrativo em `http://cmtechmap.local/admin`.

O tráfego entre o navegador e o gateway é HTTP. Como tudo trafega dentro da
própria máquina (`127.0.0.1`), não passa pela rede. Se você liberar o acesso na
rede local (`CM_BIND_ADDRESS=0.0.0.0`), considere pôr um proxy com TLS na
frente.
