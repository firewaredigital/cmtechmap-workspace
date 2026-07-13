# Projeto CM TECHMAP

### Abaixo de forma resumida os módulos que o sistema precisa ter:

**Módulo: Autenticação e Cadastro:**

• Cadastro e login de usuários (clientes e construtores).

• Recuperação de senha via e-mail.

• Autenticação através de Google e Facebook.

**Módulo: Perfil básico cliente ou colaborador:**

• Seção de perfil com foto, informações pessoais e histórico de atividades. • Página com opção para editar detalhes pessoais e de contato.

• Notificações inteligentes.

• Exportação em tempo real.

**Módulo: Administrativo Geral.**

**1\. Gestão de Usuários**

**Funcionalidades:**

• Gerenciamento de Perfis:

• Visualizar, editar e excluir perfis de usuários.

• Definir permissões específicas para cada nível de usuário (Administrador ou usuário comum).

• Ativação e Desativação de Contas:

• Ativar ou desativar usuários da plataforma.

• Histórico de atividades dos usuários (log de ações).

• Controle de Acesso:

• Limitação de acesso a determinadas funcionalidades e módulos de acordo com o perfil de usuários.

**2\. Gestão de Assinaturas**

**Funcionalidades:**

• Planos de Assinatura:

• Criação, edição e exclusão de diferentes planos de assinatura (Gratuito, Premium, Empresarial).

• Gestão de preços e recursos incluídos em cada plano.

• Controle de Assinatura:

• Visualizar o status de assinaturas (ativa, pendente, expirada).

• Notificações renovação ou cancelamento de assinaturas.

• Histórico de assinaturas e pagamentos de cada usuário.

• Relatórios de Assinaturas:

• Geração de relatórios de assinaturas ativas, inativas e canceladas.

• Visualização de gráficos sobre crescimento ou queda nas assinaturas por período

**Módulo: Mapas Interativos com Mapbox API**

**Funcionalidades:**

**1\. Captura de Imagens por Drones:**

• Upload e processamento dos vídeos e imagens capturadas por drones diretamente na plataforma.

• Visualização das áreas mapeadas com alta precisão.

**2\. Geração de Relatórios:**

• Cálculo manual (ou automatizado conforme análise posterior) de metragem de áreas construídas e terrenos.

• Relatórios detalhados sobre áreas edificadas, não edificadas e áreas verdes. • Comparação entre a área do terreno e a área construída para uso na planta de valores.

**3\. Plataforma de Gestão de Mapas:**

• Interface para inserção e visualização dos dados mapeados.

• Visualização em tempo real dos dados atualizados, com funcionalidades de filtro por tipo de área (ruas, áreas verdes, edificações, etc.).

**4\. Renderização de Mapas Dinâmicos:**

• Utilização de Vector Tiles e Raster Tiles para criação de mapas responsivos com suporte a diferentes zooms e estilos sem perder qualidade.

• Suporte a renderização client-side com navegação fluida e sem pixelização.

**5\. Customização Visual de Mapas:**

• Utilização da Styles API para customizar completamente o visual dos mapas em tempo real.

• Alteração de cores, ícones, labels e fontes, permitindo a adequação da identidade visual da aplicação ou do projeto.

• Adição de camadas e dados personalizados diretamente no mapa, como áreas específicas ou pontos de interesse.

**Módulo: Relatórios de Medidas de Construção**

**Funcionalidades:**

• Pasta de Projetos

• Coleta de Dados de Medição:

• Interface em tabela para inserir dados de medição (área, volume, alturas, distâncias).

• Importação de arquivos de medições via Excel ou PDF.

• Histórico de medições com data e responsável pelo registro.

• Geração de Relatórios:

• Geração de relatórios detalhados de medidas realizadas em cada projeto. • Opção para personalização dos relatórios (título, logo da empresa). • Visualização e Exportação:

• Exportação de relatórios em formato PDF, Excel ou CSV.

• Visualização de relatórios dentro da plataforma com gráficos e tabelas. • Notificações e Alertas:

• Alertas automáticos para prazos de verificação de medidas.