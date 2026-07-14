# Guia Completo: Criar Novo Usuario no Keycloak (CM TechMap)

Este guia descreve, passo a passo e de ponta a ponta, como criar um novo usuario no Keycloak para uso na aplicacao CM TechMap.

## 1. Objetivo

Criar um novo usuario com acesso ao realm `cm-techmap`, definir senha, atribuir papeis (roles) e validar login com sucesso.

## 2. Pre-requisitos

1. Backend/all-in-one em execucao.
2. Keycloak acessivel.
3. Credenciais de administrador do Keycloak.
4. Realm `cm-techmap` existente.

## 3. Confirmar dados basicos no ambiente

No host onde o stack esta rodando, confirme o usuario admin configurado:

```bash
cd applications/deploy/all-in-one
grep -E '^KEYCLOAK_ADMIN_USERNAME=' .env.single
```

Resultado esperado (no seu ambiente atual):

- `KEYCLOAK_ADMIN_USERNAME=admin`

Para identificar a porta local atual do Keycloak:

```bash
cd applications/deploy/all-in-one
grep -E '^HOST_KEYCLOAK_PORT=' .env.single
```

## 4. Descobrir URL do Keycloak

Use um dos modos abaixo.

### 4.1. Acesso local (mais comum para administracao)

URL base:

- `http://127.0.0.1:<HOST_KEYCLOAK_PORT>`

Admin Console:

- `http://127.0.0.1:<HOST_KEYCLOAK_PORT>/admin`

### 4.2. Acesso publico via tunel

Se estiver usando tunel/public URL:

```bash
cd applications/deploy/all-in-one
grep -E '^KEYCLOAK_EXTERNAL_URL=' .env.single
```

Exemplo:

- `KEYCLOAK_EXTERNAL_URL=https://seu-endereco-publico/auth`

Admin Console publico:

- `https://seu-endereco-publico/auth/admin`

## 5. Criar usuario pelo painel Keycloak (metodo recomendado)

## 5.1. Login no Admin Console

1. Abra a URL `/admin`.
2. Entre com usuario administrador (ex.: `admin`).
3. Digite a senha de administrador configurada no ambiente.

## 5.2. Selecionar o realm correto

1. No canto superior esquerdo, selecione o realm `cm-techmap`.
2. Garanta que voce NAO esta no realm `master` ao criar usuario da aplicacao.

## 5.3. Criar usuario

1. Menu lateral: `Users`.
2. Clique em `Create new user`.
3. Preencha os campos:
   - `Username` (obrigatorio)
   - `Email` (recomendado)
   - `First name` (opcional)
   - `Last name` (opcional)
   - `Email verified` (opcional; geralmente `ON` se email ja validado)
   - `Enabled` = `ON`
4. Clique em `Create`.

## 5.4. Definir senha

1. Com o usuario aberto, va para aba `Credentials`.
2. Clique em `Set password`.
3. Defina senha forte.
4. Em `Temporary`:
   - `OFF` para senha definitiva.
   - `ON` para forcar troca de senha no primeiro login.
5. Confirme.

## 5.5. Atribuir roles (muito importante)

1. Va para aba `Role mapping`.
2. Clique em `Assign role`.
3. Adicione os papeis necessarios para esse usuario.

Observacao:

- Sem role correta, o login pode funcionar, mas a aplicacao pode negar acesso/autorizacao.

## 6. Validar usuario criado

## 6.1. Validacao no proprio Keycloak

1. Volte em `Users`.
2. Pesquise pelo username.
3. Verifique:
   - usuario `Enabled`
   - email/atributos corretos
   - roles atribuidas

## 6.2. Validacao na aplicacao

1. Faça logout da conta atual.
2. Acesse a tela de login da aplicacao.
3. Entre com novo usuario e senha.
4. Verifique se as telas protegidas carregam sem erro de permissao.

## 7. Checklist rapido de criacao

1. Realm correto: `cm-techmap`.
2. Usuario criado e `Enabled`.
3. Senha definida em `Credentials`.
4. `Temporary` configurado conforme politica.
5. Roles atribuidas em `Role mapping`.
6. Login testado com sucesso.

## 8. Problemas comuns e como resolver

## 8.1. Usuario nao consegue logar

Possiveis causas:

1. Senha nao definida.
2. Usuario `Disabled`.
3. Realm incorreto.
4. Senha marcada como temporaria e fluxo nao concluido.

Acao:

1. Revisar `Users` -> usuario -> `Details` e `Credentials`.

## 8.2. Login funciona, mas aplicacao nega acesso

Possivel causa:

1. Roles nao atribuidas corretamente.

Acao:

1. Revisar `Role mapping` e atribuir roles exigidas pela aplicacao.

## 8.3. Nao abre Admin Console

Possiveis causas:

1. Porta dinamica diferente da esperada.
2. Stack nao subiu totalmente.
3. Tunel nao saudavel.

Acao:

1. Conferir `HOST_KEYCLOAK_PORT` em `.env.single`.
2. Conferir containers em execucao.
3. Rodar smoke check.

## 8.4. Erro de credencial de admin

Possiveis causas:

1. Senha alterada e nao atualizada localmente.
2. Ambiente antigo em cache.

Acao:

1. Verificar variaveis admin no `.env.single`.
2. Reiniciar stack se necessario.

## 9. Boas praticas de seguranca

1. Nao reutilizar senha de admin em usuarios comuns.
2. Usar senha forte para cada conta.
3. Evitar usar `master` para contas da aplicacao.
4. Criar usuario administrativo dedicado (nao usar apenas `admin` padrao).
5. Revisar roles minimas necessarias (principio do menor privilegio).
6. Registrar quem criou cada usuario (auditoria interna).

## 10. Modelo de padrao recomendado para novos usuarios

1. Username padronizado (ex.: nome.sobrenome).
2. Email corporativo valido.
3. Senha inicial forte + politica de troca.
4. Role por perfil:
   - usuario comum
   - gestor
   - administrador

## 11. Procedimento resumido (one-page)

1. Abrir Keycloak Admin Console.
2. Selecionar realm `cm-techmap`.
3. `Users` -> `Create new user`.
4. Preencher e salvar.
5. `Credentials` -> `Set password`.
6. `Role mapping` -> `Assign role`.
7. Testar login na aplicacao.

## 12. Referencia de arquivos de ambiente

- `applications/deploy/all-in-one/.env.single`

Campos mais relevantes:

- `KEYCLOAK_ADMIN_USERNAME`
- `KEYCLOAK_ADMIN_PASSWORD`
- `HOST_KEYCLOAK_PORT`
- `KEYCLOAK_EXTERNAL_URL`

---

Se precisar, adicione ao final deste guia um bloco interno de "Matriz de Roles por Perfil" com os papeis exatos usados pela sua equipe, para padronizar provisao de acesso.
