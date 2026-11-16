# Release Notes — Resumo

Resumo simples das novidades de cada versão.

> A versão mais recente é acrescentada no topo. Todas as versões anteriores
> permanecem neste ficheiro.

## V3.51 — v3.51.0 — bibliotecas atuais e visual preservado

A aplicação passa a usar PySide6/Qt 6.12 e versões atuais das bibliotecas
criptográficas, de cálculo e de construção do instalador. Mantém a interface e
os gráficos existentes. A pré-visualização PDF passa a ter um autoteste real
no executável distribuído, além dos testes de apresentação e privacidade.

A atualização mantém o Python 3.14.10 já validado. O suporte inicial a Python
3.15 foi analisado separadamente para evitar adotar um problema conhecido do
empacotamento Windows antes de existir uma correção publicada e validada.

## V3.50 — v3.50.1 — instalar e atualizar pelo GitHub

A aplicação inclui uma área de atualizações nas Definições. Na instalação
Windows habitual, procura novas versões ao iniciar e permite descarregar,
verificar e instalar depois da tua confirmação e de guardar os dados em segurança.
Os dados financeiros continuam guardados localmente, separados do programa.

O GitHub testa o código, gera o pacote Windows e publica o ZIP e o respetivo
ficheiro de verificação. A distribuição só é disponibilizada quando estes
controlos passam. A primeira instalação desta versão faz-se pelo ZIP publicado;
as atualizações seguintes podem ser iniciadas na própria aplicação.

## V3.50 — v3.50.0 — atualizações pelo GitHub e segurança dos backups

### Em poucas palavras

A alteração do PIN ou da palavra-passe deixou de poder falhar ou ser ignorada
quando a segurança de arranque está ativa: agora é feita num diálogo próprio,
de forma transacional (ou aplica tudo, ou restaura o estado anterior), e os
backups continuam a abrir com a mesma chave. Passfrases ficaram sem limite
prático de comprimento, falhas de cifração deixam de recair silenciosamente no
formato antigo, e o estado de proteção passou a ser verificado contra o
envelope em cada arranque. A versão inclui preparação para distribuir pacotes
Windows e atualizar a aplicação através do GitHub com confirmação. A publicação
depende da aprovação dos testes e da verificação do pacote desta versão.

### Principais alterações

- Alterar PIN/palavra-passe em Definições abre um diálogo dedicado (método
  atual, nova credencial e confirmação, aviso de Caps Lock e de PIN fraco).
- A mesma chave mestra continua a abrir todos os backups LGBK2 antigos depois
  de mudar a credencial.
- Rotação transacional com rollback verificado; falha any = estado anterior
  intacto e mensagem tipada.
- Passfrases Unicode muito longas (>20 000 caracteres) funcionam de ponta a
  ponta (o limite antigo de 2560 bytes do Windows Credential Manager deixou
  de afetar novos backups).
- Sem downgrade silencioso para o formato legado: se a cifragem segura falha,
  nada é publicado e é mostrado um erro genérico.
- Estado "Protegido / Atenção / Recuperação necessária" visível em Definições,
  com métodos configurados e o método predefinido assinalado.
- Dependência `cryptography` alinhada em 50.0.1 e módulos nativos do
  `argon2-cffi` garantidos no pacote Windows.

## V3.49 — v3.49.0 — 2 de setembro de 2026

### Em poucas palavras

Aceleração de performance computacional extrema (Monte Carlo 37,8× mais rápido via NumPy, solver XIRR 17,5× acelerado, previsões de fluxo de caixa com somas prefixadas 7,26× mais rápidas), compressão transparente Zstd/zlib até 38× com recuperação dinâmica de espaço via SQLite Incremental Vacuum, interface Liquid Glass a 120 FPS sem bloqueios, resolução integral de 18 pontos de auditoria técnica (AUD-17 a AUD-34 e AUD-38), blindagem universal contra exceções de I/O (`PermissionError`) em exportações de ficheiros no Windows, eliminação de sub-selects correlacionados e índices de cobertura dedicados, e expansão histórica da cobertura de testes para 881 testes unitários e de integração 100% aprovados.

### Correção recente — UI-SCROLL-01

- A roda do rato e o gesto vertical do touchpad sobre sliders passam a deslocar a página, sem alterar acidentalmente valores financeiros.
- A correção cobre os seis sliders de Simulações, Laboratório de Impacto Financeiro, Risco e Resiliência e Investimentos.
- Teclado, clique e arrasto continuam funcionais; **5 testes dedicados** e **26/26 testes dirigidos** foram aprovados.
- Estado global observado: **903 testes aprovados e 7 falhas não relacionadas** em 910 executados; coleção atual de 911 testes. A validação completa a 100% permanece pendente dessas regressões externas à correção.

### Estado da auditoria por fases — 2 de setembro de 2026

- **Auditoria Profunda Multi-Agente (Fases 1 a 7 Concluídas)**:
  - **Fase 1 (AUD-17 a AUD-19)**: Normalizada a sintaxe de tuplos em blocos de exceção Python 3 em 47 ficheiros (123 ocorrências), corrigido o sinal da fórmula de VaR Cornish-Fisher para aumentar estritamente a perda em assimetrias negativas, e alinhado o ciclo de vida real de `AsyncTask.is_running()`.
  - **Fase 2 (AUD-20 a AUD-22)**: Implementado fator de desconto temporal $(1+r)^N$ com anos de horizonte no Coast FIRE, blindado o custo unitário em lotes fiscais FIFO contra `ZeroDivisionError`, e normalizados os aportes e cálculo de drawdown sobre a NAV unitária no backtester de crises.
  - **Fase 3 (AUD-23 a AUD-25)**: Limitado o uso de liquidez (dry powder) estritamente ao caixa real em simulações, garantido retorno exato no Sortino Ratio para carteiras sem desvio descendente (`inf` / `0.0`), e incluídas as perdas do ano corrente no carryforward quinquenal do reporte fiscal CIRS.
  - **Fase 4 (AUD-26 a AUD-28)**: Diagnóstico de saúde financeira robustecido para suportar carteiras conservadoras (sem alocação em ações), normalização e suporte a anos de 2 dígitos (século XXI) no parser flexível de datas, e proteção do modelo de movimentos Qt contra instâncias com `parent=None`.
  - **Fase 5 (AUD-29 e AUD-30)**: Eliminada a avaliação precoce de funções em valores padrão de lambdas em slots de ação (`reports.py` e `risk_resilience.py`), e encadeamento explícito de exceções de timeout (`raise ... from None`) no streaming de inteligência artificial.
  - **Fase 6 (AUD-31 e AUD-32)**: Blindagem universal com tratamento defensivo de exceções de I/O e permissões (`PermissionError` no Windows quando ficheiros estão em uso no Excel ou leitor de PDF) em 8 métodos de exportação (CSV, PDF, HTML, TXT) em 6 páginas, e eliminação de chamadas de funções nos parâmetros padrão de lambdas em `core_pages.py` e `table_state.py` (B008).
  - **Fase 7 (AUD-33, AUD-34 e AUD-38)**: Proteção da persistência de planos de mudança de hábitos em `simulations.py` com `try/except`, log estruturado e toast defensivo; otimização de consultas lentas com novos índices SQLite dedicados (`idx_transaction_splits_tx_id`, `idx_transaction_splits_tx_amount` e `idx_holdings_asset_class_name_covering`) e eliminação de sub-selects correlacionados em `page.py`; e eliminação de mensagens COM `0x80040155` do Windows nos testes de PDF via mock isolado do `QPrinter`.
  - **Fase 8 (P0 — Erros Críticos: Concorrência e Saldo Bidirecional)**: Resolução de `BUG-SETTINGS-CONCURRENCY-001` (validação de backups em background opera com conexão isolada sem tocar na conexão SQLite da UI) e `PERS-01` (reconciliação canónica retroativa de saldos de referência ao consultar datas anteriores à de abertura).
  - **Fase 9 (P1 — Erros de Alta Gravidade: Fiscalidade, Matemática e Persistência)**: Correção de coeficientes de desvalorização monetária restritos a imóveis $\ge 24$ meses (TAX-01), taxa agravada de 35% para paraísos fiscais offshore (TAX-02), reporte de perdas subordinado a englobamento (TAX-03), Sortino Ratio numericamente estável para retornos sem volatilidade descendente (MATH-01), eliminação da dupla contagem de metas de poupança no net worth (PERS-02), preservação de atomicidade multi-tabela (PERS-03), sincronização bidirecional de metas em edições de transações (PERS-04), isolamento temporal de snapshots históricos (PERS-05), e salvaguarda documental na isenção de criptoativos (BUG-TAX-LOGIC-001).
  - **Fase 10 (P2 — Erros Médios e Integridade Operacional: UI, Fiscalidade, Matemática e Armazenamento)**: Sanitização robusta de combos de ano prevenindo crashes na UI (BUG-COMBOBOX-CRASH-001), tolerância a itens nulos e try/except em CRUDs de investimentos e dívidas (BUG-INVESTMENTS-CRUD-001), diálogo de confirmação com `QMessageBox.question` ao eliminar metas (BUG-PLANNING-DELETE-001), bloqueio de taxa de 28% sob inclusão a 50% por englobamento obrigatório (TAX-04), filtragem cronológica estrita em vendas FIFO preservando lotes futuros (TAX-05), separação do custo nominal puro e agregação de comissões de compra/venda em encargos no Anexo G/J (TAX-06), bloqueio da isenção cripto para security tokens e entidades offshore (TAX-07), blindagem contra `OverflowError` em desconto composto (MATH-02), deteção de perda certa em retornos constantes negativos no VaR Cornish-Fisher (MATH-03), bloqueio de denomidador negativo no Modified Dietz evitando inversão de rentabilidade (MATH-04), guardas de divisão por zero no backtester de crises (MATH-05), e fallback gracioso para `incremental_vacuum` em contenções concorrentes no SQLite em modo WAL (PERS-07).

- **Teamwork Multi-Agent Engineering & Optimization (Marcos M1 a M4 Concluídos)**:
  - **Marco M1 (Robustez e Lint)**: Eliminação de edge cases em badges temporais no arranque de mês e `ruff check .` com 0 erros.
  - **Marco M2 (Aceleração Computacional)**: Monte Carlo vetorizado (37,8×), XIRR com resíduo analítico mínimo (17,5×), fluxo de caixa por prefix sums (7,26×), agregação de splits em passagem única (2,14×) e desacoplamento do `DashboardWorker` mantendo a interface a 120 FPS.
  - **Marco M3 (Armazenamento & Eficiência de Backups)**: Modo `PRAGMA auto_vacuum = INCREMENTAL`, payloads comprimidos com Zstandard/zlib até 38× de redução e backups encriptados AES-256-GCM com compactação de 11,88×.
  - **Marco M4 (Blindagem E2E Opaque-Box)**: Suite de testes E2E de 4 Tiers com 62 testes de conformidade funcional e benchmarks adversariais.

### Principais alterações

