# CM TECHMAP — Guia de Teste Ponta a Ponta

Guia completo para instalar o sistema, entrar como cada perfil de uma
prefeitura e percorrer o fluxo inteiro: do cadastro do município à imagem de
drone processada, à malha fina do IPTU e à consulta pública do cidadão.

Todos os tempos, telas e comportamentos descritos aqui foram **medidos numa
instalação real** (Ubuntu, 19 GB de RAM, 8 núcleos) em 27/07/2026. Onde
existe limitação, está dito de forma explícita — nada foi suavizado.

---

## Sumário

1. [Antes de começar](#1-antes-de-começar)
2. [Instalação](#2-instalação)
3. [Primeiro acesso](#3-primeiro-acesso)
4. [Os perfis de usuário](#4-os-perfis-de-usuário)
5. [Etapa 1 — Cadastrar o município (super admin)](#etapa-1--cadastrar-o-município-super-admin)
6. [Etapa 2 — Criar o projeto de mapeamento (gestor)](#etapa-2--criar-o-projeto-de-mapeamento-gestor)
7. [Etapa 3 — Enviar a imagem georreferenciada](#etapa-3--enviar-a-imagem-georreferenciada)
8. [Etapa 4 — Ver no mapa](#etapa-4--ver-no-mapa)
9. [Etapa 5 — Processar: relevo e edificações](#etapa-5--processar-relevo-e-edificações)
10. [Etapa 6 — Importar o cadastro imobiliário](#etapa-6--importar-o-cadastro-imobiliário)
11. [Etapa 7 — Malha fina do IPTU](#etapa-7--malha-fina-do-iptu)
12. [Etapa 8 — Revisão fiscal](#etapa-8--revisão-fiscal)
13. [Etapa 9 — Relatórios](#etapa-9--relatórios)
14. [Etapa 10 — Consulta pública do cidadão](#etapa-10--consulta-pública-do-cidadão)
15. [Testando o comportamento por perfil](#15-testando-o-comportamento-por-perfil)
16. [Operação do dia a dia](#16-operação-do-dia-a-dia)
17. [Limitações conhecidas](#17-limitações-conhecidas)
18. [Solução de problemas](#18-solução-de-problemas)

---

## 1. Antes de começar

### O que você precisa

| Item | Mínimo | Recomendado |
|---|---|---|
| Sistema | Windows 10/11 ou Ubuntu 20.04+ | — |
| Memória RAM | 8 GB | 16 GB |
| Disco livre | 20 GB | 50 GB |
| Docker Desktop (Windows) | instalado e **em execução** | — |

### Imagens para testar

Você precisa de uma **ortofoto georreferenciada** — um GeoTIFF que já carrega
as coordenadas de onde cada pixel está no mundo. Formatos aceitos no envio
direto: `.tif`, `.tiff`, `.geotiff`.

Há uma imagem de exemplo no próprio projeto:
`applications/teste-orto/orthophoto.tif` (23 MB, resolução de 2,31 cm/pixel,
região de Goiás).

> **Internet na primeira análise:** ao processar o primeiro voo, o sistema
> baixa o modelo de IA (~16 MB) uma única vez. Depois disso, tudo funciona
> sem internet.

> **Um GeoTIFF comum não serve.** O arquivo precisa ter as informações
> geográficas embutidas. Se enviar uma foto sem georreferência, o sistema
> recusa com a mensagem *"GeoTIFF sem limites geográficos"* — é proteção, não
> defeito: sem coordenadas a imagem não tem onde ser posicionada no mapa.

---

## 2. Instalação

### Windows — pelo instalador gráfico

1. Instale o **Docker Desktop** e espere o ícone ficar verde (*Running*)
2. Dê **duplo clique** em `CM-TECHMAP-Instalador.exe`
3. Aceite a solicitação de permissão do Windows (necessária para registrar o
   endereço do sistema)
4. Siga o assistente: **Avançar → escolher a pasta → Instalar**
5. Aguarde. **A primeira instalação leva de 10 a 20 minutos** — o computador
   compila os componentes. A janela mostra o progresso; é normal parecer
   parada em algumas etapas longas.
6. Ao final, marque *"Abrir o CM TECHMAP agora"* e clique em **Concluir**

### Ubuntu / Linux

```bash
cd applications/deploy/local
sudo ./install.sh
```

### O que o instalador faz sozinho

Verifica os pré-requisitos, sorteia senhas exclusivas para esta máquina,
escolhe uma porta livre, registra o endereço no sistema, compila e sobe 12
serviços, aplica as migrações do banco e valida quatro pontos críticos
(interface, API, autenticação e rota de login) antes de declarar sucesso.

### Sobre o endereço

O padrão é **`http://cmtechmap.local`**. Se a porta 80 estiver ocupada por
outro programa (Apache, IIS, Skype), o instalador **não desliga nada** — ele
escolhe outra porta e informa o endereço final, por exemplo
`http://cmtechmap.local:8090`. Use sempre o endereço que o instalador exibiu.

---

## 3. Primeiro acesso

Abra o endereço no navegador. Você verá a tela de login do CM TECHMAP.

O sistema tem **dois portais distintos**, e o destino é definido pelo seu
perfil — não há troca manual:

- **Portal Administrativo** — quem opera a plataforma e cadastra municípios
- **Portal da Prefeitura** — quem trabalha no dia a dia do município

---

## 4. Os perfis de usuário

A instalação vem com nove usuários de demonstração. Estes cinco cobrem todos
os níveis de acesso:

| E-mail | Senha | Perfil | Portal |
|---|---|---|---|
| `superadmin@cmtechmap.com.br` | `SuperAdmin@2026` | super_admin | Administrativo |
| `admin@prefeitura.gov.br` | `Admin@2026` | tenant_admin | Prefeitura |
| `ana.souza@prefeitura.gov.br` | `Ana@2026` | gestor | Prefeitura |
| `fernanda.lima@prefeitura.gov.br` | `Fernanda@2026` | operador | Prefeitura |
| `pedro.santos@prefeitura.gov.br` | `Pedro@2026` | viewer | Prefeitura |

Outros disponíveis: `carlos.mendes@prefeitura.gov.br` / `Senha123!`
(tenant_admin), `juliana.costa@prefeitura.gov.br` / `Juliana@2026` (gestor),
`roberto.alves@prefeitura.gov.br` / `Roberto@2026` e
`maria.oliveira@prefeitura.gov.br` / `Maria@2026` (operadores).

> ⚠️ **São senhas públicas de demonstração.** Antes de usar com dados reais,
> troque todas no console administrativo. Ver [seção 17](#17-limitações-conhecidas).

### O que cada perfil pode fazer

Matriz **verificada por teste** contra a instalação:

| Ação | tenant_admin | gestor | operador | viewer |
|---|:---:|:---:|:---:|:---:|
| Ver projetos | ✅ | ✅ | ✅ | ✅ |
| Criar projeto | ✅ | ✅ | ❌ | ❌ |
| Enviar imagens | ✅ | ✅ | ✅ | ❌ |
| Importar cadastro imobiliário | ✅ | ❌ | ❌ | ❌ |
| Ver painel fiscal | ✅ | ✅ | ✅ | ❌ |
| Aprovar/rejeitar discrepância | ✅ | ✅ | ❌ | ❌ |
| Gerenciar usuários | ✅ | ❌ | ❌ | ❌ |

Quando falta permissão, a resposta é clara — *"Permissões insuficientes.
Perfis exigidos: ..."* — e não uma tela quebrada.

---

## Etapa 1 — Cadastrar o município (super admin)

**Entre como** `superadmin@cmtechmap.com.br` / `SuperAdmin@2026`
→ você cai no **Portal Administrativo**.

1. No menu lateral, clique em **Onboarding**
2. Percorra as 5 etapas do assistente:

**Etapa 1 — Prefeitura.** Preencha:
- *Nome do Município*: `Prefeitura de Goiânia`
- *Slug*: preenchido sozinho a partir do nome (`prefeitura_de_goiania`).
  Só aceita letras minúsculas, números e `_` — ele vira o nome do
  compartimento de dados no banco. Acentos e espaços são convertidos
  automaticamente enquanto você digita.
- *Código IBGE*: `5208707` (7 dígitos — o de Goiânia)
- *Cidade*: `Goiânia` · *Estado*: `GO`
- *E-mail do Contato*: qualquer e-mail válido

**Etapa 2 — Regras IPTU.** Ano base (`2026`) e as zonas fiscais. Cada zona
tem alíquota (%) e valores por m² de terreno e de construção. Comece com:

| Zona | Alíquota | R$/m² Terreno | R$/m² Construído |
|---|---|---|---|
| Zona Urbana | 1,0 | 50 | 800 |
| Zona Central | 1,5 | 120 | 1500 |

**Etapa 3 — Cadastro.** Opcional aqui; a importação de verdade é na
[Etapa 6](#etapa-6--importar-o-cadastro-imobiliário).

**Etapa 4 — Projeto.** Nome do primeiro projeto (mínimo 3 caracteres).

**Etapa 5 — Revisão.** Confira e clique em **Provisionar Município**.

**O que acontece:** em poucos segundos o sistema cria um compartimento de
dados isolado para o município, com 15 tabelas próprias, índices espaciais e
regras de segurança — e já grava dentro dele as zonas de IPTU e o projeto
inicial. A tela mostra o nome do compartimento criado e o código do projeto
(ex.: `PRJ-001`).

*Medido: HTTP 201 em 2,6 segundos, com 2 zonas de IPTU e 1 projeto criados.*

---

## Etapa 2 — Criar o projeto de mapeamento (gestor)

**Entre como** `ana.souza@prefeitura.gov.br` / `Ana@2026`
→ **Portal da Prefeitura**.

1. Menu **Projetos** → botão **Novo Projeto**
2. Preencha nome, descrição, cidade e estado
3. Salvar

O sistema atribui um código sequencial (`PRJ-001`, `PRJ-002`, …) e o status
inicial `pendente`.

> Se entrar como **operador** (`fernanda.lima`), o botão de criar projeto não
> funciona — é intencional: operadores enviam imagens, gestores criam
> projetos.

---

## Etapa 3 — Enviar a imagem georreferenciada

Este é o passo central. **Entre como gestor ou operador.**

1. Abra o projeto criado
2. Clique em **Enviar imagens** (ou no ícone de upload)
3. Arraste o arquivo `.tif` ou clique para selecionar
4. Confirme o nome do projeto e envie

### O que acontece por dentro

O sistema recebe o arquivo em blocos (sem carregar tudo na memória),
converte para **COG** — um formato de imagem que permite abrir só o pedaço
visível na tela, em vez do arquivo inteiro —, extrai a georreferência e
registra o resultado.

*Medido com a ortofoto de exemplo (23 MB):*

| | Resultado |
|---|---|
| Tempo total | **20 segundos** |
| Resolução detectada | 2,31 cm/pixel |
| Coordenadas extraídas | oeste −50,3708 · sul −15,4406 · leste −50,3682 · norte −15,4383 |
| Voo | criado automaticamente |

> **Comparação:** o mesmo arquivo na infraestrutura em nuvem gratuita leva
> cerca de 212 segundos. Localmente são 20 — dez vezes mais rápido, porque a
> máquina tem muito mais memória e processador.

### Se der erro

| Mensagem | Causa | Solução |
|---|---|---|
| *"Tipo de arquivo inválido"* | Extensão diferente de .tif/.tiff/.geotiff | Converta a imagem |
| *"GeoTIFF sem limites geográficos"* | Imagem sem georreferência | Use um arquivo georreferenciado |
| *"project_id inválido"* | Projeto não selecionado | Abra o projeto antes de enviar |
| *"Arquivo excede o limite"* | Maior que o teto configurado | Ajuste `UPLOAD_MAX_FILE_SIZE_GB` |

---

## Etapa 4 — Ver no mapa

Vá para **Visão Geral** (dashboard). O mapa carrega com a ortofoto sobreposta
à base cartográfica.

O que testar:
- **Zoom e navegação** — os níveis vão de 17 a 23 (do bairro ao detalhe do
  telhado). *Medido: cada quadro do mapa carrega em ~0,23 s.*
- **Camadas** — alternar entre base padrão e satélite
- **Medições** — o painel lateral permite medir distâncias e áreas sobre a
  imagem
- **Visão 3D** — quando há modelo de elevação (próxima etapa)

---

## Etapa 5 — Processar: relevo e edificações

Com a ortofoto enviada, o sistema pode extrair o **relevo (DSM)** e os
**contornos das edificações**.

1. Abra o projeto → aba do voo
2. Clique em **Processar** (ou *Gerar DSM e Edificações*)
3. Acompanhe o progresso — a barra atualiza em tempo real

*Medido: 149 segundos para o modelo de elevação de 195 MB, e a
**rede neural de segmentação** (ONNX, executada em CPU) identificou
**22 edificações** com área e confiança individuais — na primeira execução
o sistema baixa o modelo (~16 MB) automaticamente, uma única vez.*

> **É IA de verdade:** uma U-Net treinada em imagens aéreas analisa os
> pixels — não regras de cor. Na mesma imagem, o método antigo por
> heurística marcava 55% dos pixels como "construção"; o modelo marca 3,4%,
> concentrado onde as estruturas realmente estão.

O resultado fica disponível como camadas do projeto: o modelo de elevação
permite a visualização 3D, e os contornos ficam salvos como arquivo
geográfico (`footprints.geojson`).

> **Sobre a fotogrametria completa:** este fluxo trabalha sobre uma ortofoto
> **já pronta**. Se você tiver as **fotos brutas do drone** (centenas de JPGs),
> o sistema também monta a ortofoto do zero usando o motor de fotogrametria
> (NodeODM), que está incluído nesta instalação local. Esse processamento é
> muito mais pesado — de dezenas de minutos a horas, conforme o número de
> fotos.

---

## Etapa 6 — Importar o cadastro imobiliário

Para cruzar o que a imagem mostra com o que está declarado, o sistema precisa
do cadastro da prefeitura.

**Entre como** `admin@prefeitura.gov.br` / `Admin@2026` — **só o
tenant_admin importa cadastro.**

1. Menu **Integração**
2. Selecione o arquivo CSV
3. Confira a pré-visualização e confirme

### Formato do arquivo

Colunas esperadas (nomes exatos):

```csv
inscricao,endereco,proprietario,area_terreno,area_construida,cpf_cnpj,geometry
01.001.0001,Rua das Flores 100,Maria Silva,600,80,123.456.789-00,"POLYGON((-50.3708 -15.4394, -50.3707 -15.4394, -50.3707 -15.4390, -50.3708 -15.4390, -50.3708 -15.4394))"
01.001.0002,Rua das Flores 120,Joao Souza,400,250,987.654.321-00,
01.001.0003,Av Central 55,Construtora ABC Ltda,800,150,12.345.678/0001-90,"POLYGON((-50.3700 -15.4390, -50.3699 -15.4390, -50.3699 -15.4389, -50.3700 -15.4389, -50.3700 -15.4390))"
```

- `inscricao` — inscrição cadastral (chave única do imóvel)
- `area_terreno` / `area_construida` — em m², como número
- **`geometry` — o contorno do lote em WKT** (coordenadas geográficas,
  longitude/latitude). É esta coluna que permite o cruzamento espacial com as
  construções detectadas na imagem. Pode ficar vazia — o imóvel continua
  consultável, mas **nunca casará com uma detecção**: qualquer construção
  sobre ele aparecerá como "não cadastrada".
- Geometrias imperfeitas são toleradas: o sistema conserta
  auto-interseções e, se vier um multipolígono, usa a maior parte.
- Reimportar **atualiza** os imóveis existentes em vez de duplicar

A resposta informa quantos vieram com geometria:
`{"imported": 3, "with_geometry": 2, "errors": 0}`.

> **De onde tirar o WKT?** Do sistema de geoprocessamento da prefeitura
> (exportação shapefile → WKT), ou desenhando sobre o próprio mapa do CM
> TECHMAP com a ferramenta de medição para casos pontuais.

*Medido: 3 imóveis importados, 2 com geometria, 0 erros.*

---

## Etapa 7 — Malha fina do IPTU

Aqui o sistema compara o que existe fisicamente (detectado na imagem) com o
que está declarado no cadastro.

1. Menu **Fiscal IPTU**
2. Selecione o projeto e a **tolerância de área** (padrão 15%)
3. Clique em **Executar Análise**

O sistema procura quatro situações:

| Situação | O que significa |
|---|---|
| **Não cadastrado** | Construção visível na imagem, ausente do cadastro |
| **Área subdeclarada** | Construção maior que a declarada |
| **Área superdeclarada** | Construção menor que a declarada |
| **Demolido** | Consta no cadastro, mas não existe mais |

Para cada caso, estima a diferença de arrecadação em reais, usando as
alíquotas configuradas na Etapa 1.

### Resultado medido

Executado com a ortofoto de exemplo processada pela rede neural
(22 edificações) e o CSV da Etapa 6 (2 lotes com geometria):

| Indicador | Valor |
|---|---|
| Detecções cruzadas (rede neural) | 22 |
| Lotes no perímetro | 2 |
| **Discrepâncias geradas** | **24** |
| — Não cadastradas | 22 |
| — Demolidas | 2 |
| **Gap estimado de arrecadação** | **R$ 13.306,40** |

Leitura do resultado: os lotes que declaram construção onde o modelo não
vê nenhuma caíram em *demolida*, e as 22 construções detectadas sem lote
correspondente aparecem como *não cadastradas* — **num cadastro de verdade,
com milhares de lotes cobrindo a área, esse número despenca**: "não
cadastrada" alta é sintoma de cadastro incompleto, não erro.

> **Reprocessar:** se você reenviar a imagem ou atualizar o sistema, rode o
> processamento do voo com a opção *force* — a análise substitui as
> detecções da própria versão, sem duplicar.

---

## Etapa 8 — Revisão fiscal

Com discrepâncias na base, cada uma passa por um fluxo de decisão.

1. Menu **Fiscal IPTU → Revisão**
2. A fila lista os casos por severidade e valor estimado
3. Abra um caso: mostra a imagem, o contorno detectado, os dados do cadastro
   e a diferença calculada

Ações disponíveis (exigem **gestor** ou **tenant_admin**):

- **Aprovar** — confirma a irregularidade
- **Rejeitar** — descarta (com justificativa)
- **Agendar vistoria** — encaminha para verificação em campo
- **Registrar resultado da vistoria** — fecha o ciclo com o que foi apurado

O painel acompanha o total de casos, quantos foram aprovados e rejeitados, e
o **valor total estimado de arrecadação recuperável**.

*Medido: aprovada a subdeclaração com o parecer "Confirmado por imagem" —
o sistema registrou quem revisou e quando, e o painel passou a mostrar
1 aprovada com o gap correspondente confirmado no painel.*

---

## Etapa 9 — Relatórios

Menu **Relatórios** → **Novo Relatório**.

Oito tipos disponíveis:

| Tipo | Conteúdo |
|---|---|
| `project_summary` | Resumo do projeto |
| `flight_detail` | Detalhamento de um voo |
| `comparison` | Comparação entre dois períodos |
| `project_consolidated` | Consolidado de vários voos |
| `property_appraisal` | Avaliação de imóvel |
| `fiscal_revenue` | Arrecadação fiscal |
| `technical_qa` | Qualidade técnica do levantamento |
| `custom` | Personalizado |

Formatos: **PDF** ou **Excel (.xlsx)**. A geração roda em segundo plano; o
relatório aparece na lista quando fica pronto, com link de download.

---

## Etapa 10 — Consulta pública do cidadão

O sistema expõe uma área **sem login**, para o cidadão consultar a situação
do próprio imóvel.

1. Saia do sistema (ou abra uma janela anônima)
2. Acesse `/consulta` no mesmo endereço
   (ex.: `http://cmtechmap.local:8090/consulta`)
3. Busque pela inscrição cadastral

Mostra os dados públicos do imóvel e sua localização no mapa. É a peça de
transparência: o contribuinte confere o que a prefeitura tem registrado sem
precisar de atendimento presencial.

---

## 15. Testando o comportamento por perfil

Para simular a prefeitura de verdade, percorra o mesmo caminho com perfis
diferentes e observe as diferenças:

**Como `viewer` (`pedro.santos`)** — consegue ver projetos e o mapa, mas não
envia imagens nem acessa o painel fiscal. É o perfil do vereador, do
secretário que só acompanha.

**Como `operador` (`fernanda.lima`)** — envia imagens e acompanha
processamentos, mas não cria projetos nem decide sobre discrepâncias. É o
técnico de campo.

**Como `gestor` (`ana.souza`)** — cria projetos, envia imagens e decide sobre
as discrepâncias fiscais. É o chefe do setor de cadastro.

**Como `tenant_admin` (`admin@prefeitura.gov.br`)** — tudo o que o gestor faz,
mais importar o cadastro imobiliário e gerenciar os usuários da prefeitura.

**Como `super_admin`** — não vê dados de nenhum município no dia a dia; cuida
da plataforma: cadastra prefeituras, acompanha assinaturas e monitora o
sistema.

> **Isolamento entre municípios:** cada prefeitura tem um compartimento de
> dados separado no banco. Um usuário de um município não alcança dados de
> outro, mesmo que tente pela API diretamente.

---

## 16. Operação do dia a dia

### Windows
Atalhos no Menu Iniciar: **Abrir**, **Iniciar serviços**, **Parar serviços**,
**Ver situação**, **Desinstalar**.

### Linux
```bash
cd applications/deploy/local
./cmtechmap status     # estado dos serviços e consumo de recursos
./cmtechmap stop       # para tudo (os dados ficam preservados)
./cmtechmap start      # sobe novamente
./cmtechmap logs       # acompanha o que está acontecendo
./cmtechmap backup     # cópia do banco e dos arquivos
./cmtechmap update     # atualiza para uma versão nova do código
```

### Backup

Gera uma pasta com o dump do banco e todos os arquivos (imagens,
modelos de elevação, relatórios) — guarde antes de qualquer atualização.

### Desligar e religar

Parar não apaga nada. Ao religar, o sistema volta com todos os projetos e
imagens. O serviço de autenticação leva cerca de 1 minuto para ficar pronto —
se o login falhar logo após iniciar, aguarde e tente de novo.

---

## 17. Limitações conhecidas

Transparência sobre o que **não** está pronto:

**1. Lotes sem a coluna `geometry` não cruzam com detecções.**
É uma propriedade do método, não um defeito: o cruzamento é espacial. Imóveis
importados sem geometria ficam consultáveis, mas qualquer construção sobre
eles aparece como "não cadastrada" até a geometria ser fornecida.

**2. Senhas de demonstração são públicas.**
Os nove usuários vêm com senhas conhecidas, documentadas aqui e no código.
**Antes de usar com dados reais**, troque todas no console administrativo, em
`/admin` no mesmo endereço (usuário `cmadmin`, senha gerada na instalação e
gravada em `deploy/local/.env.local`).

**3. O tráfego é HTTP, não HTTPS.**
Como tudo acontece dentro da própria máquina, os dados não passam pela rede.
Mas se você liberar o acesso para outros computadores
(`CM_BIND_ADDRESS=0.0.0.0`), coloque um proxy com TLS na frente.

**4. O DSM desta versão é sintético.**
O modelo de elevação gerado a partir de uma ortofoto pronta é estimado por
análise de imagem, não medido por fotogrametria. Para elevação real, use o
fluxo com as fotos brutas do drone.

---

## 18. Solução de problemas

**A tela não abre**
Verifique se o Docker está rodando e se os serviços estão de pé
(`./cmtechmap status` ou o atalho *Ver situação*). Confirme que está usando o
endereço exato informado pelo instalador, com a porta correta.

**Login retorna erro logo após instalar**
O serviço de autenticação demora alguns minutos na primeira vez. Aguarde e
tente novamente.

**O envio da imagem falha**
Confirme a extensão (`.tif`, `.tiff`, `.geotiff`) e que o arquivo é
georreferenciado. Arquivos muito grandes exigem mais memória — acompanhe com
`./cmtechmap status`.

**O processamento não termina**
Veja `./cmtechmap logs celery-worker`. A causa mais comum é falta de memória:
reduza `NODEODM_MEMORY_LIMIT` no `.env.local` e reinicie.

**O mapa fica em branco**
A imagem pode estar fora da área visível. Use o botão de enquadrar no mapa,
ou confirme na tela do projeto que as coordenadas foram extraídas.

**Reinstalar do zero**
Desinstale (o desinstalador pergunta se deve apagar os dados) e instale de
novo. Se restarem dados de uma instalação anterior sem o arquivo de
configuração, o instalador avisa e interrompe em vez de subir um ambiente
quebrado — nesse caso, remova os volumes conforme a instrução exibida.

---

## Resumo do que foi verificado

Executado numa instalação real em 27/07/2026:

| Etapa | Resultado |
|---|---|
| Instalação (12 serviços) | ✅ 4/4 validações |
| Login dos 5 perfis | ✅ portal correto para cada um |
| Matriz de permissões | ✅ 20 combinações testadas |
| Cadastro de município | ✅ 201 em 2,6 s, com IPTU e projeto |
| Criação de projeto | ✅ código sequencial atribuído |
| Envio de ortofoto (23 MB) | ✅ 201 em 20 s, resolução 2,31 cm |
| Mapa (zoom 17–23) | ✅ quadro em 0,23 s |
| Relevo + edificações | ✅ 149 s, DSM de 195 MB, **22 edificações via rede neural** |
| Importação cadastral | ✅ 3 imóveis, 0 erros |
| Malha fina | ✅ **24 discrepâncias, gap R$ 13.306,40** sobre detecções neurais (demolidas plantadas → encontradas) |
| Revisão fiscal | ✅ aprovação registrada com parecer, painel atualizado |