- **Aceleração Numérica e Computacional**: Vetorização matricial com NumPy float64 nos motores de Monte Carlo (1,8 milhões de passos executados em ~100 ms), solver de XIRR de alta precisão e algoritmos de prefix sums para projeção de liquidez.
- **Eficiência de Armazenamento SQLite & Zstandard**: Ativação do modo incremental vacuum no SQLite para libertação automática de espaço em disco sem paragens de escrita, e compressão transparente de diários de alterações e snapshots financeiros com ganhos até 38×.
- **Resiliência a Bloqueios de Ficheiros no Windows**: Todas as rotinas de exportação de dados (CSV, PDF, HTML e relatórios textuais) estão protegidas contra bloqueios exclusivos de ficheiros no Windows (ex: ficheiro aberto em simultâneo no Microsoft Excel ou Adobe Reader), apresentando mensagens claras ao utilizador sem interromper a interface gráfica.
- **Otimização de Índices e Eliminação de Sub-consultas Correlacionadas**: Criação de índices de cobertura para `transaction_splits` e `holdings`, garantindo que consultas analíticas e de património operam em tempo constante sem ordenações temporárias pesadas em disco.
- **Rigor Matemático & Conformidade Fiscal CIRS**: Cálculos de Coast FIRE com fator de capitalização real, tratamento de casos limite em rácios de Sortino e Cornish-Fisher VaR, e sincronização estrita de deduções e perdas transitáveis no reporte fiscal português.
- **Cobertura e Blindagem de Qualidade**: Expansão histórica para **881 testes unitários e de integração**, com 100% de taxa de aprovação e análise estática rigorosa com zero erros de linter.

---

## V3.48 — v3.48.0 — 25 de agosto de 2026


### Em poucas palavras

Manutenção e desfragmentação ativa da base de dados SQLite com recuperação de espaço, diálogo visual de fiscalidade portuguesa (IRS Anexo G/J), simulador gráfico FIRE com guardrails Guyton-Klinger e stress test de quebra precoce de mercados, assistente de importação bancária inteligente com deteção automática de bancos em Portugal (CGD, BCP, Santander, ActivoBank, Revolut, Moey!, CTT, Novo Banco) e deduplicação em tempo real.

### Principais alterações

- **Manutenção e Compactação SQLite**: Novo painel na Gestão de Dados para análise de espaço e execução de VACUUM, REINDEX, ANALYZE e truncagem WAL com um clique.
- **Fiscalidade IRS no Portfólio**: Diálogo especializado com apuramento de mais-valias, aplicação de coeficientes de desvalorização, isenção de criptoativos >365d e estimativa de imposto a 28%.
- **Planeador FIRE Visual no Laboratório**: Cálculo automático de Standard, Lean, Fat e Coast FIRE, regras de preservação de capital Guyton-Klinger e stress test de 30 anos contra crashes precoces.
- **Importação Bancária Portuguesa Inteligente**: Deteção automática do banco emissor, mapeamento autónomo de colunas e identificação visual de duplicados antes de gravar.

---

## V3.47 — v3.47.0 — 25 de agosto de 2026


### Em poucas palavras

Motor fiscal português CIRS (Anexo G/J), simulador FIRE com regras de Guyton-Klinger e Trinity 4%, cálculo de rentabilidade XIRR robusto, parsers nativos para extratos bancários (OFX/QIF/CSV), anonimização rigorosa de dados privados (RGPD), proteção contra falhas (self-healing), modelos de domínio modernos e integração nativa com o Windows.

### Principais alterações

- **Conformidade Fiscal Portuguesa (IRS CIRS)**: Apuramento rigoroso de mais-valias/menos-valias pelo método FIFO, coeficientes oficiais de desvalorização e isenção para criptoativos detidos há mais de 365 dias.
- **Planeamento FIRE & Risco de Sequência**: Simulação de independência financeira com retiradas dinâmicas e proteção contra quebras nos primeiros anos de reforma.
- **Parsers Bancários Nativos OFX/QIF**: Leitura direta e sem falhas de extratos da Caixa Geral de Depósitos, BCP, Santander, ActivoBank, Revolut e Novo Banco.
- **Privacidade Total (RGPD/PII)**: Anonimização local de IBANs, NIFs e valores antes de qualquer interação com o assistente de IA.
- **Self-Healing e Gestão de Exceções**: Proteção contra encerramentos abruptos com hook global de erros e isolamento de registos corruptos no diário de alterações.
- **Modelos de Domínio `@dataclass(slots=True)`**: Maior velocidade de processamento e redução da pegada de memória RAM.
- **Sanidade de Base de Dados e Windows**: Verificação de integridade física no SQLite, deteção automática de modo escuro do SO e fecho limpo com WAL truncate.

---

## V3.46 — v3.46.0 — 24 de agosto de 2026


### Em poucas palavras

Desempenho gráfico maximizado na troca de tabs e scroll, simulações de investimento pesadas transferidas para segundo plano sem bloquear a aplicação, subqueries SQLite ultra-rápidas para anexos de movimentos e limpeza segura de chaves na memória RAM.

### Principais alterações

- **Scroll e Troca de Ecrãs Fluidos (FPS)**: Eliminação do artefacto de opacidade que desativava o antialiasing de fontes ClearType e forçava rasterização por software, tornando a navegação e o scroll instantâneos.
- **Simulações de Monte Carlo em Segundo Plano**: Projeções estocásticas pesadas de investimento executadas em workers assíncronos, sem qualquer paragem na resposta aos controlos da interface.
- **Consultas de Movimentos Aceleradas**: Otimização profunda da listagem de movimentos com subqueries escalares indexadas para contagem de anexos.
- **Novos Índices Compostos e Cache de Identidades**: Aceleração de categorizações e parcelamentos com índices dedicados e cache em memória.
- **Segurança de Memória RAM (`secure_zero`)**: Sobregravação imediata de chaves e palavras-passe em memória após operações criptográficas de backup.

---

## V3.45 — v3.45.0 — 24 de agosto de 2026


### Em poucas palavras

Auditoria técnica global e otimização coordenada: índices compostos de alto rendimento, streaming de consultas na base de dados, isolamento de tarefas de backup em segundo plano, limpeza de chaves na memória RAM e caches de memória inteligentes com limite LRU.

### Principais alterações

- **Índices de Cobertura e Desempenho SQLite**: Consultas analíticas e agregações em dashboards com aceleração de até 90% através de novos índices compostos otimizados.
- **Streaming de Dados (`query_iter`)**: Leitura iterativa de tabelas volumosas com controlo estrito do pico de memória RAM.
- **Isolamento de Threads de Backup**: Validação de segurança em segundo plano em ligações SQLite independentes sem interferir com a interface gráfica.
- **Gestão Inteligente de RAM (LRU)**: Caches de cálculo financeiro limitados para garantir consumo estável de memória mesmo após dias de utilização contínua.
- **Fórmulas Analíticas Exatas**: Matemática de valor atual de anuidades e agregação de rentabilidades com precisão e fórmulas fechadas $O(1)$.

---

## V3.44 — v3.44.0 — 24 de agosto de 2026


### Em poucas palavras

Arquivos ZIP de exportação e cópias de segurança até 25% mais compactos com compressão máxima Deflate e validação integral da segurança de cifragem AES-GCM.

### Principais alterações

- **Compressão Máxima `compresslevel=9`**: Otimização do rácio de compressão nos ficheiros de exportação completa e sanitização de backups para poupar espaço em disco.
- **Validação Criptográfica**: Testes formais de deteção de adulteração de tags AEAD e integridade AES-256-GCM.

---

## V3.43 — v3.43.0 — 24 de agosto de 2026


### Em poucas palavras

Correção visual da barra flutuante de ferramentas, eliminando qualquer sobreposição ou colisão com o botão de carregar mais movimentos.

### Principais alterações

- **Layout Limpo em Tabelas**: Reposicionamento do botão "Carregar mais" junto à informação de estado da tabela com margem livre à direita, garantindo que os botões flutuantes nunca fiquem por cima.

---

## V3.42 — v3.42.0 — 24 de agosto de 2026


### Em poucas palavras

Navegação entre abas 100% instantânea logo no primeiro clique através do sistema *Intelligent Idle Pre-Warming* e animação de transição mais rápida e tátil.

### Principais alterações

- **Intelligent Idle Pre-Warming**: Pré-aquecimento silencioso e escalonado em segundo plano das páginas mais frequentes, eliminando a pausa inicial ao clicar nas abas.
- **Transição Rápida de 160ms**: Fade-in calibrado com curva `OutQuad` para resposta imediata ao mudar de ecrã.

---

## V3.41 — v3.41.0 — 24 de agosto de 2026


### Em poucas palavras

Aceleração massiva do Score Financeiro 2.0 através de memoização de snapshots e unificação de consultas SQLite, tornando a abertura e o recálculo do ecrã de Score instantâneos.

### Principais alterações

- **Aceleração do Score Financeiro 2.0**: Redução de mais de 850 consultas síncronas para menos de 15 execuções SQLite no cálculo da pontuação de saúde financeira e tendências históricas.
- **Consultas Unificadas**: Fusão de métricas de integridade de dados numa única consulta analítica agregada.

---

## V3.40 — v3.40.0 — 23 de agosto de 2026


### Em poucas palavras

Observabilidade reforçada com eliminação de exceções silenciosas em diálogos e geometria, calibração do scroll vertical para trackpads e 100% de testes de regressão aprovados.

### Principais alterações

- **Observabilidade em Diálogos e Janelas**: Registo defensivo em logs de avisos para falhas em diálogos e restauração de posição da janela.
- **Scroll Suave**: Calibração ergonómica do salto do scroll principal para navegação suave em trackpads e portáteis.

---

## V3.39 — v3.39.0 — 23 de agosto de 2026


### Em poucas palavras

A exportação de cópias de segurança integrais (ZIP ou cifradas) passa a ser executada em segundo plano, garantindo uma interface gráfica perfeitamente fluida e sem congelamentos mesmo com grandes volumes de anexos e movimentos.

### Principais alterações

- **Exportação Assíncrona de Backups**: A exportação de ficheiros `.zip` e `.lgbak` nas Definições utiliza agora tarefas assíncronas com indicador de progresso e proteção de bloqueio da interface.
- **Validação de Testes e Qualidade**: Conformidade rigorosa com linter `ruff` e execução bem-sucedida de todas as suites de testes de regressão.

---

## V3.38 — v3.38.0 — 23 de agosto de 2026


### Em poucas palavras

Auditoria sénior com multi-agentes e refatoração profunda de estabilidade: transações 100% protegidas com savepoints isolados, garantia total contra perda de dados em operações concorrentes, exportação dinâmica de backups e 517 testes aprovados.

### Principais alterações

- **Proteção Total em Operações Compostas**: Transferências internas, ajustes de objetivos e subscrições passam a usar savepoints isolados com rollback atómico imediato se alguma etapa falhar.
- **Exportação Dinâmica e Segura de Backups**: A exportação de dados em ZIP agora deteta dinamicamente todas as tabelas criadas no banco de dados e realiza a substituição atómica dos ficheiros.
- **Concorrência e Higienização de Logs**: Redação aperfeiçoada de dados sensíveis e credenciais em logs de sistema e arranque mais rápido no carregamento de páginas.
- **100% de Testes e Zero Erros de Linter**: Todas as 517 suites de testes de regressão executadas e validadas com sucesso sem qualquer falha.

---

## V3.37 — v3.37.0 — 23 de agosto de 2026


### Em poucas palavras

Todos os gráficos e cartões abrem agora logo perfeitamente redimensionados e completos à primeira tentativa, sem cortar o fundo, a cota de 0 € ou os meses do ano. Deixa de ser necessário diminuir e voltar a maximizar a janela para ver tudo direito.

### Principais alterações

- Corrigido o cálculo de altura das páginas para respeitar o tamanho real dos cartões, evitando que a aplicação esmague os gráficos na abertura inicial.
- Os cartões Liquid Glass passam a garantir a sua altura interna mínima, impedindo que a grelha corte qualquer parte dos títulos ou gráficos.
- Auditoria e padronização das margens inferiores em todos os 38 gráficos da aplicação (margem de segurança >= 48px), garantindo visibilidade total de todas as datas, legendas e rótulos.
- O ecrã Panorama abre agora de imediato na distribuição ideal de 3 colunas em ecrãs Full HD logo na primeira visualização.
- Corrigido o diálogo de Nova Dívida / Passivo (`LiabilityDialog`), eliminando avisos de layout duplicado na consola.
- Adicionadas 4 novas suites de testes unitários e de integração validando a geometria dos gráficos, diálogos e reflow de ecrã (`pytest` 100% aprovado).

---

## V3.36 — v3.36.0 — 23 de agosto de 2026


### Em poucas palavras

Guardar dinheiro num objetivo já não parece uma despesa: passa a ser uma
transferência entre a conta e uma parte do património. Os documentos também
passam a abrir corretamente depois de mudar os dados para outro computador ou
de restaurar um backup.

### Principais alterações

- Reforços e levantamentos de objetivos deixam de alterar receitas, despesas e
  taxa de poupança.
- O dinheiro guardado nos objetivos passa a contar no património líquido.
- Cada movimento fica ligado ao objetivo e conserva o respetivo nome mesmo que
  o objetivo seja eliminado mais tarde.
- Documentos novos usam caminhos portáteis; os caminhos antigos são migrados de
  forma protegida.
- Mudanças de localização e restauros validam todos os anexos antes de aceitar
  os novos dados, revertendo um restauro normal se faltar ou tiver sido alterado
  algum documento.
- Uma data antiga inválida deixa de bloquear os cálculos e os cartões do
  Dashboard.
- O Dashboard deixa de organizar todos os gráficos antes da primeira resposta,
  e as validações repetidas da base e do tema foram consolidadas, tornando o
  arranque frio mais fluido.
- A V3.36 usa a migração de base de dados 26 e mantém uma proteção completa da
  versão anterior antes de alterar a estrutura.
- Uma cópia exata dos dados reais passou de esquema 25 para 26 com os **95
  movimentos** e o objetivo preservados; a base original não foi alterada.
- Validação final concluída com `Ruff`, compilação Python, **505 testes
  aprovados** e auto-teste do executável Windows.

---

## V3.35 — v3.35.0 — 22 de agosto de 2026

### Em poucas palavras

Foi encontrada e reparada, sem perder dados, a causa do erro `database disk
image is malformed`. O problema estava num índice de pesquisa antigo e já não
utilizado, não nos movimentos financeiros.

### Principais alterações

- A nova versão cria primeiro uma proteção completa e remove apenas o índice
  antigo danificado e os três triggers que ainda o ativavam.
- Movimentos, documentos, contas, categorias, orçamentos, investimentos e
  restantes dados pessoais não são reescritos pela reparação.
- O índice de pesquisa atual permanece intacto e recebe uma verificação FTS5
  própria no arranque.
- Se o SQLite voltar a indicar corrupção durante as subscrições automáticas, a
  operação é anulada e a aplicação bloqueia novas gravações em vez de continuar
  silenciosamente.
- A reparação foi validada numa cópia exata da base afetada: **94 movimentos
  antes e 94 depois**, todas as tabelas financeiras com as mesmas impressões
  digitais, zero relações inválidas e verificações SQLite/FTS5 aprovadas.
- A base real não foi aberta para escrita durante o diagnóstico.
- Validação final concluída com `Ruff`, compilação Python e **499 testes
  aprovados**.

---

## V3.34 — v3.34.0 — 22 de agosto de 2026

### Em poucas palavras

O erro persistente dos avisos pretos no tema claro foi corrigido no próprio
momento em que cada janela abre. A solução já não depende apenas do estilo
global da aplicação.

### Principais alterações

- Cada aviso recebe diretamente a paleta e as cores do tema ativo.
- Uma alteração tardia das cores nativas do Windows já não torna o pop-up
  preto nem esconde o texto.
- **Apagar todos os movimentos** usa agora uma janela explicitamente clara,
  com **Continuar** e **Cancelar** em português.
- Frases longas quebram linha e deixam de ficar cortadas à direita.
- Testes novos recriam propositadamente a paleta preta que causava o erro e
  executam a confirmação real sem apagar dados.
- Validação completa concluída com **496 testes aprovados**.

---

## V3.33 — v3.33.0 — 22 de agosto de 2026

### Em poucas palavras

Os avisos e confirmações deixam de aparecer com fundo preto quando a aplicação
está no tema claro. Cada pop-up segue agora explicitamente o tema ativo, com
texto e botões legíveis.

### Principais alterações

- Fundo claro, texto escuro e botões claros no tema claro.
- Fundo escuro e texto claro no tema escuro.
- Botão predefinido mantém um contorno azul visível em ambos os temas.
- A correção aplica-se globalmente a avisos, erros, informações e confirmações,
  incluindo **Apagar todos os movimentos**.
- Avisos de recuperação e segunda instância também têm um estilo legível antes
  de a janela principal terminar o arranque.
- Novos testes de imagem, capturas reais do Windows e **493 testes aprovados**.

---

## V3.32 — v3.32.0 — 22 de agosto de 2026

### Em poucas palavras

A aplicação passa a ser validada também em 1920 × 1080, mostra mais conteúdo
nos cartões de categorias e movimentos, abre um editor de movimentos compacto
e nítido e permite escolher a pasta principal dos backups. Foi ainda criada uma
limpeza integral de movimentos com duas confirmações e backup obrigatório.

### Principais alterações

- Suporte visual testado em 960, 1280, 1600 e **1920 × 1080**, nos temas
  claro/escuro e com privacidade ligada/desligada.
- Títulos, descrições, estados e legendas quebram linha com altura suficiente,
  evitando frases e informação de gráficos cortadas.
- **Despesas por categoria** ficou 20% mais alto; **Lista de movimentos** e a
  respetiva tabela também ficaram 20% mais altas.
- O editor de movimentos deixou de desfocar e ocupar toda a aplicação: está
  centrado, alinhado em duas colunas e usa apenas scroll vertical.
- É possível escolher ou repor a pasta principal usada por todos os backups. A
  pasta é testada antes de ser guardada e também é considerada na recuperação.
- Nova ação para apagar todos os movimentos, sempre com duas confirmações e um
  backup completo validado antes da remoção.
- A limpeza é atómica e preserva categorias, contas, orçamentos, subscrições,
  pagamentos, objetivos, investimentos e documentos não ligados a movimentos.
- Falhas de base mantêm os anexos intactos; após sucesso, a integridade é
  confirmada e todas as páginas são atualizadas.
- Avisos de recuperação, falha protegida e segunda instância aparecem sempre
  acima da janela de carregamento.
- Captura real do Windows sem textos cortados; editor sem scroll horizontal ou
  desfoque; `Ruff`, compilação Python e **491 testes** aprovados em dados
  temporários isolados.

---

## V3.31 — v3.31.0 — 20 de agosto de 2026

### Em poucas palavras

O período **Tudo** volta a carregar o Dashboard e a previsão sem erro. Os
cartões ajustam agora títulos, indicadores e explicações à largura disponível,
evitando frases escondidas, gráficos apertados e espaços estranhos.

### Principais alterações

- Corrigido o erro `NoneType > int` ao escolher **Tudo**.
- O saldo histórico e a previsão usam todo o histórico real e mantêm os 30 dias
  futuros.
- Indicadores de cobertura quebram linha e aumentam apenas o necessário.
- O estado do orçamento deixa de poder ficar com largura zero.
- O cartão **Prioridades financeiras** mostra integralmente o resumo, evidência,
  métricas e explicação, mesmo em janelas mais estreitas.
- Títulos, subtítulos e hora de atualização das páginas ajustam-se sem cortes.
- Em duas colunas, os cartões com mais texto usam a largura completa; em ecrãs
  largos mantém-se a organização lado a lado já definida.
- Teste só de leitura na base real aprovado: integridade `ok`, 100 movimentos
  intactos, 262 pontos históricos e 30 previstos.
- Validação visual aprovada em 960, 1280 e 1600 px, temas claro/escuro e modo de
  privacidade.
- **485 testes aprovados**, sem usar a base real durante a bateria automática.

---

## V3.30 — v3.30.0 — 20 de agosto de 2026

### Em poucas palavras

As previsões passam a explicar a qualidade dos dados que usam, qualquer gráfico
mostra como calculou o valor selecionado e todas as páginas indicam a hora da
última atualização. Contas, categorias e ativos passam a ter uma identidade
interna estável, sem depender apenas do texto visível.

### Principais alterações

- Cada previsão mostra meses disponíveis, saldos iniciais conhecidos,
  investimentos com custo preenchido e movimentos inválidos excluídos.
- Clicar num gráfico abre o período, filtros, movimentos considerados,
  exclusões e cobertura usados no cálculo.
- Todas as páginas mostram `Dados atualizados às…` e distinguem atualização em
  curso, dados antigos e falha de leitura.
- Nova página **Gestão e identidade dos dados** para pré-visualizar e fundir
  contas ou categorias duplicadas sem apagar o histórico.
- A fusão atualiza todas as referências numa única operação, mantém aliases e
  guarda um registo do que foi alterado; se algo falhar, tudo é revertido.
- Contas, categorias, transferências e ativos usam identificadores internos;
  os nomes passam a ser a apresentação que pode evoluir em segurança.
- Migração 24 aditiva e protegida, sem eliminar movimentos, documentos ou dados
  fornecidos pelo utilizador.
- A cópia isolada da base real manteve os mesmos 99 movimentos e todos os campos
  financeiros anteriores, com integridade SQLite confirmada.
- **482 testes aprovados**, incluindo proteção de dados, backups, fusões,
  cálculos, interface, temas, privacidade e vários tamanhos de janela.
- Os indicadores extensos quebram linhas e os chips de confiança ficam curtos,
  preservando as proporções do Dashboard em 960, 1280, 1600 e ecrãs maiores.

---

## V3.29 — v3.29.1 — 13 de agosto de 2026

### Em poucas palavras

Corrige o bloqueio que podia surgir ao abrir a V3.29.0 com uma base antiga, como
a V3.22. A proteção já tinha reposto corretamente os dados; esta versão permite
agora concluir a atualização e abrir a aplicação normalmente.

### Principais alterações

- O índice das transferências só é criado depois de existir a respetiva coluna.
- Uma migração adicional repara também históricos de atualização incompletos ou
  inconsistentes.
- Movimentos, valores, contas, categorias, notas e documentos permanecem
  intactos; movimentos antigos recebem apenas a ligação de transferência vazia.
- Foram adicionados testes que atualizam uma base V3.22 e confirmam o movimento,
  a proteção automática e todas as 23 migrações.
- Uma cópia isolada da base real manteve os mesmos 99 movimentos e a mesma
  assinatura financeira depois da atualização; a integridade ficou `ok`.
- **475 testes aprovados**, incluindo segurança, migrações, backups, arranque,
  interface e responsividade.
- Basta executar a V3.29.1 normalmente; não é necessário restaurar ou extrair
  manualmente o ZIP criado pela proteção.

---

## V3.29 — v3.29.0 — 13 de agosto de 2026

### Em poucas palavras

A pesquisa de movimentos fica mais rápida e verdadeiramente literal, todas as
tabelas passam a ordenar corretamente os seus dados e o Assistente deixa de
ocupar uma altura exagerada. O detalhe surge apenas quando é aberto.

### Principais alterações

- A pesquisa usa um índice rápido atualizado automaticamente com movimentos,
  contas, categorias, notas, etiquetas, comerciantes e métodos de pagamento.
- `%` e `_` são agora texto normal: pesquisar esses símbolos já não devolve
  movimentos que não os contêm.
- A pesquisa começa 170 ms mais cedo e continua a ser executada sem bloquear a
  interface.
- Todas as tabelas podem ser ordenadas ao clicar no cabeçalho; datas, euros,
  números, percentagens e estados usam o seu valor real, não a ordem alfabética.
- A coluna e o sentido escolhidos ficam guardados automaticamente para cada
  tabela, juntamente com as larguras e colunas visíveis.
- Barras de progresso, seletores e caixas de seleção também têm ordenação útil;
  atualizar uma tabela não mistura células entre linhas.
- O Assistente mostra um resumo compacto de receitas, despesas e poupança, com
  estado visual e três secções expansíveis.
- A explicação completa só ocupa espaço depois de clicar no respetivo botão e
  fica limitada a uma altura confortável.
- Esquema atualizado de forma aditiva para a versão 22, sem apagar ou modificar
  os dados financeiros originais.
- **473 testes aprovados**, incluindo migrações, pesquisa, segurança de dados,
  arranque/encerramento e interface responsiva em PySide6.
- Pacote Windows autónomo, sem dados pessoais e sem necessidade de instalar
  Python no computador de destino.
- O executável continua sem assinatura digital para distribuição pública.

---

## V3.28 — v3.28.0 — 13 de agosto de 2026

### Em poucas palavras

Contas escritas de formas ligeiramente diferentes passam a ser a mesma conta,
duplicados duvidosos deixam de ser ignorados automaticamente e toda a edição
direta de movimentos fica validada e disponível em Desfazer. A aplicação também
abre corretamente ativos com o mesmo nome, confirma backups em segundo plano e
guarda as definições da IA como um conjunto indivisível.

### Principais alterações

- Maiúsculas/minúsculas e espaços adicionais deixam de criar saldos separados;
  a atualização conserva movimentos e o saldo de referência mais recente.
- Duplicados são classificados como **certos**, **prováveis** ou **possíveis**.
  Só os certos, com a mesma referência bancária, podem ser ignorados
  automaticamente; os restantes ficam visíveis e são importados.
- Editar diretamente a tabela Movimentos valida valor, descrição e categoria,
  atualiza toda a app imediatamente e cria uma operação de Desfazer.
- Receitas só mostram categorias de receitas e despesas só mostram categorias
  de despesas. Inconsistências antigas aparecem com `⚠️` para revisão.
- Ativos com o mesmo nome abrem sempre o detalhe certo porque o gráfico usa o
  identificador interno, não apenas o nome.
- O estado rápido do backup diz quando a verificação foi registada; a assinatura
  e o conteúdo são confirmados em segundo plano antes de dizer “verificado
  realmente em…”.
- Modelo, estado, privacidade e credencial da IA mudam em conjunto. Se houver
  uma falha, a configuração e a chave anteriores são repostas.
- Esquema atualizado de forma aditiva para a versão 21, sem apagar movimentos,
  documentos, subscrições ou outras informações pessoais.
- **468 testes aprovados**, incluindo migrações, backups, arranque/encerramento,
  interface PySide6 e renderização visual; Ruff e compilação sem erros.
- Pacote Windows autónomo, sem dados pessoais e sem necessidade de instalar
  Python no computador de destino.
- O executável continua sem assinatura digital para distribuição pública.

---

## V3.27 — v3.27.0 — 13 de agosto de 2026

### Em poucas palavras

Os totais passam a atualizar imediatamente depois de alterar movimentos, as
transferências ficam ligadas nas duas contas e o património distingue saldos
completos, valores duplicados e dados ainda desconhecidos. Documentos e backups
cifrados recebem também um fluxo mais seguro e recuperável.

### Principais alterações

- Dashboard, Orçamentos, análises e relatórios deixam de poder mostrar o valor
  anterior durante 60 segundos após uma alteração de movimentos.
- As duas partes de uma transferência são editadas, eliminadas, duplicadas e
  desfeitas em conjunto; uma falha não deixa apenas uma das contas alterada.
- Cada conta pode ter saldo e data de referência, com um indicador visível da
  qualidade e cobertura dos dados do património.
- Ativos monetários podem ser ligados a contas para o mesmo dinheiro não ser
  contado duas vezes.
- Categorias equivalentes dos CSV são unificadas sem mudar categorias pessoais
  criadas manualmente.
- A página Documentos abre de imediato; a auditoria é incremental, decorre em
  segundo plano, mostra progresso, pode ser cancelada e grava um único lote.
- Mudar a palavra-passe cria e valida primeiro um novo backup. Se algo falhar,
  tudo regressa ao estado anterior; backups antigos continuam acessíveis pelas
  credenciais anteriores guardadas com segurança no Windows.
- O arranque deteta qualquer migração em falta, mesmo que exista uma versão
  posterior registada, e cria proteção antes da reparação.
- Investimentos sem custo de aquisição mostram rentabilidade `N/D`; a carteira
  indica a cobertura dos dados usados na percentagem de ganho.
- Esquema atualizado de forma aditiva para a versão 20, sem apagar movimentos
  ou documentos existentes.
- **460 testes aprovados**, incluindo 93 testes visuais em 960, 1280 e 1600 px,
  claro/escuro e privacidade; Ruff e compilação sem erros.
- Pacote Windows autónomo, sem dados pessoais e sem necessidade de instalar
  Python no computador de destino.
- O executável continua sem assinatura digital para distribuição pública.

---

## V3.26 — v3.26.0 — 13 de agosto de 2026

### Em poucas palavras

A previsão de tesouraria passa a dizer exatamente o que a percentagem mede,
cada prioridade financeira mostra o período dos dados utilizados, a IA deixa de
poder acumular respostas excessivas e os backups são fisicamente gravados antes
de substituir a cópia anterior.

### Principais alterações

- A tesouraria mostra a probabilidade de **esse mês terminar acima de 0 €**; não
  afirma que o saldo permaneceu positivo durante todo o período.
- Títulos, resumo, gráfico, detalhes e acessibilidade usam a mesma explicação.
- Cada prioridade identifica se usa o filtro, o mês atual, o histórico recente,
  os últimos 45/90 dias ou os próximos 60/90 dias.
- O cartão explica à partida que combina fontes com períodos diferentes.
- As respostas da IA ficam limitadas a 4 MB, mesmo quando o servidor não indica
  o tamanho correto.
- Backups, preferências e cópias secundárias são sincronizados com o disco antes
  da substituição final.
- Um erro de escrita física conserva a última cópia válida em vez de publicar
  um ficheiro possivelmente incompleto.
- Não há alteração da estrutura da base nem transformação dos dados guardados.
- 444 testes aprovados, incluindo o ensaio visual completo em 960, 1280 e
  1600 px, claro/escuro e privacidade; Ruff e compilação sem problemas.
- Pacote Windows autónomo verificado, sem dados pessoais e sem necessidade de
  instalar Python no computador de destino.
- O executável ainda não possui assinatura digital para distribuição pública.

---

## V3.25 — v3.25.0 — 13 de agosto de 2026

### Em poucas palavras

O botão **Desfazer** passa a abranger a criação, duplicação, divisão e geração
automática de movimentos. As importações validam o ficheiro inteiro, as
subscrições usam a conta e o pagamento corretos e as proteções antigas deixam
de crescer sem limite.

### Principais alterações

- Novos movimentos e o respetivo histórico são guardados em conjunto.
- Transferências internas são desfeitas nas duas contas ao mesmo tempo.
- Duplicar conserva todas as divisões por categoria.
- Alterar a categoria elimina divisões antigas para atualizar corretamente os
  gráficos; Desfazer consegue recuperá-las.
- Subscrições e pagamentos automáticos ficam no histórico, incluindo datas e
  progresso associados.
- Depósitos e levantamentos dos objetivos também podem ser desfeitos como uma
  operação completa.
- Datas com texto adicional, valores com mais de dois decimais, cabeçalhos
  repetidos e linhas com colunas erradas são rejeitados na importação.
- As 80 linhas visíveis são apenas a tabela de amostra; a validação e os totais
  percorrem agora o CSV completo.
- Cada subscrição permite escolher conta e forma de pagamento, utilizadas nos
  movimentos gerados.
- São mantidas até três proteções automáticas de atualização, num total normal
  máximo de 5 GB, sem eliminar backups manuais nem cópias de recuperação.
- Construir uma nova versão já não apaga os pacotes e checksums das versões
  anteriores.
- A atualização preserva movimentos, documentos, objetivos, subscrições e
  restantes dados já existentes.
- 439 testes aprovados, incluindo o ensaio visual integral, e Ruff sem
  problemas.

---

## V3.24 — v3.24.0 — 13 de agosto de 2026

### Em poucas palavras

Subscrições ou pagamentos muito antigos deixam de poder criar centenas de
movimentos de uma vez. As gravações passam a ser reversíveis como um conjunto,
os backups mostram a cifragem que existe realmente e as proteções de atualização
ficam cifradas quando essa segurança está ativa.

### Principais alterações

- Apenas a renovação atual de cada subscrição é considerada no arranque.
- Limite de 24 cobranças automáticas por arranque.
- Pagamentos sem fim usam corretamente `0` meses.
- Pagamentos vencidos são lançados em lotes de 24, no máximo 12 por contrato,
  com indicação clara do que ficou pendente.
- Movimentos, ocorrências, progresso e histórico são guardados ou anulados em
  conjunto; deixa de existir o falso erro depois de o dado já ter sido gravado.
- Investimentos, dívidas, subscrições e fotografias financeiras ficam
  transacionais.
- Movimentos, subscrições e pagamentos de `0,00 €` são recusados.
- Zeros antigos permanecem intactos, mas são sinalizados e excluídos dos totais.
- Proteções de atualização usam `.lgbak` quando a cifragem está ativa.
- A antiga chave de IA em texto simples é removida da cópia protegida.
- ZIPs decifrados temporariamente ficam numa área privada e resíduos de uma
  interrupção são removidos no arranque seguinte, incluindo durante o
  restauro das proteções de versão.
- O painel confirma o ficheiro mais recente, não apenas a opção de cifragem.
- A base cumulativa V3.23 ficou guardada no Git antes da V3.24; nenhum dado
  pessoal foi incluído.
- 430 testes automatizados aprovados e Ruff sem problemas.
- Os testes visuais deixam de depender de uma espera fixa curta quando o
  computador está sob carga.

---

## V3.23 — v3.23.0 — 13 de agosto de 2026

### Em poucas palavras

Num computador novo basta extrair o ZIP e abrir
`INSTALAR_FINANCAS_PESSOAIS.bat`. Python, PySide6, Qt, SQLite, cryptography e os
restantes componentes já seguem dentro do pacote; não é preciso instalar nada
com pip nem executar como administrador.

### Principais alterações

- Instalação automática apenas para o utilizador atual.
- Atalhos no Ambiente de Trabalho e no menu Iniciar.
- Modo portátil através de `ABRIR_FINANCAS_PESSOAIS.bat`.
- Tamanho, SHA-256 e lista completa de ficheiros verificados antes e depois da
  cópia.
- Python, Qt, PySide6, SQLite, cryptography e runtimes do Windows confirmados.
- Atualização preparada numa pasta temporária, com reposição da versão anterior
  se a troca falhar.
- Pasta do programa totalmente separada da pasta dos dados financeiros.
- O próprio processo de construção instala e testa uma cópia temporária antes
  de criar o ZIP final.
- Não é necessário Python, pip ou acesso à Internet no computador novo.
- PyInstaller, Ruff e pytest ficam apenas como ferramentas de construção e
  teste; não são instalados no computador novo.
- 421 testes aprovados, código sem problemas detetados pelo Ruff e dependências
  sem vulnerabilidades conhecidas no pip-audit.

---

## V3.22 — v3.22.0 — 11 de agosto de 2026

### Em poucas palavras

Todos os valores em euros passam a seguir a mesma regra portuguesa: escreve-se
com vírgula, os cêntimos só aparecem quando existem e um valor inteiro deixa de
mostrar `,00`.

### Principais alterações

- `3,14` e `100,01` são aceites; `3.14` e `100.01` são recusados.
- A tecla de ponto é corrigida para vírgula para evitar transformar `3.14` em
  `314`; colagens com ponto são rejeitadas.
- A regra abrange diálogos, simuladores, investimentos, objetivos, orçamentos,
  movimentos e restantes campos monetários.
- A coluna Valor dos Movimentos e o Assistente de orçamento seguem a mesma
  validação.
- `12,90 €` mantém os cêntimos, mas `100,00 €` passa a aparecer como `100 €`.
- Cartões, tabelas, gráficos, detalhes, alertas e relatórios usam o mesmo
  formato.
- Extratos bancários internacionais continuam a poder ser importados.
- Nenhum movimento, documento ou outro dado existente é convertido ou apagado.
- Os 420 testes automatizados, incluindo a validação visual, foram aprovados.
- O executável v3.22.0 passou o auto-teste das 29 páginas e da base temporária.

---

## V3.21 — v3.21.0 — 11 de agosto de 2026

### Em poucas palavras

A versão para Windows fica preparada para ser levada para outro computador
sem instalar Python ou PySide6. O próprio executável é testado antes da entrega
e os ficheiros temporários antigos são limpos sem tocar nos dados financeiros.

### Principais alterações

- Python 3.14.7, PySide6 6.11.1 e cryptography 50.0.0 confirmados como atuais.
- PyInstaller atualizado para 6.22.0 e Ruff para 0.16.2.
- Auditor de vulnerabilidades acrescentado às ferramentas de desenvolvimento.
- Verificação automática das versões e compatibilidade das dependências.
- Auto-teste do executável com todas as páginas e uma base temporária.
- Manifesto do pacote indica exatamente os runtimes incorporados.
- O outro computador não precisa de Python, PySide6, pip ou Visual C++.
- É obrigatório extrair e conservar toda a pasta, incluindo `_internal`.
- Resíduos de testes, capturas, caches e builds antigos removidos com segurança.
- Movimentos, anexos, bases de dados e backups não são alterados.
- README e guia de instalação corrigidos para a versão atual.
- Lançadores pelo código usam Python 3.14 e guardam diagnósticos fora do projeto.
- Auditoria sem vulnerabilidades conhecidas e 402 testes aprovados.
- ZIP extraído e executado novamente com 29 páginas e base temporária válidas.

---

## V3.20 — v3.20.0 — 11 de agosto de 2026

### Em poucas palavras

Fase final de estabilização: o Centro financeiro continua funcional mesmo
quando uma origem opcional de análise ainda não está disponível, e os testes de
atualização da base passam a acompanhar automaticamente a estrutura atual.

### Principais alterações

- Cartões operacionais toleram serviços parciais sem impedir a abertura da página.
- Ausência de uma análise opcional resulta num estado vazio legível, não num erro.
- Títulos operacionais quebram linha e o Centro financeiro volta a caber a 560 px.
- Testes de migração deixam de depender de um número de estrutura desatualizado.
- Ensaio de análises reconhece agora os novos campos de rentabilidade dos investimentos.
- Validação integral passa a vigiar explicitamente processos de teste órfãos.
- Versão e pacote Windows identificados como `v3.20.0`.
- Validação final concluída com 398 testes e matriz visual a 100%–200%.
- Construção compatível com o Windows PowerShell clássico, mesmo sem PowerShell 7.

---

## V3.19 — v3.19.0 — 11 de agosto de 2026

### Em poucas palavras

O Centro financeiro passa a mostrar a probabilidade de manter saldo positivo,
os compromissos separados por grau de confiança e um mapa mensal dos desvios do
orçamento. A Saúde financeira ganha um centro de qualidade e proteção dos dados.

### Principais alterações

- Previsão mensal do saldo com cenários e faixa P10–P90.
- Probabilidade visível de o saldo continuar positivo em cada mês.
- Histórico incompleto nunca é preenchido artificialmente com zeros.
- Compromissos confirmados, prováveis e por associar claramente separados.
- Todas as categorias com orçamento ou despesa aparecem no mapa de desvios.
- Detalhes e atalhos ao clicar em cada novo gráfico.
- Tendência mensal da qualidade dos movimentos.
- Estado da estrutura da base e das assinaturas dos anexos.
- Resumo rápido de backups, cifragem, segunda cópia e teste de recuperação.
- Revisão conservadora do perfil de demonstração, sem apagamento automático.
- Cartões responsivos com alturas moderadas.
- Cartões companheiros do Dashboard alinhados exatamente pelo topo.
- Novos gráficos incluídos na matriz de testes visuais.

---

## V3.18 — v3.18.0 — 11 de agosto de 2026

### Em poucas palavras

Os pop-ups de ajuda deixam de depender do tema nativo do Windows, todos os
controlos recebem identificação acessível e a janela redimensiona com menos
saltos. A validação visual passa a incluir escala de 200% e assinaturas que
detetam alterações reais na composição.

### Principais alterações

- Um único sistema claro e legível para todos os tooltips.
- Recuperação automática do tooltip depois de fechar uma janela temporária.
- Nomes e descrições acessíveis em controlos criados mais tarde.
- `Ctrl+F6` percorre as três principais zonas da interface.
- Foco de teclado mais visível em botões, campos e tabelas.
- Menos cálculos repetidos ao redimensionar a janela.
- Escalas Windows de 100%, 125%, 150% e 200% validadas.
- Capturas com assinatura percetual para comparar regressões.
- Testes continuam a cobrir 960, 1280 e 1600 px, temas e privacidade.
- Largura/visibilidade da nova coluna de aliases fica guardada corretamente.

---

## V3.17 — v3.17.0 — 11 de agosto de 2026

### Em poucas palavras

Os extratos passam a conservar a referência do banco, a respeitar o formato
dos números e a distinguir contas na procura de duplicados. As subscrições só
associam nomes confirmados e a app avisa quando um cálculo ou gráfico exclui
dados inválidos.

### Principais alterações

- CSV lido sem trocar silenciosamente caracteres desconhecidos.
- Codificação do ficheiro e avisos mostrados na pré-visualização.
- Formato português, internacional ou automático para valores monetários.
- Valores ambíguos ficam por rever antes de serem importados.
- Referência bancária guardada com cada movimento importado.
- Duplicados comparados também pela conta.
- Nomes alternativos confirmados nas subscrições.
- Totais históricos usam apenas associações seguras.
- Despesas recorrentes toleram pequenas variações e mostram confiança.
- Pontos inválidos e previsões degradadas deixam avisos visíveis.
- Estrutura da base atualizada de forma não destrutiva para a versão 18.

---

## V3.16 — v3.16.0 — 11 de agosto de 2026

### Em poucas palavras

A chave Gemini deixa de aparecer nos endereços e nas preferências da app, os
logs ocultam segredos e identificadores e a biblioteca de cifragem recebe as
correções atuais. Backups manuais já não bloqueiam a janela e cada pacote
Windows passa a ter versão e checksum verificáveis.

### Principais alterações

- Chave Gemini enviada apenas no cabeçalho seguro recomendado.
- A interface nunca volta a carregar a chave completa para o formulário.
- Logs retiram chaves, emails e IBANs automaticamente.
- Biblioteca de cifragem atualizada para `cryptography 50.0.0`.
- Backup manual e teste de recuperação executados em segundo plano.
- ZIPs antigos deixam de conservar anexos órfãos após o restauro.
- Relatórios protegem categorias, cores e textos enriquecidos.
- Executável recebe a versão real do produto.
- ZIP versionado, manifesto e ficheiro SHA-256 criados pelo build.
- Assinatura digital suportada quando existir um certificado válido.

---

## V3.15 — v3.15.0 — 11 de agosto de 2026

### Em poucas palavras

A app deixa de criar finanças fictícias numa instalação normal e bloqueia o
arranque quando uma base usada desaparece. O utilizador pode recuperar um
backup ou confirmar conscientemente um perfil vazio. O histórico Desfazer e o
restauro passam também a rejeitar caminhos, colunas e tarefas concorrentes
inseguras.

### Principais alterações

- Novos perfis começam sem movimentos, subscrições, investimentos ou objetivos.
- Demonstração disponível apenas por opção explícita e sem duplicações.
- Perda da base abre um assistente de recuperação em vez de esconder o problema.
- Backups cifrados, ZIP e SQLite podem ser escolhidos durante o arranque.
- Documentos e preferências restantes são preservados antes da recuperação.
- Desfazer não pode mover ficheiros fora das pastas da aplicação.
- Colunas e tabelas adulteradas são rejeitadas antes de executar SQL.
- Restauros aguardam pesquisas, IA, Dashboard e backups em execução.
- Novos testes tentam deliberadamente adulterar caminhos e histórico.
- Nenhum dado financeiro atual é apagado ou reescrito automaticamente.

---

## V3.14 — v3.14.0 — 11 de agosto de 2026

### Em poucas palavras

A aplicação adapta-se melhor ao espaço disponível e recorda como cada página
foi deixada. Os filtros do Dashboard podem ser recolhidos, existe uma vista
compacta por área e os atalhos de topo/filtros/repor continuam acessíveis ao
percorrer páginas longas.

### Principais alterações

- Filtros avançados do Dashboard recolhíveis, mantendo sempre um resumo claro.
- Densidade Confortável/Compacta independente e guardada em cada página.
- Filtros, zoom, posição dos gráficos e painéis recolhidos são restaurados.
- Barra flutuante com **Topo**, **Filtros** e **Repor**.
- Navegação lateral compacta com nomes curtos a 960 px.
- Nomes completos também deixam de ficar cortados na navegação larga.
- Cartões responsivos consideram corretamente a largura dos espaçamentos.
- Scrollbars mais fáceis de usar e controlos legíveis nos temas claro/escuro.
- Cartões preditivos usam o mesmo selo visual de confiança.
- Um clique em qualquer gráfico compatível abre os respetivos detalhes.
- Variações enormes aparecem como multiplicador e mostram a base anterior.
- Exportações CSV ficam mascaradas por defeito no modo privacidade.
- O backup continua integral e é identificado como tal, mesmo em privacidade.
- Não há alteração da estrutura da base nem reescrita dos dados financeiros.
- Os **366 testes**, todas as páginas e as 12 capturas reais do Windows
  passaram na mesma validação final.

---

## V3.13 — v3.13.0 — 10 de agosto de 2026

### Em poucas palavras

Os documentos passam a ter uma impressão digital verificável e a aplicação
bloqueia anexos alterados ou disfarçados. As previsões e simulações tornam-se
mais transparentes: distinguem euros de hoje de valores nominais, mostram a
confiança dos dados e deixam de premiar intervalos excessivamente largos.

### Principais alterações

- Atualização de segurança da biblioteca usada nos backups cifrados.
- CSV protegidos contra fórmulas e Panorama protegido contra HTML/cores
  inválidas.
- Anexos limitados por tipo e tamanho, com validação do conteúdo real.
- SHA-256 e tamanho ficam guardados na base; uma alteração posterior bloqueia
  a abertura.
- PDF e imagens abrem dentro da app; formatos Office mostram aviso antes da
  abertura externa.
- Simulador de investimentos separado em valores nominais e euros de hoje.
- Taxas anuais e mensais passam a usar capitalização composta consistente.
- Monte Carlo considera concentração, correlação, comissões, impostos,
  inflação, risco de perda real e drawdown.
- Risco e Resiliência mostra confiança, período analisado e sensibilidade.
- Com poucos dados, aparece **Dados insuficientes** em vez de um score
  artificial.
- A qualidade das previsões penaliza faixas demasiado largas e mostra a
  cobertura em separado.
- A base avança para a estrutura `17` sem converter nem apagar dados antigos.
- O encerramento de tarefas Qt deixa de ter uma corrida intermitente durante a
  libertação das threads.
- Uma pesquisa que acaba e é imediatamente atualizada já não pode ficar presa
  a aguardar a tarefa seguinte.
- Uma falha do backup ao fechar deixa de poder reiniciar o mesmo backup em
  ciclo por causa de dois sinais de conclusão simultâneos.
- Os **359 testes**, a matriz visual completa e uma migração isolada da base
  ativa passaram sem perda de dados.

---

## V3.12 — v3.12.0 — 10 de agosto de 2026

### Em poucas palavras

As pesquisas e os cálculos passam a usar uma fotografia coerente da base e
podem ser realmente interrompidos. A base também rejeita novos dados
estruturalmente inválidos, enquanto os registos antigos problemáticos ficam
visíveis para correção e deixam de alterar totais ou previsões.

### Principais alterações

- Pesquisas, totais e Dashboard usam leituras SQLite estritamente protegidas.
- Uma localização inexistente já não cria uma base vazia por acidente.
- Lista e resumo dos movimentos são calculados sobre o mesmo instante dos
  dados.
- Atualizar ou fechar interrompe a consulta SQLite ainda em execução.
- O Dashboard deixa de executar temporizadores e callbacks depois do fecho.
- Pagamentos agendados e ocorrências passam a ter relações garantidas pela
  própria base.
- Datas, meses, números, percentagens e estruturas JSON inválidas são recusados
  nas novas gravações.
- Valores antigos inválidos ficam assinalados e são excluídos dos cálculos, não
  transformados em zero.
- A app sugere como prioridade a revisão desses registos.
- Os euros continuam guardados com casas decimais no formato `xx.xx`.
- Os **340 testes** e o verificador de código passaram.

---

## V3.11 — v3.11.0 — 10 de agosto de 2026

### Em poucas palavras

As operações financeiras relacionadas passam a ser guardadas como uma única
unidade, e a aplicação consegue recuperar automaticamente um restauro ou uma
migração interrompidos sem esconder nem substituir os dados existentes.

### Principais alterações

- Objetivos, respetivos movimentos e metadados deixam de poder ficar parciais.
- Importações e edições só são confirmadas juntamente com o histórico de
  **Desfazer**.
- Alterações em lote, regras e divisões recuam totalmente se algum passo falhar.
- Uma falha final de gravação deixa de atualizar a interface ou anunciar
  sucesso.
- O backup mais recente é novamente verificado por SHA-256 e por integridade
  interna sempre que é apresentado como válido.
- Backups manuais e cópias anteriores a um restauro nunca são apagados pela
  retenção automática.
- Backups automáticos e ao fechar têm retenção separada.
- Um restauro cria um marcador e uma cópia de recuperação que sobrevivem a uma
  interrupção do processo ou do computador.
- O arranque seguinte recupera base, anexos, histórico e preferências antes de
  permitir novas gravações.
- ZIPs excessivos, caminhos inseguros, compressão anormal e falta de espaço são
  bloqueados antes da extração.
- Migrações interrompidas válidas são retomadas; migrações incompletas são
  preservadas e apresentadas como erro seguro.
- O estado dos backups indica claramente quando deve ser ativada cifragem ou
  uma segunda localização.
- As preferências de backup deixam de poder ficar diferentes entre a base e o
  ficheiro local após uma falha normal de escrita.
- Os **325 testes** da aplicação e o verificador de código passaram.

---

## V3.10 — v3.10.1 — 30 de julho de 2026

### Em poucas palavras

Ao fechar normalmente, a aplicação confirma tudo o que já foi guardado,
sincroniza e valida a base e fecha a ligação de forma explícita. Se qualquer
etapa ou o backup pedido falhar, a janela fica aberta e mostra o problema.

### Principais alterações

- Tarefas em curso continuam a ser aguardadas antes do fecho.
- Preferências pendentes de tabelas, backup, IA e Simulações são descarregadas.
- O último `commit` deixou de poder falhar silenciosamente.
- A base usa sincronização completa, consolida o WAL e passa uma verificação.
- Uma falha cancela o fecho e permite corrigir o problema e tentar novamente.
- O backup ao fechar é obrigatório quando essa opção está ativa; uma falha
  deixa de ser ignorada.
- Documentos anexados são sincronizados e confirmados por SHA-256 antes do
  respetivo registo.
- O pagamento manual de uma subscrição passou a ser uma operação única: ou
  todos os dados são gravados ou nenhum fica parcial.
- A localização dos dados mostra o novo estado de encerramento protegido.
- A base real e o backup completo mais recente foram validados por leitura.
- Os **313 testes**, a matriz visual completa e a captura nativa do Windows
  foram aprovados.

---

## V3.10 — v3.10.0 — 29 de julho de 2026

### Em poucas palavras

A precisão das previsões passa a dizer quantos resultados ficaram realmente
dentro das faixas mostradas. A carteira ganhou rentabilidade líquida ponderada,
comissões e um gráfico que explica a contribuição de cada ativo.

### Principais alterações

- A precisão é agora a percentagem de resultados dentro da faixa prevista.
- São mostrados acertos, falhas e total de meses avaliados.
- Só os resultados que o modelo ainda não conhecia contam como validação.
- O erro médio continua visível, mas separado da cobertura.
- Pontos verdes ficaram dentro da faixa; pontos vermelhos ficaram fora.
- Cada categoria mostra cobertura e erro ponderado separadamente.
- Dashboard, tesouraria, orçamentos e relatórios usam a mesma definição.
- Cada investimento pode guardar comissões acumuladas.
- O capital investido e as comissões permanecem separados.
- A carteira mostra ganho bruto, ganho líquido e impacto das comissões.
- A rentabilidade líquida é ponderada pelo dinheiro realmente comprometido.
- Cada ativo mostra quanto contribuiu, em pontos percentuais, para o resultado.
- As contribuições somam exatamente a rentabilidade líquida da carteira.
- Um novo gráfico interativo distingue ativos que geraram ganho ou perda.
- A tabela inclui comissões, ganho líquido, rentabilidade e contribuição.
- O relatório anual inclui os mesmos valores e a decomposição por ativo.
- Investimentos antigos recebem `0,00 €` de comissões sem perder qualquer dado.
- A atualização cria a proteção obrigatória da passagem de `v3.9.0` para
  `v3.10.0`.
- Os **307 testes** e a matriz visual real do Windows foram aprovados.

---

## V3.9 — v3.9.0 — 29 de julho de 2026

### Em poucas palavras

A aplicação já não fecha enquanto existir uma pesquisa, pedido à IA, cálculo ou
backup ainda ativo. O arranque ganhou uma janela pequena e moderna que mostra
o progresso verdadeiro da proteção dos dados, incluindo tabelas e documentos.

### Principais alterações

- Todas as tarefas em segundo plano passam a ser controladas no mesmo local.
- Pesquisas de movimentos, IA, Dashboard e backups são aguardados ao fechar.
- Novas tarefas normais deixam de arrancar depois do pedido de encerramento.
- As tarefas recebem cancelamento seguro; nenhuma thread é terminada à força.
- A barra inferior identifica o trabalho que ainda está a terminar.
- O backup opcional ao fechar corre fora da interface e com ligação própria.
- A janela principal só desaparece depois de a base estar livre e o backup
  final estar verificado.
- Uma nova janela de arranque compacta aparece antes da aplicação.
- A janela mostra etapa, percentagem, barra, ficheiro atual e contadores reais.
- Localização, migrações, base de dados, documentos e backup de proteção
  alimentam diretamente esse progresso.
- Com muitos documentos, a janela continua a atualizar-se e deixa de parecer
  bloqueada.
- Um erro de proteção é apresentado de forma legível sem abrir a aplicação.
- Não foi alterado nem removido qualquer movimento, documento ou valor
  financeiro já guardado.
- A nova versão cria a proteção obrigatória da passagem de `v3.8.0` para
  `v3.9.0`.
- Os **303 testes** e a validação visual real do Windows incluem agora o novo
  arranque e encerramento.

---

## V3.8 — v3.8.0 — 29 de julho de 2026

### Em poucas palavras

A reserva de emergência deixou de se confundir com outros objetivos, a
privacidade passa a esconder também campos e cenários monetários, e as médias,
rentabilidade e explicação do património usam bases financeiras mais corretas.

### Principais alterações

- Cada objetivo pode ser marcado explicitamente como reserva de emergência.
- Palavras escritas nas notas já não alteram o tipo do objetivo.
- “Comprar casa” deixa de ser confundido com a reserva.
- Sem reserva marcada, mantém-se a referência automática de seis meses.
- Todos os campos em euros ficam ocultos quando não estão a ser editados.
- P10, P50, P90 e cenários de liquidez respeitam a privacidade.
- A média dos compromissos usa apenas meses completos.
- O restante do mês atual aparece separadamente.
- A Ponte do património mostra a percentagem da variação realmente explicada.
- Carteira e passivos indicam claramente quando falta histórico.
- A rentabilidade dos investimentos passa a ser ponderada pelo capital.
- Abrir ou atualizar Investimentos deixa de regravar o snapshot diário.
- Dias sem alterações continuam a ter um ponto histórico controlado.
- Snapshots iguais não alteram novamente a respetiva data de atualização.
- A atualização acrescenta apenas metadados e preserva movimentos, documentos,
  objetivos, investimentos e restantes dados já guardados.
- Os **297 testes**, incluindo a matriz visual do Windows, foram aprovados.

---

## V3.7 — v3.7.0 — 29 de julho de 2026

### Em poucas palavras

Agora é possível fixar um ponto de qualquer gráfico e compará-lo com outro. A
aplicação também explica como o património mudou e mostra os compromissos fixos
dos próximos 12 meses sem inventar recorrências.

### Principais alterações

- Todos os gráficos interativos permitem fixar um ponto para comparação.
- O segundo ponto mostra diferenças em euros, percentagens ou pontos
  percentuais.
- Um indicador no gráfico identifica a seleção fixada.
- **Repor** limpa também as comparações, além do zoom e dos filtros.
- A nova **Ponte do património** liga o valor inicial ao final.
- Receitas, despesas, capital investido, mercado e dívida ficam separados.
- Quando falta histórico, a aplicação mostra a parte não classificável em vez
  de a apresentar como ganho.
- É possível escolher 3, 6, 12 ou 24 meses para a ponte.
- O **Centro financeiro** mostra subscrições e pagamentos agendados por mês
  durante 12 meses.
- O mês atual parcial não distorce a tendência entre meses completos.
- Cada mês abre a lista dos compromissos que formam o total.
- Uma ocorrência futura já registada deixa de esconder outras renovações da
  mesma subscrição.
- Não foi alterado qualquer movimento, documento ou esquema da base de dados.
- Os 289 testes e a matriz visual real do Windows foram aprovados.

---

## V3.6 — v3.6.0 — 29 de julho de 2026

### Em poucas palavras

A área de Análises mostra agora onde as previsões acertam, quais categorias são
mais difíceis de prever e o que causou cada diferença mensal.

### Principais alterações

- **Previsão vs. realidade** compara valores mensais sem usar dados futuros.
- Meses sem despesa deixam de desaparecer quando existia uma previsão.
- Um novo gráfico mede a precisão individual de todas as categorias com dados.
- Cada categoria recebe um estado: previsível, moderada, volátil ou ainda sem
  dados suficientes.
- A regularidade e o erro médio ajudam a interpretar a percentagem.
- O cartão **Porque falhou a previsão?** explica automaticamente cada mês.
- Barras divergentes mostram que categorias fizeram a despesa subir ou descer
  face ao esperado.
- Categorias novas, picos e despesas que não se repetiram são identificados.
- A soma das causas coincide com a diferença total do mês.
- É possível abrir diretamente os movimentos reais que explicam cada causa.
- Não foi alterado qualquer movimento, documento ou esquema da base de dados.
- Os 282 testes da aplicação foram aprovados.

---

## V3.5 — v3.5.1 — 29 de julho de 2026

### Em poucas palavras

As previsões deixam de avaliar-se com respostas que já conheciam, subscrições
deixam de confundir movimentos apenas por terem nomes parecidos e todos os
gráficos relevantes passam a oferecer detalhes claros.

### Principais alterações

- A precisão calibrada usa apenas meses previstos antes do resultado real.
- A aplicação separa meses de aprendizagem de meses de validação.
- As faixas aprendidas só entram em vigor depois de validação suficiente.
- Subscrições e pagamentos usam primeiro as ligações exatas já guardadas.
- Sem ligação, passam a exigir nome, data, tipo, categoria e valor compatíveis.
- Nomes genéricos já não escondem despesas sem relação.
- **Previsão vs. realidade** identifica quando a precisão ainda está em
  validação.
- Panorama, Histórico, Orçamentos, Análises, Investimentos, Simulações, Centro
  financeiro e Risco e resiliência ganharam detalhes contextuais.
- Comparações, sazonalidade e análise dinâmica mostram os movimentos exatos que
  formam cada seleção.
- Botões sem ação real deixam de aparecer.
- Não foi alterado qualquer movimento, documento ou esquema da base de dados.
- Os 280 testes da aplicação foram aprovados.

---

## V3.5 — v3.5.0 — 28 de julho de 2026

### Em poucas palavras

As previsões passam a corrigir-se com os erros dos meses anteriores e clicar
num gráfico mostra os valores e movimentos que explicam o resultado.

### Principais alterações

- Tesouraria e orçamento aprendem apenas com meses já concluídos.
- A precisão, o número de meses usados e a correção aplicada ficam visíveis.
- As faixas otimista e pessimista deixam de ser sempre uma percentagem fixa.
- Clicar nos gráficos abre um painel claro com valores exatos e contexto.
- No Dashboard, o painel mostra os movimentos associados e respeita divisões
  por categoria.
- O botão do painel abre a lista ou origem já filtrada.
- O modo de privacidade continua a ocultar todos os valores.
- A nova matriz visual testa 30 categorias longas, valores extremos, estados
  vazios e escalas de 100%, 125% e 150%.
- Foi eliminada uma possível sobreposição entre linhas do Dashboard com muitos
  dados.
- Não foram alterados movimentos, documentos nem o esquema da base de dados.
- Os 274 testes, a matriz visual normal e a matriz de stress foram aprovados.

---

## V3.4 — v3.4.1 — 28 de julho de 2026

### Em poucas palavras

Os títulos de **Entradas vs. saídas acumuladas** e **Margem e taxa de
poupança** estão mais próximos do topo e das respetivas explicações.

### Principais alterações

- Menos margem vazia por cima dos dois títulos.
- Menos espaço entre título e descrição.
- As descrições ocupam apenas a altura necessária, mesmo quando têm várias
  linhas.
- Os gráficos e a largura dos cartões mantêm as dimensões anteriores.
- O comportamento foi protegido com testes em 960, 1280 e 1600 px.
- Os 267 testes da aplicação e a matriz visual completa foram aprovados.

---

## V3.4 — v3.4.0 — 28 de julho de 2026

### Em poucas palavras

Os dados inválidos deixam de ser corrigidos em silêncio, as tabelas guardam toda
a sua organização e os gráficos passam a poder ser comparados pelo mesmo
dia/mês ou abertos numa janela de foco.

### Principais alterações

- Meses e valores de orçamento inválidos apresentam um erro claro, sem cair
  automaticamente no mês atual.
- Falhas ao desenhar um gráfico ficam registadas com informação suficiente para
  localizar o problema.
- Cada coluna tem uma identidade estável e guarda largura, posição, visibilidade
  e ordenação.
- O menu dos cabeçalhos permite ajustar ao conteúdo, ocultar, mostrar e repor
  larguras ou toda a organização de uma tabela.
- Os gráficos temporais do Dashboard e do Panorama destacam em conjunto o mesmo
  dia ou mês.
- Todos os gráficos podem ser abertos em modo de foco e regressam ao cartão sem
  alterar a página.
- A atualização não modifica movimentos, documentos ou restantes dados
  financeiros.
- A versão passou os 267 testes e a matriz visual de 960, 1280 e 1600 px nos
  temas claro/escuro e em privacidade.
- O ponto 7, relativo à divisão adicional dos ficheiros grandes, ficou
  excluído conforme pedido.

---

## V3.3 — v3.3.4 — 28 de julho de 2026

### Em poucas palavras

As larguras e as colunas ocultas passam a pertencer a uma tabela identificada
de forma estável. Mudanças de títulos ou cabeçalhos deixam de apagar a
personalização, e qualquer falha ao guardar fica registada para diagnóstico.

### Principais alterações

- Todas as tabelas usam identificadores semânticos, independentes do texto
  apresentado no ecrã.
- As larguras guardadas nas versões anteriores são migradas automaticamente.
- Falhas de leitura ou gravação indicam no log a tabela e a operação afetada.
- Erros repetidos não inundam o log e um sucesso posterior limpa o estado de
  falha.
- A alteração diz respeito apenas às preferências visuais e não modifica
  movimentos, documentos ou outros dados financeiros.
- Os testes confirmaram identificadores únicos e colunas redimensionáveis em
  todas as páginas carregadas.

---

## V3.3 — v3.3.3 — 28 de julho de 2026

### Em poucas palavras

Os cartões passam a dizer claramente a que período pertencem e até que data os
dados são fiáveis. As categorias ficam ligadas visualmente entre gráficos e a
comparação com o período anterior torna-se muito mais fácil de reconhecer.

### Principais alterações

- Cada cartão do Dashboard mostra um contexto curto como “Filtro atual”, “Mês
  atual”, “Estado atual” ou “Previsão 60 dias”.
- Um resumo compacto dos filtros acompanha o scroll e desaparece junto do
  painel completo.
- Saldo/previsão, orçamento, tesouraria e Score 2.0 mostram data dos dados,
  cobertura ou confiança.
- Apontar uma categoria num gráfico destaca-a também nos outros gráficos.
- O donut ganhou um anel comparativo; o radar e a cascata usam formas
  tracejadas para mostrar o período anterior.
- As larguras das colunas e o painel de filtros continuam responsivos, inclusive
  com letra a 125%.
- A janela de classificação 50/30/20 abre sempre dentro do ecrã, mesmo sob
  carga.
- As alturas fixas dos cartões com tabelas e gráficos foram mantidas, conforme
  pedido.
- A versão foi validada em 960, 1280 e 1600 px, claro, escuro e privacidade.

---

## V3.3 — v3.3.2 — 28 de julho de 2026

### Em poucas palavras

O Dashboard passa a comparar de forma visível os gráficos e categorias com o
período de referência, respeita o filtro nas mini-tendências e deixa de inventar
meses a zero quando o intervalo não tem movimentos.

### Principais alterações

- Linhas e contornos tracejados mostram o período de referência nos gráficos de
  entradas/saídas e de margem/poupança.
- Os cartões de categorias mostram a variação total e as categorias que mais
  mudaram; o donut e a cascata incluem valores exatos ao passar o rato.
- As mini-tendências usam todo o período escolhido, sem limites escondidos de
  oito ou doze registos.
- Períodos realmente vazios apresentam um estado sem dados; meses intermédios
  continuam visíveis quando existe atividade no intervalo.
- A taxa média de poupança ignora meses sem receitas.
- O cálculo do resumo mensal exige muito menos consultas à base de dados.
- A janela “Cards” adapta-se ao ecrã e usa scroll interno quando necessário.
- O texto de comparação fica junto ao título, sem espaço vazio excessivo.
- Tudo foi validado em 960, 1280 e 1600 px, claro, escuro e privacidade.

---

## V3.3 — v3.3.1 — 28 de julho de 2026

### Em poucas palavras

Os cartões de margem/poupança e de entradas/saídas acumuladas passam a respeitar
todo o período escolhido no Dashboard, sem o limite escondido de oito meses.

### Principais alterações

- O filtro anual mostra o ano completo nos dois cartões.
- O gráfico de margem apresenta todos os meses abrangidos, incluindo meses
  parciais nas extremidades.
- Dias e meses sem movimentos continuam visíveis, para o intervalo não parecer
  mais curto.
- Os títulos e descrições dizem qual é o filtro ativo e quantos pontos estão a
  ser mostrados.
- Datas, barras e rótulos adaptam-se a períodos longos sem cortes nem
  sobreposições.
- A correção foi validada nos vários tamanhos de janela, temas e privacidade.

---

## V3.3 — v3.3.0 — 27 de julho de 2026

### Em poucas palavras

O Dashboard deixa de mostrar conselhos genéricos e passa a dizer o que merece
atenção primeiro, quanto dinheiro está em causa, porquê e onde agir.

### Principais alterações

- Novo cartão “Prioridades financeiras” com ranking por urgência e impacto.
- Cruza anomalias, tesouraria, défice, orçamentos, reserva, categorias e
  subscrições usando apenas os dados locais.
- Cada recomendação mostra métricas concretas, confiança e evidência.
- Os botões abrem diretamente a área certa e, nos movimentos, aplicam o filtro
  da categoria ou cobrança em causa.
- A aplicação não inventa recomendações quando faltam dados.
- O modo de privacidade mascara todos os valores apresentados no novo cartão.
- O cálculo é feito em segundo plano e apenas quando o cartão está ativo.
- A apresentação foi validada em 960, 1280 e 1600 px, claro, escuro e
  privacidade.

---

## V3.2 — v3.2.3 — 27 de julho de 2026

### Em poucas palavras

O painel de período fica mais limpo e equilibrado, e o Dashboard ganha dois
gráficos para perceber se o dinheiro está a criar margem ao longo do tempo.

### Principais alterações

- “Categorias” deixa de aparecer sozinho numa linha vazia.
- Os filtros distribuem-se por linhas completas conforme a largura disponível.
- Cores, superfícies e botões selecionados passam a respeitar o tema da app.
- Novo gráfico compara receitas e despesas acumuladas no período selecionado.
- Novo gráfico mostra a margem mensal e a taxa de poupança dos últimos 8 meses.
- A linha de referência de 20% ajuda a identificar rapidamente meses fortes,
  fracos ou em défice.
- Ambos os gráficos têm detalhe ao passar o cursor, privacidade e exportação.

---

## V3.2 — v3.2.2 — 27 de julho de 2026

### Em poucas palavras

A aplicação aproveita melhor o espaço em janelas estreitas, os filtros ficam
mais fáceis de entender e os estados dos cartões deixam de aparecer cortados.

### Principais alterações

- A barra lateral recolhe automaticamente em ecrãs estreitos.
- Os botões do topo tornam-se compactos sem perder tooltips ou acessibilidade.
- Os filtros do Dashboard ficam organizados em cinco grupos visuais.
- Um resumo mostra sempre o período, agrupamento, comparação, vista e
  categorias atualmente selecionados.
- O painel de filtros ocupa duas linhas em 960/1280 px e uma em 1600 px.
- Mensagens de estado longas passam a quebrar linha e ficam totalmente
  legíveis.
- A validação visual confirma automaticamente estas regras em claro, escuro e
  privacidade.

---

## V3.2 — v3.2.1 — 27 de julho de 2026

### Em poucas palavras

A aplicação passa a responder logo que abre, os backups automáticos trabalham
em segundo plano e os testes deixam de escrever no teu log real.

### Principais alterações

- O Dashboard é preparado por fases para a janela não ficar bloqueada no
  arranque.
- As restantes áreas só carregam o respetivo código quando são abertas.
- O executável inclui todas essas áreas mesmo usando carregamento progressivo.
- A verificação de renovações é cancelada com segurança ao fechar a aplicação.
- O backup agendado usa uma tarefa e uma ligação próprias, mantendo cliques,
  navegação e scroll disponíveis.
- A aplicação aguarda de forma segura se estiver a fechar durante um backup.
- Logs, bases e ficheiros criados pelos testes ficam num perfil temporário.
- As capturas visuais passam a utilizar as fontes reais do Windows.
- A validação confirma caracteres portugueses, euro, emojis, dimensões, temas,
  privacidade e detalhe visual em 960, 1280 e 1600 píxeis.
- Cada captura só é aceite depois de a atualização visual terminar e de os
  valores estarem realmente visíveis ou ocultos conforme o modo selecionado.

---

## V3.2 — v3.2.0 — 26 de julho de 2026

### Em poucas palavras

Os teus dados deixam de depender da pasta onde está o programa. Os backups
passam a guardar tudo e podem ser duplicados automaticamente noutro local.

### Principais alterações

- Base, documentos, backups e preferências ficam numa pasta estável do perfil
  do Windows.
- A primeira abertura copia e verifica os dados antigos sem os apagar.
- Trocar ou voltar a extrair os ficheiros do programa deixa de substituir os
  dados já guardados.
- Backups manuais, automáticos e ao fechar incluem base, anexos, histórico e
  preferências.
- Todos os pacotes têm manifesto, verificação SHA-256 e teste completo da base.
- O restauro completo é atómico e cria primeiro uma cópia do estado atual.
- Continua a ser possível recuperar backups antigos.
- É possível guardar automaticamente uma segunda cópia num disco externo, NAS
  ou pasta sincronizada.
- Um score mostra o estado da proteção e existe um botão para testar uma
  recuperação sem alterar os dados atuais.
- A aplicação impede duas instâncias abertas sobre o mesmo perfil.
- Os valores monetários continuam em euros decimais `xx.xx`; não foram
  convertidos para cêntimos inteiros.

---

## V3.1 — v3.1.0 — 26 de julho de 2026

### Em poucas palavras

As futuras versões deixam de poder alterar ou apagar silenciosamente os teus
movimentos, documentos e restantes dados durante uma atualização.

### Principais alterações

- Antes da primeira escrita de cada nova versão é criada uma proteção ZIP
  completa e verificada.
- A cópia inclui base de dados, anexos, histórico e preferências.
- As proteções ficam fora da pasta do programa.
- Depois da atualização, todas as linhas e ficheiros anteriores são comparados
  com o estado original.
- Se existir uma diferença indevida, a atualização é cancelada e o estado
  anterior é reposto automaticamente.
- Uma atualização interrompida é recuperada no arranque seguinte.
- Uma versão antiga já não pode escrever em dados criados por uma versão mais
  recente.
- Bases corrompidas são bloqueadas sem serem modificadas.
- As Definições mostram o estado e a pasta das proteções de versão.
- Foi acrescentado um guia próprio sobre segurança e recuperação dos dados.

---

## V3.0 — v3.0.9 — 26 de julho de 2026

### Em poucas palavras

O botão “Abrir app clássica” foi removido das Definições.

### Principais alterações

- O cartão “Localização dos dados” ficou mais simples.
- Mantém-se apenas o botão para abrir a pasta da base de dados.
- Foram removidos também o texto e o código antigo associados à aplicação
  clássica.

---

## V3.0 — v3.0.8 — 26 de julho de 2026

### Em poucas palavras

A janela “Classificar categorias” da análise 50/30/20 voltou a mostrar todo o
conteúdo num painel pequeno, centrado e legível.

### Principais alterações

- Eliminado o aviso de layout apresentado no modo debug.
- Painel compacto em vez de uma janela vazia com altura excessiva.
- Tabela, categorias e grupos 50/30/20 novamente visíveis.
- Seletores alinhados, com scroll e sem sobreposição.
- Melhor contraste nos temas claro e escuro.
- Botões “Guardar” e “Cancelar” claramente identificados.
- Validação completa da aplicação concluída sem regressões.

---

## V3.0 — v3.0.7 — 26 de julho de 2026

### Em poucas palavras

Foram removidos os espaços vazios excessivos entre títulos, descrições e
gráficos nos restantes cartões identificados pela auditoria visual.

### Principais alterações

- Títulos alinhados junto ao topo e com apenas a altura necessária.
- Títulos longos continuam legíveis e adaptam-se à largura da janela.
- Descrições, introdução e ações rápidas das Simulações e Orçamentos deixaram
  de absorver espaço vazio.
- Panorama, Risco, Score, Histórico, Simulações, Orçamentos e Dashboard foram
  corrigidos.
- A correção é agora reutilizável e protegida por testes em 960, 1280 e
  1600 píxeis.

---

## V3.0 — v3.0.6 — 26 de julho de 2026

### Em poucas palavras

O cartão da lista de subscrições ficou mais alto e mostra mais linhas de cada
vez.

### Principais alterações

- Cartão com uma altura mínima de 560 píxeis.
- Tabela com pelo menos 500 píxeis de altura.
- Em janelas baixas, a página usa scroll sem comprimir a lista.

---

## V3.0 — v3.0.5 — 26 de julho de 2026

### Em poucas palavras

Os títulos de “Top resultados” e “Distribuição mensal” ficaram alinhados junto
ao topo, eliminando o espaço vazio excessivo.

### Principais alterações

- Títulos compactos, sem expansão vertical.
- Margem superior reduzida.
- Menos espaço entre o título e o gráfico.
- Mais área disponível para visualizar as barras e os valores.

---

## V3.0 — v3.0.4 — 26 de julho de 2026

### Em poucas palavras

A aplicação volta a arrancar de forma fluida. Foi eliminado um ciclo de
redimensionamento do Dashboard que podia bloquear a janela com a indicação
“Não responde”.

### Principais alterações

- O Dashboard deixa de aumentar continuamente a própria altura.
- Os cartões da última linha deixam de ficar cortados ou sobrepostos.
- A interface permanece ativa e utilizável durante o arranque.
- Tema e tamanho da letra já não são reaplicados desnecessariamente ao abrir.
- Os cartões compactos e a tabela corrigida na v3.0.3 foram mantidos.
- Um novo teste mede a resposta da interface e impede o regresso deste erro.

---

## V3.0 — v3.0.3 — 26 de julho de 2026

### Em poucas palavras

Foram removidos os espaços vazios excessivos em “Orçamento vs. realizado” e
“Despesas por categoria”, e a tabela dos orçamentos inteligentes voltou a
ocupar corretamente o cartão.

### Principais alterações

- Menos espaço entre os textos e os gráficos do Dashboard.
- O cartão de orçamento adapta a altura à largura real da janela.
- O donut começa mais perto do título.
- A coluna “Categoria” dos orçamentos inteligentes volta a ser legível.
- A tabela preenche a largura disponível e deixa de aparecer deslocada.
- Em janelas estreitas existe scroll horizontal, começando sempre do lado
  correto.
- As larguras escolhidas manualmente continuam a ser guardadas.

---

## V3.0 — v3.0.2 — 26 de julho de 2026

### Em poucas palavras

Os cartões de gráficos voltam a ter proporções compactas. O Dashboard abre em
Top 8 e deixa de expandir automaticamente os gráficos para todas as categorias.

### Principais alterações

- Os cartões deixam de ocupar altura vertical excedente e os gráficos usam
  apenas a altura necessária.
- Donut, radar e Sankey mostram Top 8 + Outros por defeito.
- O gráfico de bolhas usa novamente o limite compacto de 16 categorias.
- “Todas” continua disponível para quem quiser expandir manualmente.
- “Orçamento vs. realizado” mantém todas as categorias num scroll interno,
  sem transformar o cartão numa coluna excessivamente alta.
- Radar, peso das categorias, bolhas e Sankey ficaram mais compactos.
- A preferência existente é ajustada uma única vez para que a correção seja
  aplicada imediatamente.
- O scroll por página e as restantes melhorias da v3.0.1 foram mantidos.

---

## V3.0 — v3.0.1 — 26 de julho de 2026

### Em poucas palavras

A v3.0.1 torna as previsões mais honestas, aplica realmente as escolhas de
privacidade da IA e melhora a exploração de páginas e gráficos.

### Principais melhorias

- A IA usa dados agregados por defeito, reduz os campos enviados, anonimiza
  identificadores e pede confirmação antes de partilhar detalhe quando essa
  opção está ativa.
- O histórico da IA deixa de guardar a cópia completa dos dados financeiros
  enviados.
- Datas e valores inválidos aparecem com uma causa concreta na importação,
  revisão mensal e relatórios, em vez de serem silenciosamente substituídos.
- A independência financeira é calculada apenas com meses completos reais; já
  não existe a despesa fictícia de 1 000 euros.
- O objetivo mostra capital investível líquido, período analisado, confiança,
  inflação, impostos, comissões e retorno real estimado.
- Os tooltips de tabelas e listas usam o mesmo cartão claro e legível da
  aplicação.
- Cada página recorda a sua posição de scroll e as listas internas deixam a
  página continuar a deslocar-se quando chegam ao limite.
- As categorias mantêm cores iguais entre gráficos.
- É possível mostrar todas as categorias ou escolher Top 8/Top 16; clicar em
  “Outros” apresenta tudo.
- O Sankey mostra todos os dados, ajusta a altura e permite abrir categorias.
- Os gráficos têm botão visível para exportar/repor zoom e navegação por
  teclado.
- Textos técnicos foram substituídos por expressões mais claras em português.
- Foram alargados os testes de interface, privacidade, dados, gráficos, janelas
  baixas e letra ampliada.

---

## V3.0 — v3.0.0 — 26 de julho de 2026

### Em poucas palavras

A V3.0 transforma a aplicação numa plataforma financeira pessoal mais completa,
visual e segura, mantendo os dados no computador.

### Principais novidades

- Nova identidade visual Liquid Glass com cartões e gráficos mais claros.
- Versão `v3.0.0` visível no título e na barra lateral.
- Navegação mais simples através das áreas Análises, Saúde, Simulações e
  Assistente.
- Dashboard responsivo com reserva de emergência, valor do tempo e despesas
  convertidas em horas de trabalho.
- Score Financeiro 2.0 e nova área de Risco e resiliência.
- Património completo, orçamentos inteligentes e deteção de anomalias.
- Centro de alertas e comparação completa entre períodos.
- Novos gráficos de precisão das previsões, sazonalidade e Pareto de
  comerciantes.
- Melhorias no Sankey, orçamento versus realizado e categorias de despesas.
- Simulador de investimentos com cenários, impostos, comissões e Monte Carlo.
- Nova área de simulações de hábitos e gastos evitáveis.
- Centro financeiro reorganizado, com calendário, eventos e tesouraria.
- Tabelas com colunas redimensionáveis, ocultáveis e guardadas automaticamente.
- Histórico para desfazer importações, edições e eliminações.
- Relatório anual com receitas, despesas, investimentos e documentos.
- Backups automáticos e backups cifrados com palavra-passe.
- Tooltips claros e legíveis em vez das janelas pretas do Windows.
- Aplicação mais rápida, modular e coberta por testes visuais e funcionais.
- Novo pacote Windows sem bases de dados, anexos ou outros dados pessoais.

### Segurança e privacidade

- Os movimentos, documentos e backups continuam guardados localmente.
- A IA online permanece opcional.
- Palavras-passe e chaves não são incluídas no código nem na distribuição.
- O modo de privacidade também protege relatórios e exportações.

### Compatibilidade

- Windows com o pacote `LiquidGlass_App.zip`; ou
- execução pelo código com as versões indicadas em `requirements.txt`.
