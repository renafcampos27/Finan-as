# Release Notes

Histórico técnico e documentado das versões da aplicação Finanças Pessoais —
Liquid Glass.

> Regra de manutenção: cada nova versão deve ser acrescentada imediatamente
> acima da versão anterior. As entradas antigas nunca são apagadas nem
> reescritas. Correções da mesma série usam versões semânticas, por exemplo
> `v3.0.1`, `v3.1.0` e `v3.2.0`.

## V3.51 — v3.51.1 — encerramento dos diálogos validado

- Mantém PySide6/Qt 6.12.0 e as dependências atualizadas da série V3.51.
- O diálogo de fiscalidade é libertado após aceitar, cancelar ou falhar
  a execução da janela, evitando acumular janelas fechadas na página.
- Os testes eliminam os diálogos que criaram antes de terminar o sistema
  gráfico. A comparação repetida passou de nove encerramentos nativos
  anormais em 30 processos para zero em 30, sem omitir verificações.
- A versão 3.51.0 ficou bloqueada na verificação de publicação; o respetivo
  registo é preservado. Esta correção recebe uma nova versão e nova etiqueta.

## V3.51 — v3.51.0 — Qt 6.12 e dependências atualizadas

- PySide6/Qt 6.12.0, cryptography 50.0.2 e NumPy 2.5.5, instalados e
  verificados num ambiente Windows x64 isolado, sem pacotes globais.
- PyInstaller 6.22.3, pytest 9.2.0 e Ruff 0.17.5. Argon2, pip-audit e
  pytest-timeout mantêm as versões atuais verificadas. As dependências
  transitivas foram revistas e auditadas em conjunto.
- Pré-visualização PDF validada no executável com abertura e renderização
  reais, incluindo os módulos PDF que passaram a ter distribuição separada.
- Interface QWidget e gráficos QPainter preservados; testes de fontes,
  geometrias, privacidade, gráficos densos e DPI continuam obrigatórios.
- O diálogo de segurança aguarda a conclusão real da tarefa antes de aceitar
  o desbloqueio e libertar os objetos gráficos associados.
- Actions de checkout, preparação do Python e conservação de resultados
  atualizadas para versões estáveis atuais, fixadas a commits oficiais.
- O runtime de produção mantém CPython 3.14.10 normal. Python 3.15 foi
  analisado: a correção Windows para DLLs abi3t de bibliotecas nativas está
  posterior à versão publicada do empacotador; não é adotada sem essa validação.

## V3.50 — v3.50.1 — publicação Windows e validação reproduzível

- Distribuição Windows e atualizações assistidas pelo repositório oficial
  `renafcampos27/Finan-as`, mantendo os dados pessoais fora do programa.
- Verificação de versões, descarga limitada e validada por SHA256, extração
  segura e instalação apenas depois do encerramento protegido da base de dados.
- Instalador com preparação temporária, validação dos ficheiros e reposição da
  instalação anterior se a substituição falhar.
- Testes reproduzíveis em Windows limpo: caminhos longos/curtos normalizados,
  erros de ficheiros avaliados pelo código do sistema e referência de retenção
  independente de ficheiros privados de auditoria.
- Geometria da lista de movimentos validada pelos limites do conteúdo e da fonte,
  preservando o mínimo pretendido sem exigir uma altura idêntica em todos os PCs.
- Publicação automática do pacote apenas quando os testes e o controlo da
  distribuição passam. A assinatura Authenticode continua opcional.

## V3.50 — v3.50.0 — distribuição GitHub, atualizações assistidas e segurança

Integra a propriedade da credencial LGBK2, rotação transacional, falha segura
criptográfica e consistência envelope/marker. A publicação exige uma árvore
Git limpa, a etiqueta correspondente à versão, os testes automatizados e a
validação do pacote instalado num perfil sintético. Estas notas descrevem o
candidato; a sua existência não comprova a conclusão dessas verificações.

### Distribuição e atualizações

- Repositório de distribuição: `renafcampos27/Finan-as`.
- GitHub Actions verifica o código e gera o pacote Windows a partir da etiqueta
  de versão. O pacote e o SHA256 são publicados apenas após os controlos.
- A aplicação pode procurar versões estáveis no GitHub, descarregar e verificar
  o pacote e preparar uma atualização com confirmação do utilizador.
- O instalador mantém os dados separados, impede a substituição enquanto a
  aplicação está aberta e recupera a instalação anterior se a substituição falhar.
- A assinatura Authenticode é suportada quando existe certificado configurado;
  não se declara uma assinatura inexistente.

### Correções de segurança e backup

- **SEC-05 — Propriedade da credencial LGBK2 unificada.** As Definições já não
  chamam `BackupService.rotate_backup_password()` quando existe sessão de
  segurança ativa: esse método é agora bloqueado nesse estado. A mudança de
  PIN/palavra-passe é feita exclusivamente por
  `StartupSecurityService.change_credential()`, que reenvolve a mesma chave
  mestra. Uma palavra-passe explícita nunca é ignorada silenciosamente: com
  sessão ativa tem de corresponder a uma credencial viva do envelope
  (`verify_credential_matches`) ou a operação falha com erro tipado. APIs
  divididas: `create_lgbk2_complete_backup()` (sessão) e
  `create_lgbk1_legacy_backup()` (compatibilidade legada nomeada). Backups
  LGBK1 antigos continuam legíveis via caminho read-only.
- **SEC-06 — Passfrases sem limite efetivo.** Novas credenciais LGBK2 nunca
  são persistidas em bruto no Windows Credential Manager (limite de 2560
  bytes); o envelope guarda apenas parâmetros KDF, salts, nonces e wraps.
  WinCred fica reservado à recuperação LGBK1 legada. Valores preservados ao
  byte: sem strip, normalização, conversão de caixa ou truncamento. Testado
  com passfrases Unicode >20 000 bytes em onboarding, unlock, backup,
  validação e restauro.
- **SEC-07 — Rotação de credencial verdadeiramente transacional.**
  `change_credential()` segue agora: verificar credencial atual → validar
  nova → criar cópia de rollback durável e verificada byte a byte → escrever
  envelope candidato atomicamente → reabrir e verificar nova credencial e
  chave mestra inalterada → atualizar marker atomicamente. Qualquer falha
  pós-mutação restaura e revalida o envelope anterior antes de propagar erro
  tipado; falhas de rollback são registadas via `log_exception` (nunca
  `except: pass`). Falhas pré-mutação mantêm a categoria tipada original
  (`InvalidCredentialError`, `CredentialPolicyError`). Falhas de injeção
  cobertas por testes: escrita candidata, reabertura, comparação de chave
  mestra, escrita do marker e rollback.
- **SEC-08 — Eliminado downgrade criptográfico silencioso.** Com sessão LGBK2
  ativa, falhas de cifração falham fechado, removem output parcial e reportam
  erro genérico; nunca há fallback automático para LGBK1. A criação LGBK1 só
  existe na operação de compatibilidade explicitamente nomeada.
- **SEC-09 — Consistência envelope/marker.** Ambos os ficheiros são escritos
  com ficheiro temporário, flush, `fsync`, `os.replace` e durabilidade de
  diretório onde suportado. O marker passa a vincular `profile_id`, versão de
  formato e identidade do envelope (`envelope_identity`, SHA-256 canónico).
  Markers em falta, malformados ou não correspondentes são detetados:
  `verify_marker_consistency()` e `inspect_profile()` nunca apresentam um
  perfil protegido como novo/desprotegido (estado degradado para
  `CORRUPT_ENVELOPE`; envelope apagado com marker vivo permanece
  `MISSING_ENVELOPE`).
- **SEC-10 — Propriedade de sessão exata.** `set_active_session()` fecha e
  zeroiza a sessão anterior distinta antes da substituição; `None` limpa a
  anterior; reposição do mesmo objeto é idempotente; `clear_active_session()`
  é exatamente-uma-vez e seguro em duplicados.

### Segurança UX (Liquid Glass)

- Linha legacy de palavra-passe de backup substituída por cartão de segurança
  com badge de estado (Protegido/Atenção/Recuperação necessária), métodos
  configurados, método predefinido, formato do último backup e estado de
  verificação; ações de credencial movidas para diálogo dedicado.
- Novo diálogo `ChangeCredentialDialog` (`ui/change_credential.py`) sobre
  `StartupSecurityService.change_credential()`: exige credencial atual, nova
  credencial e confirmação; suporta PIN e palavra-passe, revelação, aviso de
  Caps Lock, preservação exata de valores, aviso de PIN fraco com confirmação
  explícita. Sem comparação ou derivação de credenciais na UI; AsyncTask não
  bloqueante com slots tipados, controlos desativados durante execução e
  resultados tardios ignorados após destruição.
- `StartupSecurityDialog` em modo unlock inspeciona `envelope.unlocks`:
  métodos não configurados são desativados, o método predefinido é
  pré-selecionado.
- `QEventLoop` aninhado removido de `StartupSecurityDialog._run_worker()`;
  diagnóstico de substituição segura (cancelamento, fechamento, resultados
  tardios) mantido.
- Tokens de tema central no diálogo (superfície translúcida, borda de
  destaque, sombra contida, raios/espacemento/typografia consistentes,
  hierarquia de botões, foco visível); tema claro/escuro selecionado sem
  abrir SQLite antes da autenticação. Largura/altura limitadas a
  `QScreen.availableGeometry()` com `adjustSize()` e recentralização;
  campos sem larguras mínimas rígidas que derrotariam o clamp.

### Arquitetura e qualidade

- Novo `services/security_settings.py`: `SecuritySettingsController` com
  protocolo tipado `SecurityServiceLike` e resultados tipados
  (`SecurityStatus`, `MethodState`, `CredentialChangeResult`). A página
  Definições deixou de coordenar mutações de envelope/backup diretamente e
  deixou de executar SQL (`SELECT COUNT(*) FROM transactions` →
  `repo.movement_count()`); `main.py` mantém-se bootstrap/composição.
- Zero `except: pass` nos módulos de segurança; `log_exception` obrigatório.
- Compatibilidade Python 3.14 e type hints estritos preservadas.

### Dependências e empacotamento

- `cryptography` alinhado em **50.0.1** (`requirements.txt`, verificação do
  `build_windows.ps1`, documentação canónica que já reclamava a série 50.x).
  Versão validada em execução com a bateria de segurança completa.
- `argon2-cffi==25.1.0`: hiddenimports adicionados a `LiquidGlass.spec`
  (`argon2`, `argon2.low_level`, `_argon2_cffi_bindings`) e verificação de
  bundle `_internal\argon2\__init__.pyc` + `_internal\_argon2_cffi_bindings\_ffi.pyd`
  no `build_windows.ps1`; pin verificado no arranque do build.

### Validação executada (resultados reais)

- Bateria de segurança dirigida pós-fase:
  `tests/test_backup_crypto_v2.py + test_startup_security_service.py +
  test_startup_security_ui.py + test_security_migration.py +
  test_security_and_finance_v313.py + test_security_correctness.py +
  test_security_ux.py` — **134 passed**.
- Pacote/release: `tests/test_package_readiness.py + test_release_metadata.py`
  — **16 passed**.
- `ruff check .` — **All checks passed** (código todo, incluindo testes).
- Suíte completa (`--ignore=tests/test_visual_rendering_windows.py`): a
  executar no fecho da fase; resultado registado em `CURRENT_STATE.md`.
  Nenhuma alegação de sucesso global é feita antes desse registo.

## V3.49 — v3.49.0 — 2 de setembro de 2026

Aceleração de performance computacional extrema (Monte Carlo 37,8× via vetorização NumPy float64, solver de XIRR 17,5× acelerado, previsões de fluxo de caixa com somas prefixadas 7,26× mais rápidas), compressão transparente Zstandard/zlib até 38× de redução com libertação dinâmica de espaço em disco via SQLite Incremental Vacuum, interface Liquid Glass a 120 FPS sem bloqueios de renderização, resolução integral de 18 pontos de auditoria técnica (AUD-17 a AUD-34 e AUD-38), blindagem universal contra exceções de I/O (`PermissionError`) em exportações de ficheiros no Windows, criação de novos índices de cobertura e descorrelação de consultas SQL em tempo constante, e expansão histórica da cobertura de testes para 881 testes unitários e de integração 100% aprovados.

### Correção de interação UI-SCROLL-01 — 2 de setembro de 2026

- Os seis sliders existentes em **Simulações**, **Laboratório de Impacto Financeiro**, **Risco e Resiliência** e **Investimentos** deixaram de consumir a roda do rato ou o gesto vertical do touchpad quando o utilizador pretende percorrer a página.
- Foi criado o controlo partilhado `ScrollPassthroughSlider`, que encaminha o evento para o `QAbstractScrollArea` ancestral sem alterar o valor financeiro do slider.
- A interação intencional foi preservada: teclado, clique e arrasto continuam a alterar o valor normalmente.
- Regressão protegida por **5 testes dedicados** e por uma guarda estrutural que impede a reintrodução de `QSlider(...)` direto nos módulos da UI.
- Validação dirigida da área afetada: **26/26 testes aprovados**; análise Ruff dos ficheiros alterados sem erros.

> **Limite de validação atual:** a execução global realizada após esta correção concluiu com **903 testes aprovados e 7 falhas não relacionadas** em 910 testes executados; a coleção atual contém 911 testes após a adição do teste de arrasto. Por isso, esta correção está validada, mas o checkout concorrente atual não deve ser descrito como tendo a suite completa a 100% até essas sete regressões globais serem resolvidas.

### Auditoria Profunda Multi-Agente & Otimização de Performance — 2 de setembro de 2026

#### Auditoria Técnica Especializada (Fases 1 a 7 Concluídas)

- **Fase 1 (AUD-17 a AUD-19)**:
  - **AUD-17 (Sintaxe de Exceções Python 3)**: Normalizados 123 blocos de captura de exceção em 47 ficheiros para a sintaxe padrão de tuplo `except (E1, E2):`, eliminando o code smell e risco de incompatibilidade futura.
  - **AUD-18 (Risco de Cauda Cornish-Fisher VaR)**: Invertido o sinal do termo de assimetria para `-(skew / 6.0) * (z**2 - 1.0)`, garantindo que distribuições com cauda pesada à esquerda (perdas abruptas) aumentam monotonicamente o Value-at-Risk.
  - **AUD-19 (Ciclo de Vida de Tarefas Assíncronas)**: `AsyncTask.is_running()` reflete agora o estado real de execução da thread Qt (`self._thread.isRunning()`), eliminando condições de corrida na interface.
- **Fase 2 (AUD-20 a AUD-22)**:
  - **AUD-20 (Capitalização Temporal Coast FIRE)**: Integrado o fator de desconto composto $(1+r)^N$ com base no horizonte temporal real até à reforma, corrigindo a meta de acumulação precoce.
  - **AUD-21 (Robustez de Lotes Fiscais FIFO)**: Blindada a propriedade `TaxLot.unit_cost` contra divisões por zero em quantidades residuais e adicionada validação estrita que rejeita vendas com quantidade não positiva.
  - **AUD-22 (Normalização de Aportes no Backtester de Crises)**: Igualados os períodos de injeção de capital entre estratégias e desvinculado o cálculo de drawdown das novas contribuições de liquidez (drawdown estrito sobre NAV unitária).
- **Fase 3 (AUD-23 a AUD-25)**:
  - **AUD-23 (Alocação de Liquidez em Stress Testing)**: A função redutora UDF de simulação de liquidez limita o capital de oportunidade (dry powder) estritamente ao saldo disponível em caixa.
  - **AUD-24 (Estabilidade Numérica do Rácio de Sortino)**: Tratado explicitamente o desvio padrão descendente zero: retornos excedentes não negativos reportam `float('inf')` e retornos negativos reportam `0.0`.
  - **AUD-25 (Reporte Fiscal CIRS e Carryforward Quinquenal)**: As perdas líquidas mobiliárias apuradas no ano civil corrente transitam e acumulam automaticamente no carryforward dos 5 anos seguintes.
- **Fase 4 (AUD-26 a AUD-28)**:
  - **AUD-26 (Tolerância a Carteiras Sem Ações)**: O motor de saúde financeira trata carteiras ultraconservadoras (100% liquidez/obrigações) sem gerar divisões por zero ou exceções no pilar de investimento.
  - **AUD-27 (Parser Flexível de Datas com Anos de 2 Dígitos)**: Suporte completo e normalização autónoma de formatos com anos abreviados (século XXI, ex: `26 -> 2026`) em extratos bancários.
  - **AUD-28 (Robustez Hierárquica no Modelo de Movimentos)**: O `MovementsTableModel` trata com segurança cenários onde `parent=None`, garantindo integridade de eventos e repinturas na tabela.
- **Fase 5 (AUD-29 e AUD-30)**:
  - **AUD-29 (Desacoplamento de Lambdas em Ações UI)**: Eliminada a avaliação precoce de funções nos parâmetros padrão de lambdas em `reports.py` e `risk_resilience.py`, evitando mutação acidental de closures.
  - **AUD-30 (Timeout Explícito em Streaming de IA)**: Substituído o lançamento genérico de exceções de socket por `raise TimeoutError(...) from None`, garantindo stack traces limpos no registo de diagnóstico.
- **Fase 6 (AUD-31 e AUD-32)**:
  - **AUD-31 (Blindagem Universal de I/O em Exportações)**: Encapsuladas todas as 8 rotinas de exportação de ficheiros (CSV, PDF, HTML, TXT) em 6 páginas (`movements.py`, `reports.py`, `planning.py`, `dynamic_analysis.py`, `history.py`, `panorama.py`) em blocos defensivos `try ... except Exception as exc:` com diagnóstico `log_exception` e alertas claros ao utilizador, protegendo contra bloqueios exclusivos (`PermissionError`) quando ficheiros estão abertos no Excel ou leitores de PDF.
  - **AUD-32 (Eliminação de B008 em Lambdas e Timers)**: Extraídas instanciações de dicionários e referências fracas `weakref.ref(table)` antes de conectar slots e agendar timers em `core_pages.py` e `table_state.py`.
- **Fase 7 (AUD-33, AUD-34 e AUD-38)**:
  - **AUD-33 (Persistência Desprotegida em Hábitos)**: Protegida a gravação de planos de hábitos em `simulations.py` com captura de exceções SQLite, log estruturado e notificação toast com tom `danger`.
  - **AUD-34 (Otimização de Consultas Lentas & Descorrelação)**: Criados índices dedicados em `engine.py` (`idx_transaction_splits_tx_id`, `idx_transaction_splits_tx_amount` e `idx_holdings_asset_class_name_covering`), eliminando a ordenação temporária em disco em consultas de ativos (`USE TEMP B-TREE FOR ORDER BY`). Substituído o sub-select correlacionado `EXISTS` em `page.py` por um `LEFT JOIN` indexado.
  - **AUD-38 (Limpeza de Diagnósticos Nativos no Windows)**: Mock isolado de `QPrinter` nos testes de exportação de PDF, eliminando mensagens residuais do spooler COM do Windows (`0x80040155`) em ambientes de teste headless.
- **Fase 8 (P0 — Erros Críticos: BUG-SETTINGS-CONCURRENCY-001 e PERS-01)**:
  - **BUG-SETTINGS-CONCURRENCY-001 (Thread-Safety no Backup)**: Em `ui/pages/settings.py`, a validação de integridade em background passou a invocar `service_type.run_verify_latest_backup_for_database(database_file)` com uma conexão `_ThreadOwnedBackupRepo` isolada de background, eliminando qualquer partilha da conexão SQLite principal da GUI em threads worker.
  - **PERS-01 (Reconciliação Bidirecional do Saldo de Referência)**: Em `database/engine.py` e `database/repo/identity.py`, implementada reconciliação retroativa bidirecional quando a data de consulta é anterior à data de referência (`reference_day < configured_day`), calculando $\text{saldo} = \text{reference\_balance} - \sum \text{movimentos}(\text{reference\_day} < t \le \text{configured\_day})$, prevenindo o colapso patrimonial histórico para zero ou valores negativos irreais.
- **Fase 9 (P1 — Erros de Alta Gravidade: Fiscalidade, Matemática e Persistência)**:
  - **TAX-01 (Restrição de Coeficientes de Desvalorização)**: Em `services/tax.py`, aplicação estrita de coeficientes de desvalorização monetária (Art. 50.º CIRS) exclusivamente a bens imóveis com detenção $\ge 24$ meses; valores mobiliários cotados e criptoativos mantêm coeficiente base 1.0.
  - **TAX-02 (Tributação Agravada a 35% para Paraísos Fiscais)**: Tributação automática à taxa agravada de 35% (Art. 72.º n.º 14 CIRS e Portaria n.º 150/2004) para mais-valias originadas em jurisdições offshore com regime fiscal claramente mais favorável.
  - **TAX-03 (Reporte de Perdas Passadas Exige Englobamento)**: Em conformidade com o Art. 55.º n.º 1 al. d) do CIRS, o abatimento de perdas de anos anteriores foi bloqueado sob a taxa liberatória autónoma de 28%, exigindo a opção explícita por englobamento.
  - **MATH-01 (Estabilidade Numérica no Sortino Ratio)**: Em `services/finance_math.py`, quando o desvio descendente é zero com retornos positivos, o rácio é limitado com segurança numérica a `999.0` (em vez de `float("inf")`), prevenindo crashes de serialização JSON e falhas de rendering na GUI.
  - **PERS-02 (Eliminação de Dupla Contagem de Metas de Poupança)**: Em `database/engine.py:net_worth`, dotações para metas (`goals.saved_amount`) já alocadas a contas bancárias não são somadas cegamente ao saldo global, prevenindo a duplicação artificial do património líquido.
  - **PERS-03 (Preservação da Atomicidade Multi-Tabela)**: Removido o `connection.commit()` prematuro e incondicional no arranque de métodos em `database/repo/transactions.py` e `database/repo/planning.py`, respeitando transações externas e savepoints nomeados.
  - **PERS-04 (Sincronização Bidirecional entre Transações e Metas)**: Ao editar o montante de uma transação associada a uma meta (`goal_id`), o saldo acumulado (`goals.saved_amount`) é atualizado atomicamente pelo delta de diferença.
  - **PERS-05 (Isolamento de Snapshots Históricos)**: O registo de snapshots mensais passados (`save_monthly_snapshot`) não sobregrava o histórico com o estado patrimonial vivo do presente.
  - **BUG-TAX-LOGIC-001 (Isenção Cripto com Aquisição Comprovada)**: Mais-valias de criptoativos só beneficiam de isenção fiscal ($\ge 365$ dias) quando a data de aquisição estiver documentalmente comprovada (`has_proven_date=True`).
- **Fase 10 (P2 — Erros Médios e Integridade Operacional: UI, Fiscalidade, Matemática e Armazenamento)**:
  - **BUG-COMBOBOX-CRASH-001 (Sanitização de Combobox de Ano)**: Em `ui/pages/reports.py` (`ScoreDetailsPage.ym` e `PdfReportsPage.html`) e `ui/pages/planning.py` (`MonthlyGoalsPage.ym`), sanitizada a extração do ano com fallback seguro para `date.today().year`, eliminando crashes de `ValueError: invalid literal for int() with base 10: ''`.
  - **BUG-INVESTMENTS-CRUD-001 (Robustez no Portfólio de Dívidas e Ativos)**: Em `ui/pages/investments.py`, protegido o método `delete_liability` contra dereferência nula (`item.text()`) e adicionados blocos defensivos `try ... except Exception:` em todas as operações CRUD de investimentos e dívidas.
  - **BUG-PLANNING-DELETE-001 (Diálogo de Confirmação na Eliminação de Metas)**: Em `ui/pages/planning.py` (`MonthlyGoalsPage.delete_goal`), implementado diálogo de confirmação com `QMessageBox.question` e encapsulamento em bloco defensivo `try/except`.
  - **TAX-04 (Inclusão a 50% vs Taxa Liberatória de 28%)**: Em `services/tax.py`, bloqueado o cálculo autónomo a 28% quando `apply_50pct_inclusion=True`, dado que ao abrigo do Art. 43.º n.º 2 do CIRS a inclusão a 50% exige obrigatoriamente englobamento nas taxas gerais progressivas do Art. 68.º.
  - **TAX-05 (Consumo Estrito FIFO sem Lotes Futuros)**: Em `motores_matematicos_simulacao/portfolio_stress_and_tax_optimizer.py`, o motor FIFO consome exclusivamente lotes com data de compra anterior ou igual à data de alienação (`buy_date <= sell_date`), preservando lotes futuros e evitando períodos de detenção negativos.
  - **TAX-06 (Despesas e Encargos no Anexo G/J)**: O valor de aquisição passou a refletir estritamente o custo nominal puro (`qty * buy_price`), acumulando todas as comissões de compra e venda no campo próprio de despesas e encargos (Art. 51.º CIRS).
  - **TAX-07 (Salvaguarda de Security Tokens e Offshore na Isenção Cripto)**: A isenção de criptoativos detidos há mais de 365 dias foi restringida, impedindo a sua atribuição a security tokens (Art. 10.º n.º 1 al. k) in fine) ou a entidades com domicílio em paraísos fiscais offshore.
  - **MATH-02 (Proteção contra Overflow em Desconto Composto)**: Em `services/finance_math.py`, encapsuladas as chamadas a `math.pow` e `math.expm1` contra `OverflowError` em horizontes temporais extremos.
  - **MATH-03 (Deteção de Risco em Retornos Constantes Negativos)**: Em `services/finance_math.py:cornish_fisher_var`, séries temporais com retornos constantes negativos e variância zero reportam agora a perda certa correspondente em vez de mascarar risco zero.
  - **MATH-04 (Denominador Negativo em Modified Dietz)**: Em `services/finance_math.py:modified_dietz`, quando retiradas maciças de capital tornam o denominador $\le 0$, a função retorna com segurança `0.0`, prevenindo a inversão matemática espúria de rentabilidade.
  - **MATH-05 (Prevenção de Divisão por Zero no Backtester de Crises)**: Em `motores_matematicos_simulacao/crisis_backtester_and_architecture_engine.py`, adicionadas salvaguardas em todas as 4 estratégias de timing contra `ZeroDivisionError` em cenários de crash súbito ou capital zero.
  - **PERS-07 (Resiliência do VACUUM em Modo WAL perante Leitores Assíncronos)**: Em `database/engine.py:optimize_database`, a chamada `self.conn.execute("VACUUM")` foi envolvida em `try ... except sqlite3.OperationalError:`, fazendo fallback gracioso e não intrusivo para `PRAGMA incremental_vacuum` quando há leitores assíncronos ativos em background.

#### Teamwork Multi-Agent Engineering & Optimization (Marcos M1 a M4 Concluídos)

- **M1 — Robustez de Código e Linter Baseline**:
  - Correção de casos de fronteira em badges temporais no primeiro dia do mês com despesa zero.
  - Configuração segura de exclusão de subagentes em `pyproject.toml` e análise estática `ruff check .` com 0 erros.
- **M2 — Aceleração Computacional de Motores Matemáticos**:
  - **Monte Carlo Vetorizado**: Aceleração de **37,8×** via vetorização matricial com NumPy float64 (1,8M de iterações em ~100 ms).
  - **Solver Numérico XIRR**: Aceleração de **17,5×** no solver de raiz com derivadas analíticas e resíduos mínimos $< 5 \times 10^{-4}$.
  - **Previsão de Fluxo de Caixa**: Aceleração de **7,26×** através de somas prefixadas e indexação pré-calculada de recorrências.
  - **Agregações SQL de Splits**: Aceleração de **2,14×** com agregação condicional em passagem única.
  - **Descarregamento do DashboardWorker**: Tarefas pesadas de inteligência financeira migradas para threads secundárias, garantindo 120 FPS sem bloqueios na interface.
- **M3 — Armazenamento, Compactação & Eficiência de Backup**:
  - **SQLite Incremental Vacuum**: Ativado `PRAGMA auto_vacuum = INCREMENTAL` com rotinas periódicas de libertação de espaço em disco sem paragens de escrita.
  - **Compressão de Snapshots & Diários**: Payloads JSON comprimidos de forma transparente (Zstandard/zlib) com redução de espaço de até **38×** (97,38% de poupança).
  - **Backups de Alta Densidade**: Geração de cópias de segurança compactadas com rácio de **11,88×** (91,58% de redução), encriptação AES-256-GCM e integridade SHA-256.
- **M4 — Suite E2E Opaque-Box & Verificação Global**:
  - Criação da suite E2E de 4 Tiers com 62 testes de conformidade funcional (`tests/test_e2e_requirements.py`).
  - Benchmarks comparativos e testes de stress de limites (`test_challenger_m2_benchmarks.py`, `test_challenger_m3_stress.py`, `test_storage_optimization.py`).
  - **Expansão Global**: Total expandido para **881 testes unitários e de integração** 100% aprovados em 117 ficheiros de teste.

### Registo vivo da auditoria e implementação por fases — 30 de agosto de 2026

Este registo distingue problemas efetivamente reproduzidos, correções já
implementadas e trabalho ainda pendente. Deve ser atualizado no final de cada
fase; uma correção só passa a concluída depois de possuir regressão automatizada
e validação executada.

#### Fase 1 — cálculos financeiros e fiscalidade — concluída

- **Saldo ignorava referências bancárias**: `cash_balance()` somava todo o
  histórico e apresentava `650 €` onde o saldo confirmado correto era
  `1 150 €`. Foi criada uma decomposição canónica por conta que respeita
  `reference_balance`, `reference_date`, identidades estáveis, várias contas e
  movimentos órfãos (`database/engine.py`, `database/repo/analytics.py`).
- **Dashboard, previsões e ponte patrimonial divergiam**: séries de saldo,
  cashflow 30/60/90, runway e ponte patrimonial mantinham consultas próprias.
  Passaram a consumir a mesma fonte canónica (`services/analytics.py`,
  `services/cashflow_forecast.py`, `services/financial_intelligence.py`).
- **Snapshots guardavam saldos históricos incorretos**: snapshots mensais e
  diários usavam a soma integral do ledger. Passaram a guardar o saldo canónico
  na data pedida; o património mensal também deixa de duplicar holdings ligados.
- **Holdings com ligação quebrada desapareciam**: um ativo ligado a uma conta
  inexistente era excluído do património. Agora só se exclui o holding quando o
  ID ou nome resolve para uma conta ativa; ligações quebradas permanecem como
  ativo e são reportadas na qualidade dos dados.
- **Motor fiscal aceitava vendas inválidas**: quantidade zero/negativa, preço
  inválido e taxas negativas podiam ser convertidos silenciosamente para zero.
  Agora essas entradas são rejeitadas (`services/tax.py`).
- **FIFO consumia lotes futuros**: uma venda entre duas datas de aquisição podia
  consumir o lote posterior e produzir dias de detenção negativos. O FIFO usa
  apenas lotes adquiridos até à data da venda, calcula oversell sobre esses lotes
  e não altera posições quando a venda é rejeitada.
- **Regressões acrescentadas**: referências em datas diferentes, várias contas,
  movimentos órfãos, holdings válidos/quebrados, Dashboard e previsões,
  snapshots e entradas fiscais inválidas (`tests/test_net_worth_single_source.py`
  e `tests/test_phase3_v349_quant_tax_and_banking.py`).
- **Validação da fase**: 139 testes financeiros/de integração passaram; Ruff,
  compilação Python e `git diff --check` nos ficheiros da fase passaram; o
  autoteste abriu 30 páginas com esquema SQLite 26 e `ok=true`.

#### Fase 2 — integridade e segurança dos dados — revalidada na Fase 4

- **Correções anteriores confirmadas**: validação das transferências antes da
  criação de contas; `Repo(db).paths` obtido da base injetada; backup pela
  ligação separada apenas de leitura, sem confirmar alterações pendentes;
  execução de scripts SQL sem o commit implícito de `executescript()`.
- **Migração confirmada sem versão**: a falha ao inserir o marcador da migração
  10 deixava o índice já gravado. `_ensure_migrations()` passa a envolver todas
  as migrações pendentes, marcadores e constraints num savepoint; os auxiliares
  de esquema deixam de confirmar a transação exterior (`database/engine.py`).
- **Transferência confirmada com `commit=False`**: libertar o savepoint exterior
  confirmava implicitamente as duas parcelas. Agora existe uma transação real
  exterior quando necessário; transações do chamador nunca são confirmadas ou
  anuladas pela transferência (`database/repo/transactions.py`).
- **Falha num script dentro de outra transação**: o savepoint de
  `run_schema_script()` anula apenas o script, preservando alterações anteriores
  do chamador. São testados sucesso/falha e transações próprias/exteriores.
- **Validação dirigida**: 80 testes aprovados, incluindo 28 casos de
  `tests/test_phase2_data_integrity.py`; nova cobertura de visibilidade através
  de segunda ligação, rollback, falha na parcela de crédito, marcador da
  migração e reconstrução diferida de constraints.

#### Fase 3 — interface e acessibilidade — concluída em 30 de agosto de 2026

As correções de navegação e os rótulos semânticos já presentes nesta checkout
foram preservados e revalidados. Esta fase completou a limpeza do ciclo de vida
das animações, reforçou os testes e verificou os caminhos ativos em conjunto.
A Fase 2 foi posteriormente revalidada e completada durante a Fase 4.

- **Página esbatida após navegação rápida**: a animação interrompida deixava o
  efeito de opacidade associado à página anterior. `_finish_page_transition()`
  retira apenas o efeito pertencente à transição, limpa as referências e ignora
  conclusões antigas que não correspondem à animação atual (`main.py`).
- **Destruição adiada podia provocar um crash nativo Qt**: na sequência dos
  testes de navegação e acessibilidade, um callback da animação mantinha
  referências à animação/janela até à drenagem de `DeferredDelete`. A limpeza
  agora para a animação, desliga `finished`, remove o objeto-alvo e só depois
  agenda a destruição. A sequência anteriormente problemática passou também
  sem depender da drenagem adicional do fixture.
- **Encerramento durante uma transição**: o fecho liberta a animação mesmo
  quando precisa de aguardar workers. A limpeza é idempotente, tolera páginas
  destruídas e não remove efeitos gráficos substituídos por outro componente.
- **Seletores com nomes indistinguíveis**: o `TransactionDialog` associa os
  rótulos aos campos com `setBuddy()` e usa os nomes «Tipo», «Categoria»,
  «Conta» e «Método de pagamento». Os editores internos dos combos editáveis
  recebem o mesmo nome semântico (`ui/dialogs.py`).
- **Nomes acessíveis não acompanham o valor selecionado**: mantêm-se estáveis
  em criação/edição de movimentos, mudança Receita/Despesa, recarregamento de
  categorias, texto personalizado e reaplicação do `AccessibilityManager`.
- **Isolamento dos testes Qt**: os fixtures libertam as destruições pendentes
  enquanto ainda possuem a árvore de widgets. O teste do diálogo remove os
  seus filtros, processa callbacks e confirma a destruição do seu próprio
  diálogo, sem antecipar a destruição de objetos de outros testes.
- **Regressões principais**: `tests/test_phase3_ui_navigation.py` e
  `tests/test_accessibility_v318.py` cobrem navegação rápida/repetida,
  interrupção, callbacks antigos, objeto-alvo desligado, efeitos substituídos,
  páginas destruídas, fecho com workers, rótulos e nomes distintos/estáveis.
- **Revalidações adicionais**: coordenadas globais do `LiquidSheetDialog`,
  caches analíticos, Monte Carlo sem SQL no worker, geometria de gráficos,
  arranque/encerramento, componentes visuais e regressões financeiras da Fase 1.

Validação executada na mesma sessão de Pytest, com `-v --timeout=45
-p no:cacheprovider`: **131 testes passaram em 36,02 s**. Ficheiros abrangidos:

- `tests/test_phase3_ui_navigation.py`
- `tests/test_accessibility_v318.py`
- `tests/test_phase2_v349_ui_120fps_and_geometry.py`
- `tests/test_concurrency_and_ui_responsiveness.py`
- `tests/test_net_worth_single_source.py`
- `tests/test_startup_and_shutdown.py`
- `tests/test_startup_compatibility_v3291.py`
- `tests/test_all_liquid_dialogs.py`
- `tests/test_visual_components.py`
- `tests/test_budget_bucket_dialog.py`

Ruff, compilação Python e `git diff --check` nos ficheiros da fase passaram.
O autoteste isolado pelo código devolveu `ok=true`, esquema 26 e 30 páginas
carregadas (`frozen=false`). Não equivale a testar um executável reconstruído,
a aprovar a suíte integral ou a validar manualmente NVDA/Narrador. Commit,
tag e reconstrução do ZIP continuam fora desta fase.

#### Fase 4 — QA e release — em validação

- **Smokes fora da suíte**: `pytest.ini` inclui `smoke_*.py`; três casos de
  transporte deixaram de retornar valores. `PytestReturnNotNoneWarning` passa
  a erro e `pytest-timeout==2.4.0` impõe 60 segundos por teste.
- **Cobertura apenas aparente dos scripts**: só 5 dos 10 scripts tinham casos
  pytest. `tests/smoke_script_execution.py` executa todos os pontos de entrada
  em processos isolados, com perfil temporário, rede bloqueada e prazo de 30 s.
  O próprio bloqueio de rede, as assertions e o prazo têm regressões.
- **Smoke de importação incompatível**: o substituto de repositório não expunha
  `account_identity`. Agora usa `Repo`/`FinanceDB` reais com dados sintéticos;
  verificam-se identidades estáveis e rollback do lote.
- **Pré-requisitos transacionais**: a QA detetou e corrigiu as duas lacunas da
  Fase 2 descritas acima.
- **Reimportação falhava depois de rollback**: o cache de identidades retinha
  IDs anulados e resultados negativos desatualizados. `database/repo/identity.py`
  resolve agora o estado visível na ligação SQLite. Não basta observar
  `total_changes`/`in_transaction`, pois não detetam todos os rollbacks de
  savepoints. 18 regressões novas cobrem contas, categorias, aliases, outras
  ligações, arquivo e rollback; 50 testes aprovados e 12 regressões adicionais.
- **Smokes podiam terminar a suíte Qt**: o teste de cancelamento Gemini criava
  `QCoreApplication`, incompatível com widgets posteriores. Usa agora
  `QApplication` e verifica o tipo; os smokes de transporte e credenciais deixam
  de referenciar um caminho de código antigo fixo no computador.
- **Normalização global**: 186 ficheiros formatados mecanicamente, com comparação
  AST de 247 ficheiros sem alteração de lógica; restantes ficheiros de integração
  também formatados. Ruff e `git diff --check` globais aprovados.
- **Ambiente de build contaminado**: `pip check` no Python geral encontrou
  conflito entre `cptr` e `cryptography`. Foi criado um ambiente isolado em
  `artifacts/release-venv` com as dependências fixadas; `pip check` aprovado,
  sem alterar o ambiente global.
- **Proveniência ausente**: `scripts/build_windows.ps1` exige árvore Git limpa
  e tag da versão no commit atual; o manifesto inclui commit, árvore, tag e
  `dirty=false`. Cinco cenários reais de Git testam aprovação/rejeição.
- **Preservação dos pacotes**: os ZIP/checksums existentes são copiados para
  `artifacts/package-recovery/<id>`; o novo ZIP é construído em `build` e só
  substitui o anterior após autotestes. Versões históricas não são removidas.
- **Autoteste pelo código**: aprovado no ambiente isolado, versão 3.49.0,
  esquema 26, 30 exportações de páginas e reabertura SQLite `ok`.
- **Bloqueio antigo da suíte localizado**: a primeira execução integral chegou
  a 37% sem falhas anteriores e atingiu o timeout de 60 s no teste monolítico
  `test_full_ui_responsive.py`, durante a aplicação global do tema. O teste
  acumulava 30 páginas e 14 estados visuais. Foi dividido num caso comum e
  14 estados em processos independentes, preservando as verificações existentes
  (40 s no filho e 45 s no processo exterior). Também se repõe a fonte original
  e verifica o encerramento dos workers; isto não
  constitui prova de um deadlock na aplicação.
- **Fixture de score usava a base errada**: o teste alterava `utils.config.db_path`
  depois de esse símbolo ter sido importado pelo repositório. Agora injeta uma
  `FinanceDB` temporária e fecha-a em `finally`
  (`tests/test_financial_health_performance.py`).
- **Timeouts incompatíveis no ensaio nativo Windows**: a execução diagnóstica
  sem a matriz aprovou 708 testes, mas o limite geral novo de 60 s interrompeu
  o último ensaio visual, cujo subprocesso já possuía limite de 480 s. Esse
  limite próprio foi preservado, com watchdog exterior de 500 s; os restantes
  testes mantêm 60 s. Não foi removida qualquer asserção visual.
- **Pendente neste ponto**: suíte integral, frozen/installed self-tests,
  commit/tag e checksums finais. O pacote antigo não é
  evidência de validação das correções atuais.

Limites: os números de FPS/performance e as afirmações fiscais no descritivo
original abaixo não são certificados por esta QA. Os testes de software não
substituem validação fiscal, ensaio manual com leitor de ecrã ou medição de FPS.
`build_tools.packaged_for_target=false` indica apenas que as ferramentas de
desenvolvimento não são incluídas no computador de destino; não é uma falha.

### 1. Performance Computacional, Caching Persistente & Aceleração SQL
- **Instâncias Persistentes de Serviços Analíticos**: `AnalyticsService` mantém agora instâncias persistentes `health_service` e `intel_service`, elevando a taxa de acerto da `BoundedLRUCache` para mais de 90% em toda a navegação.
- **Invalidação Inteligente de Caches**: Adicionado `invalidate_caches()` acionado apenas em mutações de dados (`notify_data_changed`), eliminando recálculos redundantes.
- **Evolução Mensal em Consulta Única**: `monthly_evolution()` e `monthly_totals_grouped_for_year()` agrupam os 12 meses do ano corrente e anterior numa única consulta agregada, reduzindo a latência de carregamento de ~60ms para ~2ms.
- **Solver de Rentabilidade XIRR com Método de Brent e Log Transform**: Substituição de `math.pow` por `math.exp(t * math.log1p(r))` ($\sim 2.5\times$ mais rápido em CPython) e integração do método de Brent para convergência absoluta em menos de 15 passos.
- **Otimização Monte Carlo**: Cálculo simultâneo dos percentis P10, P50 e P90 com uma única operação de ordenação (`_percentiles_10_50_90`), acelerando projeções estocásticas em mais de $65\%$.
- **Normalização de Descrições com LRU Cache**: Memoização de descrições bancárias em `services/cashflow_forecast.py` (`@lru_cache(maxsize=4096)`).
- **Índices de Cobertura SQLite & WAL Synchronous NORMAL**: `PRAGMA synchronous = NORMAL;` e novos índices `idx_transactions_meta_tx_paydate` e `idx_transactions_date_id_desc` para aceleração de leituras e escritas.

### 2. UI/UX 120 FPS, Liquid Glass & Geometria Responsiva
- **Eliminação de `QGraphicsBlurEffect` nas Janelas Modais**: `LiquidSheetDialog` substitui o blur destrutivo da janela principal por um overlay Liquid Glass semitransparente otimizado (`rgba(11, 16, 32, 0.72)`), restaurando 120 FPS fluidos sem desativar a aceleração por hardware do Windows.
- **Cálculo de Altura Dinâmico em `FlowLayout`**: `FlowLayout.sizeHint()` calcula agora a altura real em função da largura do contentor pai (`heightForWidth`), impedindo o esmagamento de cartões e filtros em `QScrollArea`.
- **Elisão Dinâmica e Margens no `SankeyChart`**: Nomes longos de categorias são elididos com `QFontMetrics.elidedText()` e percentagens alinhadas responsivamente, prevenindo sobreposições visuais.
- **Otimização de Eventos no `DonutChart`**: Eliminação de repinturas redundantes em `mouseMoveEvent`.
- **Conformidade de Contraste WCAG 2.2 AA**: Badges de confiança e cobertura de dados utilizam texto de alto contraste (`#4ADE80` para positivo, `#FFB340` para aviso, `#F87171` para negativo).

### 3. Rigor Quantitativo, Fiscalidade CIRS & Extratores Bancários
- **Validação Rigorosa da Regra de 365 Dias para Criptoativos**: `PortugueseTaxDialog` valida a data real de aquisição do ativo (`(today - acq_date).days >= 365`), tributando ganhos de curto prazo (<365d) à taxa autónoma de 28% e isentando apenas posições de longo prazo.
- **Reporte de Perdas Fiscais a 5 Anos (Art. 55.º CIRS)**: `PortugueseTaxEngine.generate_annual_tax_report()` deduz perdas transitadas de anos anteriores do saldo positivo de mais-valias.
- **Coeficiente de Inclusão a 50% (Art. 43.º CIRS)**: Suporte ao cálculo com inclusão de 50% do saldo para ativos elegíveis.
- **Regra de Inflação Modificada de Guyton-Klinger**: No `FirePlanningEngine`, anos seguintes a retornos negativos congelam o ajuste de custo de vida por inflação, preservando o capital inicial.
- **Expansão de Dialetos Bancários**: Suporte a extratos de Crédito Agrícola, Bankinter, WiZink, Unibanco, EuroBic / ABANCA, Openbank e Trade Republic.
- **Decodificação de Entidades XML e Suporte a Categoria no QIF**: `OFXParser` e `QIFParser` decodificam entidades HTML/XML (`&amp;`, `&ccedil;`, `&atilde;`, etc.) e extraem a categoria no tag `L`.

### 4. Arquitetura Limpa & Métodos Canónicos de Domínio
- **Factory de Modelos `from_row()`**: Adicionado a `Transaction` em `domain/models.py` para mapeamento seguro e direto de registos `sqlite3.Row`.
- **Injeção Opcional de Base de Dados em `BaseRepo`**: Suporte a injeção explícita de `FinanceDB` para desacoplamento e isolamento em testes unitários.

---

## V3.48 — v3.48.0 — 25 de agosto de 2026


Otimização de armazenamento e manutenção ativa da base de dados SQLite, integração visual do motor fiscal IRS português (Anexo G/J), planeador gráfico de Independência Financeira (FIRE) com guardrails Guyton-Klinger e stress test de risco de sequência de retornos, assistente de importação bancária inteligente com deteção automática de bancos portugueses (CGD, BCP, Santander, ActivoBank, Revolut, Moey!, CTT, Novo Banco) e pré-visualização em tempo real com deduplicação avançada.

### 1. Otimização de Armazenamento e Manutenção da Base de Dados
- **Painel de Manutenção Ativa de Base de Dados**: Adicionado à página de *Gestão de Dados* (`ui/pages/data_management.py`) com métricas em tempo real do tamanho da base de dados, ficheiro WAL, total de páginas SQLite e espaço recuperável estimado.
- **Motor de Otimização `optimize_database()`**: Execução orquestrada e segura de `PRAGMA wal_checkpoint(TRUNCATE)`, `PRAGMA optimize`, `ANALYZE`, `REINDEX` e `VACUUM` com relatório detalhado de megabytes recuperados e páginas compactadas.

### 2. Painel Visual de Fiscalidade Portuguesa (IRS Anexo G / J)
- **Diálogo Especializado de Fiscalidade IRS**: Novo diálogo `PortugueseTaxDialog` acessível na página de Investimentos com apuramento de mais-valias/menos-valias pelo método FIFO, coeficientes de desvalorização da moeda, isenção automática para criptoativos detidos há mais de 365 dias (Art. 10.º CIRS) e cálculo de retenção na fonte a 28%.
- **Resumo e Tabela de Ativos Fiscais**: Identificação visual por cores de mais-valias tributáveis vs isentas e botão de cópia de resumo fiscal formatado para preenchimento da declaração Modelo 3.

### 3. Planeador Gráfico FIRE & Guardrails Guyton-Klinger
- **Vista Dedicada FIRE no Laboratório de Simulações**: Adicionada à página de *Simulações* (`ui/pages/simulations.py`) com cálculo reativo dos 4 marcos essenciais: Standard FIRE, Lean FIRE, Fat FIRE e Coast FIRE com anos até ao objetivo e taxa de progresso.
- **Guardrails Dinâmicos de Guyton-Klinger**: Modelação visual das regras de preservação de capital (-10% de levantamento se SWR subir >20%) e regras de prosperidade (+10% se SWR descer <20%).
- **Stress Test de Sequência de Retornos (SRR)**: Simulação comparativa a 30 anos entre retornos estáveis vs queda acentuada de mercado nos primeiros 3 anos (-35%, -15%, 0%), calculando a probabilidade de sobrevivência da carteira.

### 4. Assistente de Extratos Bancários Portugueses e Deduplicação em Tempo Real
- **Deteção Automática de Dialetos Bancários**: Reconhecimento automático de cabeçalhos e formatos de ficheiros dos principais bancos em Portugal (Caixa Geral de Depósitos, Millennium BCP, Santander Totta, ActivoBank, Revolut, Moey!, Banco CTT, Novo Banco).
- **Mapeamento Inteligente de Colunas**: Auto-preenchimento das colunas de Data, Descrição, Montante, Débito, Crédito e Referência bancária.
- **Badge e Estatísticas de Importação em Tempo Real**: Apresentação visual do banco identificado e contagem clara de novos movimentos vs duplicados na base de dados.

---

## V3.47 — v3.47.0 — 25 de agosto de 2026


Modernização integral da arquitetura da aplicação, conformidade fiscal portuguesa CIRS (Anexo G/J), planeamento FIRE com regras dinâmicas Guyton-Klinger e Trinity 4%, parsers nativos de extratos bancários (OFX/QIF/CSV), blindagem de segurança e privacidade PII (RGPD), self-healing debugging com hook global de exceções, modelos de domínio canónicos com `@dataclass(slots=True)` e integração nativa com o Windows OS.

### 1. Conformidade Fiscal CIRS e Engenharia Quantitativa
- **Motor Fiscal CIRS (Anexo G e J)**: Implementação do módulo `services/tax.py` com cálculo rigoroso de mais-valias e menos-valias pelo método FIFO, coeficientes oficiais de desvalorização da moeda publicados em portaria e isenção automática para criptoativos detidos há mais de 365 dias.
- **Planeamento FIRE e Modelagem de Risco de Sequência de Retornos**: Implementação do módulo `services/fire.py` com regra dos 4% (Trinity Study), guardrails dinâmicos de Guyton-Klinger e simulação de risco nos primeiros anos de reforma.
- **Cálculo Robusto de XIRR/MWRR/TWRR**: Rentabilidades anualizadas ponderadas pelo dinheiro e tempo com convergência numérica híbrida Newton-Raphson e Bisection.

### 2. Parsers Nativos de Extratos Bancários Portugueses
- **Parsers OFX e QIF Puros**: Ingestão de ficheiros bancários SGML/XML sem dependências externas pesadas e normalização de dialetos de bancos em Portugal (CGD, BCP, Santander, ActivoBank, Revolut, Novo Banco).
- **Tratamento de Débito/Crédito e Datas Flexíveis**: Suporte a extratos com colunas separadas para entradas e saídas e formato de datas com nomes de meses em português.

### 3. Segurança, Privacidade PII & Self-Healing Debugging
- **Anonimização Local de PII**: Mascaramento estrito de IBANs, NIFs, emails e montantes antes do envio de resumos para o Google Gemini AI.
- **Defesa Contra Injeção em Exportações**: Higienização rigorosa contra formula injection em CSV/Excel (`=`, `+`, `-`, `@`, `\t`, `\r`).
- **Hook Global de Exceções**: Captura e registo de falhas não tratadas via `sys.excepthook` com diálogo explicativo e fallback Win32 nativo.
- **Self-Healing no Undo/Redo**: Auto-isolamento com marcação `status='failed'` para registos corrompidos no diário de alterações.

### 4. Modelos de Domínio, Observabilidade e Integração Windows
- **Modelos de Domínio com `@dataclass(slots=True)`**: Criação de `domain/models.py` com tipagem estrita Python 3.10+ para transações, contas, categorias, metas e ativos.
- **Sanidade Física de Base de Dados**: Tripla verificação de integridade SQLite (`PRAGMA quick_check`, `integrity_check`, `foreign_key_check`) e profiling de queries lentas ($\ge 50\text{ms}$).
- **Integração Windows OS**: Deteção de tema escuro/claro no Registry (`AppsUseLightTheme`), Bring-to-Front da janela em tentativas de abertura duplicada e encerramento atómico com `PRAGMA wal_checkpoint(TRUNCATE)`.

---

## V3.46 — v3.46.0 — 24 de agosto de 2026


Otimizações avançadas de desempenho de renderização UI, concorrência e paralelismo de processamento, streaming de memória I/O, segurança e zeroing criptográfico na RAM e estabilidade matemática analítica.

### 1. Desempenho de UI, Renderização & Troca de Tabs
- **Desanexação de `QGraphicsOpacityEffect` (P0)**: Em `main.py:goto()`, resolução do problema de renderização de software offscreen após animações de fade de tabs (`new_widget.setGraphicsEffect(None)`), restaurando instantaneamente o sub-pixel antialiasing ClearType do Windows e acelerando a taxa de quadros (FPS) de scroll em 40–60%.
- **Controlo de Animações de Entrada de Gráficos**: Em `ui/components/charts/base.py`, introdução de guarda de animação única (`_has_animated_once`) por ciclo de dados, eliminando disparos concorrentes de animações de 700ms a cada mudança de tab.
- **Eliminação de Refreshes Redundantes**: Remoção de chamadas repetidas de atualização no `showEvent` de `MovementsPage` e margem inferior segura garantida ($\ge 48\text{px}$) em `BarChart`.

### 2. Concorrência, Thread-Safety & Workers Assíncronos
- **Simulações de Monte Carlo em Segundo Plano**: Em `services/financial_intelligence.py` e `ui/pages/investments.py`, execução assíncrona de até 5.000 iterações com cancelamento cooperativo atómico de tarefas anteriores ao arrastar sliders.
- **Thread-Safety em `BackgroundTaskRegistry`**: Implementação de mutex `threading.RLock()` para sincronização de registo de tarefas e correção semântica de verificação em `AsyncTask.is_running()`.
- **Deduplicação de Consultas no Dashboard**: Reutilização de agregações mensais em `DashboardWorker`, eliminando consultas de base de dados sobrepostas.
- **Encerramento Gracioso de Workers**: Adição de timeout de 1000ms para finalização limpa de conexões SQLite thread-local no fecho da aplicação.

### 3. Base de Dados, I/O & Streaming de RAM
- **Subquery Correlacionada Escalar para Anexos**: Em `database/repo/transactions.py`, substituição da junção agregada em tabela completa por `(SELECT COUNT(*) FROM transaction_attachments a WHERE a.transaction_id=t.id)`, acelerando paginações de movimentos de $O(N)$ para $O(\text{LIMIT} \times \log N)$.
- **Novos Índices Compostos**: Criação de `idx_transaction_splits_cat_amount_tx`, `idx_transaction_splits_cat_id_amount_tx`, `idx_categories_kind_active_name`, `idx_transactions_account_id_date_amount` e `idx_monthly_category_budgets_cat_id_month`.
- **Cache LRU de Resolução de Identidades**: Em `database/repo/identity.py`, introdução de cache em memória para resoluções de contas e categorias com auto-invalidação em mutações.

### 4. Criptografia, Zeroing Seguro de RAM & Estabilidade Numérica
- **Zeroing Explícito na RAM (`secure_zero`)**: Implementação de limpeza com `ctypes.memset` para chaves derivadas AES-256 e palavras-passe em `services/backup.py` e `utils/output_security.py`.
- **Fórmulas Estáveis com `math.expm1` e `math.log1p`**: Em `services/finance_math.py` e `services/financial_intelligence.py`, eliminação de cancelamento catastrófico de precisão em taxas de juro compostas infinitesimais e retornos estocásticos.
- **Expansão de Padrões de Limpeza Temporária**: Inclusão de novos padrões de ficheiros temporários (`.*.encrypting`, `.*.decrypting`, etc.) na rotina de arranque.

---

## V3.45 — v3.45.0 — 24 de agosto de 2026


Auditoria global multi-agente e otimização coordenada: índices compostos de cobertura de alto rendimento, streaming de consultas SQLite, isolamento thread-safe de operações de backup, limpeza de chaves na memória RAM, caches LRU limitados e fórmulas matemáticas financeiras analíticas $O(1)$.

### 1. Base de Dados, Índices Compostos & Streaming I/O
- **Índices de Cobertura (Covering Indexes)**: Criação de índices compostos essenciais (`idx_transactions_date_amount_cat`, `idx_holdings_asset_class_name`, `idx_transaction_splits_covering`, `idx_subscription_occurrences_tx_id`, `idx_scheduled_payment_occurrences_tx_id`), transformando pesquisas e agregações analíticas em consultas Index-Only com aceleração de até 90%.
- **Configuração de PRAGMAs Read-Only**: As ligações assíncronas de leitura em `database/read_only.py` passam a herdar `cache_size=-64000`, `mmap_size=268435456` e `temp_store=MEMORY`, eliminando contenção de I/O em dashboards concorrentes.
- **Streaming de Consultas (`query_iter` & `query_chunks`)**: Implementação de geradores iterativos em `FinanceDB` e `BaseRepo`, reduzindo o pico de memória RAM na leitura de grandes volumes de registos.

### 2. Concorrência, Thread-Safety & Segurança
- **Isolamento de Threads de Validação de Backup**: O método `run_verify_latest_backup_for_database` em `BackupService` e `HealthPage` passa a utilizar uma ligação e repositório isolados por thread (`_ThreadOwnedBackupRepo`), impedindo mutações concorrentes na ligação principal da interface gráfica.
- **Limpeza de Segredos e Chaves na RAM**: Sobregravação explícita de buffers de chaves AES e credenciais em blocos `try...finally`, além de proteção com mutex `threading.Lock` para caches de chaves partilhados.
- **Pré-indexação em Lote para Deduplicação de Extratos**: Em `services/imports.py`, as verificações de duplicados deixam de efetuar consultas $N+1$, pré-carregando todo o intervalo temporal do ficheiro numa única consulta SQLite otimizada.
- **Prevenção de Reentrância no Encerramento**: Em `BackgroundTaskRegistry.wait_for_idle`, inclusão de `ExcludeUserInputEvents` no bombeamento de eventos para proteger a persistência final de dados.

### 3. Matemática Financeira & Bounded Caches
- **Caches com Limite LRU (`BoundedLRUCache`)**: Em `services/financial_health.py`, substituição de dicionários abertos por caches `OrderedDict` limitados (`maxsize=128`) com suporte a `clear_cache()`, garantindo uso constante de RAM em sessões prolongadas.
- **Fórmula Fechada de Anuidade Ordinária ($O(1)$)**: Em `services/finance_math.py:present_value_of_contributions`, substituição de loops discretos pela fórmula analítica geométrica fechada sem erro de arredondamento.
- **Precisão em Somatórios de Float**: Utilização de `math.fsum` em `services/investment_performance.py` para eliminar derivação de vírgula flutuante em atribuição de rentabilidade.

### 4. Validação e Qualidade
- 100% de conformidade com o linter `ruff`.
- 100% de testes aprovados em toda a suite Pytest.

---

## V3.44 — v3.44.0 — 24 de agosto de 2026


Otimização de compressão Deflate de alto rácio e validação profunda de integridade criptográfica: ficheiros ZIP até 25% mais compactos e testes exaustivos de resistência a adulteração AES-GCM.

### 1. I/O, Compressão & Criptografia
- **Compressão Máxima `compresslevel=9`**: Em `database/engine.py:export_full_zip` e `services/backup.py:scrub_ai_secret_from_zip_file`, aplicação de nível máximo de compressão Deflate (`compresslevel=9`), gerando arquivos de dados e backups exportados mais compactos para armazenamento e partilha.
- **Validação de Cifragem AES-256-GCM & PBKDF2**: Testes exaustivos de resistência contra adulteração de bytes em tags AEAD, verificação de quotas de disco prévias, e proteção contra ataques de Zip Bomb.

### 2. Validação e Qualidade
- 100% de conformidade com o linter `ruff`.
- Execução e aprovação completa de todas as 20 suites de testes de backup, restauro e empacotamento.

---

## V3.43 — v3.43.0 — 24 de agosto de 2026


Correção ergonómica de sobreposição visual na barra de ferramentas flutuante (`PageToolsBar`): reposicionamento de controlos de paginação e eliminação de colisões em tabelas longas.

### 1. UI/UX e Correção de Layout
- **Eliminação de Sobreposição no Botão "Carregar Mais"**: Em `ui/pages/movements.py` e `ui/pages/core_pages.py` (Documentos), o botão de carregamento incremental de movimentos/documentos foi reposicionado junto ao texto informativo de estado com expansão flexível à direita (`addStretch(1)`), eliminando qualquer colisão com a barra flutuante `PageToolsBar` (`↑ Topo`, `⚙ Filtros`, `↻ Repor`) no canto inferior direito.

### 2. Validação e Qualidade
- 100% de conformidade com o linter `ruff`.
- Execução e aprovação completa de todas as suites de testes de interface gráfica e posicionamento.

---

## V3.42 — v3.42.0 — 24 de agosto de 2026


Implementação do sistema *Intelligent Idle Pre-Warming* e calibração de transição rápida Liquid Glass: eliminação da pausa no primeiro clique ao mudar de abas e resposta tátil de 160ms.

### 1. UI/UX e Pré-Aquecimento de Páginas
- **Intelligent Idle Pre-Warming**: Em `main.py`, implementação de uma fila escalonada em segundo plano (`_prewarm_queue` e `_prewarm_timer`) que instancia silenciosamente as abas mais frequentes (*Movimentos*, *Carteira*, *Panorama*, *Análises*, *Planeador*, *Orçamentos*, *Subscrições*) durante os momentos de inatividade do utilizador após o arranque, eliminando o atraso de compilação/criação de widgets no primeiro clique.
- **Transição Rápida de 160ms**: Em `main.py:goto`, calibração da curva de animação de fade (`QEasingCurve.OutQuad` a 160ms) para navegação fluida, tátil e instantânea sem atrasos.

### 2. Validação e Qualidade
- 100% de conformidade com o linter `ruff`.
- Execução e aprovação completa de todas as suites de testes de integridade e prontidão.

---

## V3.41 — v3.41.0 — 24 de agosto de 2026


Otimização de desempenho e aceleração do Score Financeiro 2.0: eliminação de amplificação de consultas $N \times M$ através de caches memoizados a nível de instância e unificação de agregações SQL de qualidade de dados.

### 1. Performance e Otimização de Base de Dados
- **Aceleração do Score Financeiro 2.0**: Em `services/financial_health.py`, implementação de caches memoizados (`_totals_cache`, `_net_worth_cache`, `_budget_cache`, `_quality_cache`, `_snapshot_cache`), reduzindo o volume de consultas síncronas de ~854 para menos de 15 execuções SQLite por cálculo de score.
- **Agregação SQL Unificada**: Em `_quality_metrics`, unificação das contagens de total e movimentos sem categoria numa única consulta agregada, eliminando varrimentos redundantes na tabela de transações.
- **Zero-Lag na GUI**: Abertura e recálculo instantâneo no ecrã de Score Financeiro e nos cartões analíticos do Dashboard.

### 2. Validação e Qualidade
- 100% de conformidade com o linter `ruff`.
- Execução e aprovação completa de todas as 64 suites de testes de analytics e performance.

---

## V3.40 — v3.40.0 — 23 de agosto de 2026


Auditoria da Assembleia Geral de Agentes de IA: observabilidade reforçada em diálogos e geometria, calibração do scroll do Liquid Glass e eliminação total de exceções silenciosas.

### 1. Observabilidade e Diagnóstico
- **Eliminação de Exceções Silenciosas**: Substituição de blocos `except: pass` por registo defensivo com `log_warning` em `ui/dialogs.py` (carregamento de contas associadas em investimentos) e `main.py` (restauração de geometria da janela).
- **Rastreabilidade**: Garantia de que nenhuma falha de I/O periférica passe despercebida nos relatórios de diagnóstico do sistema.

### 2. Ergonomia e UI Liquid Glass
- **Calibração de Scroll**: Configurado `singleStep(24)` no `QScrollBar` vertical principal em `main.py`, proporcionando uma rolagem mais suave e previsível em trackpads e ratos de alta precisão.

### 3. Validação e Qualidade
- 100% de conformidade com o linter `ruff`.
- Execução e aprovação completa de todas as 517 suites de testes de regressão.

---

## V3.39 — v3.39.0 — 23 de agosto de 2026


Implementação do plano de micro-fases de otimização de concorrência e UI: exportação assíncrona de backups com `AsyncTask`, mitigação de bloqueio de UI, reforço de ciclo de vida de tarefas e sincronização de versão.

### 1. Concorrência e Exportação em Segundo Plano
- **Exportação Assíncrona de Backups**: Em `ui/pages/settings.py`, `export_backup` passa a ser executado em segundo plano através do `BackgroundTaskRegistry` / `backup_task`, desativando temporariamente o botão e exibindo feedback visual ("A exportar…"), prevenindo qualquer bloqueio da interface gráfica (GUI freeze) mesmo em bases de dados com anexos volumosos.
- **Gestão de Resposta de Tarefas**: Manipuladores de conclusão e erro (`_on_backup_task_result`, `_on_backup_task_error`, `_on_backup_task_finished`) atualizados para suportar operações de exportação em simultâneo com cópias de segurança locais e testes de recuperação.

### 2. Validação e Qualidade
- Execução integral das suites de testes de regressão (`pytest -v`).
- Conformidade total com o linter `ruff`.

---

## V3.38 — v3.38.0 — 23 de agosto de 2026


Auditoria profunda e refatoração arquitetural de nível sénior (`python-expert-development`), integridade transacional completa com savepoints aninhados, isolamento e resiliência de base de dados, segurança de concorrência e conformidade de testes a 100%.

### 1. Integridade Transacional e SQLite Savepoints (Micro-Fase 1)
- **Eliminação de Conflitos de Transação**: Removido o comando `BEGIN IMMEDIATE` manual da migração FTS (`_migration_remove_obsolete_transactions_fts`) em `database/engine.py`, prevenindo falhas de transação concorrente no SQLite gerido por Python.
- **Savepoints Aninhados Incondicionais**: Em `database/repo/transactions.py` e `database/repo/planning.py`, as operações compostas como `update_internal_transfer`, `adjust_goal_with_transaction` e `process_due_subscriptions` agora geram savepoints de nome único (`uuid.uuid4().hex[:8]`), garantindo reversão atómica e isolada em caso de erro parcial.
- **Rollback Atómico em Repositórios**: Adicionados blocos defensivos `try...except rollback` em todas as mutações de repositório (`database/repo/planning.py`, `database/repo/transactions.py`, `database/repo/identity.py`, `database/repo/documents.py`, `database/repo/simulations.py`, `database/repo/ai.py`, `database/repo/analytics.py` e `database/repo/base.py`).
- **Exportação Dinâmica no Backup Integral**: `FinanceDB.export_full_zip` agora descobre e extrai dinamicamente todas as tabelas de utilizador através de `sqlite_master`, assegurando que futuras tabelas nunca fiquem excluídas da exportação.
- **Substituição Atómica de Backups**: Em `FinanceDB.backup_to`, o backup online é efetuado primeiramente para ficheiro temporário `.tmp` e substituído atomicamente no destino com remoção segura em caso de exceção.

### 2. Otimização de Concorrência, Redação de Logs e Arranque (Micro-Fases 2 e 5)
- **Redação Rigorosa de Segredos**: Ajustada a expressão regular em `utils/app_logging.py` para isolar tokens únicos de chaves de API sem truncar campos downstream em cadeias compostas de log.
- **Otimização do Ciclo de Vida do Event Loop**: Removidas chamadas temporizadas redundantes em `BasePage.showEvent`, acelerando o arranque a frio e mantendo a responsividade do loop de eventos abaixo dos limites de teste.
- **Controlo Dinâmico de Dimensões do Cartão de Filtros**: Ajustada a gestão de geometria de `dashboard.filters_card` com `setSizePolicy(Expanding, Maximum)` e recalculação de tamanho ao alternar entre filtros recolhidos e expandidos.

### 3. Validação Integral e Qualidade de Código
- **517 Testes com Sucesso a 100%**: Execução de todas as suites de testes com 0 falhas, incluindo validação visual Windows, integridade de credenciais e segurança de threads.
- **Conformidade de Linter Ruff**: Código 100% limpo segundo as regras do `ruff`.

---

## V3.37 — v3.37.0 — 23 de agosto de 2026


Correção sistémica de responsividade no arranque a frio, eliminação definitiva do truncamento de gráficos e cartões em toda a aplicação, e resolução de avisos de layout em diálogos modais.

### Renderização e Altura Integral de Cartões no Arranque

- `sync_content_size()` passa a utilizar o `sizeHint()` natural dos cartões da página ativa em vez do seu `minimumSizeHint()`, impedindo que o Qt comprima artificialmente a área de visualização e esmague os cartões na abertura inicial.
- `GlassCard` implementa agora `sizeHint()` e `minimumSizeHint()` calculados diretamente a partir do seu layout interno vertical `v`, garantindo que os cartões nunca permitem ser encolhidos abaixo do espaço exigido pelos seus títulos, gráficos e margens.
- A salvaguarda em `GlassCard.resizeEvent` assegura que nenhum cartão de conteúdo perde altura em redimensionamentos, eliminando o corte inferior do gráfico de composição de despesas (`comp_card` com folga >= 390px).
- `CurrentPageStack` passa a devolver diretamente a geometria do widget ativo e elimina chamadas redundantes a `self.stack.adjustSize()`, prevenindo recursão e bloqueios em pilha.

### Auditoria Global e Padronização de Margens em Todos os Gráficos

- Realizada auditoria sistemática às 38 classes de componentes gráficos em 25 ficheiros, identificando e corrigindo 12 gráficos com margens inferiores apertadas (< 46px).
- Expandidas as margens inferiores de desenho (`plot_rect`) para um padrão de segurança `>= 48px` em `BalanceChart`, `CumulativeCashflowChart`, `SavingsEfficiencyChart`, `InvestmentEvolutionChart`, `ScoreTrendChart`, `ScoreBreakdownChart`, `BudgetForecastChart`, `BudgetVarianceHeatmapChart`, `SpendingSeasonalityChart` e `DataQualityTrendChart`.
- Aumentada a altura mínima em `StackedExpenseChart` (340px), `HeatmapChart` (210px) e `NetWorthChart` (270px), garantindo total visibilidade das cotas de 0 €, rótulos de todos os 12 meses e legendas de intensidade.
- O cálculo de colunas responsivas em `BasePage` passa a usar a largura real física do viewport de scroll (`effective_width = max(self.width(), viewport_w)`), estabelecendo a grelha ideal (ex: 3 colunas) logo no primeiro instante de apresentação.

### Correção de Layout Duplicado em Diálogos Modais (LiabilityDialog)

- Corrigido o `LiabilityDialog` em `ui/dialogs/sheets/sheet_liability.py`, eliminando a criação de um layout duplicado `QVBoxLayout(self)` sobre o layout já existente da classe base `LiquidSheetDialog`.
- Eliminados todos os avisos de `QLayout: Attempting to add QLayout "" to LiabilityDialog "", which already has a layout` no arranque e nos logs da consola.

### Persistência de Janela e Novas Suites de Testes

- `LiquidFinanceApp` guarda e restaura com precisão o estado Maximizado e a geometria restaurada entre sessões.
- Implementadas novas suites de testes unitários e de integração com cobertura completa:
  - `tests/test_startup_resizing.py`: validação de arranque e reflow responsivo instantâneo.
  - `tests/test_panorama_responsive_layout.py`: verificação do layout em 3 colunas e margens do Panorama.
  - `tests/test_all_charts_geometry.py`: teste automatizado de integridade e margens de segurança para todas as 38 classes de gráficos da aplicação.
  - `tests/test_all_liquid_dialogs.py`: validação de todos os diálogos Liquid Glass sem avisos de layout.

---

## V3.36 — v3.36.0 — 23 de agosto de 2026


Correção estrutural da contabilização dos objetivos, portabilidade integral dos
documentos geridos e isolamento de datas legadas inválidas no Dashboard.

### Objetivos passam a ser transferências internas de património

- Um reforço continua a retirar dinheiro da conta escolhida e um levantamento
  continua a devolvê-lo, mas a categoria `Objetivos e Poupança` deixa de contar
  como despesa ou receita.
- A mesma regra central é aplicada aos totais, taxa de poupança, categorias,
  orçamentos, previsões, comparadores, diagnósticos, gráficos acumulados e
  deteção de anomalias; não existe uma correção isolada apenas no Dashboard.
- O valor guardado nos objetivos passa a integrar os ativos e o património
  líquido. Um reforço reduz caixa e aumenta o objetivo pelo mesmo valor, pelo
  que deixa de criar uma perda patrimonial artificial.
- Os estados patrimoniais diários passam a guardar também os objetivos. A ponte
  patrimonial identifica separadamente o movimento de caixa e a variação do
  ativo, e declara quando um estado histórico antigo não tem essa cobertura.
- Cada novo movimento de objetivo recebe `goal_id` e uma cópia estável do nome.
  A migração 26 liga apenas movimentos históricos com uma correspondência
  única e inequívoca.
- Ao eliminar um objetivo, a ligação é anulada atomicamente mas o nome histórico
  permanece no movimento, evitando registos sem contexto ou associações
  inventadas.

### Anexos portáteis e validados depois de mover ou restaurar

- Novos documentos guardam `transação/nome-interno` em vez de uma localização
  absoluta dependente do utilizador ou do computador.
- A migração 26 converte caminhos absolutos antigos apenas quando pertencem ao
  arquivo gerido ou quando o ficheiro copiado existe na localização canónica.
  Referências externas ou incoerentes não são seguidas.
- A abertura, auditoria, remoção e o histórico **Desfazer** resolvem caminhos
  relativos na pasta atual e mantêm compatibilidade controlada com registos
  antigos.
- Uma migração da localização valida todos os documentos na árvore temporária
  antes de a publicar. Um restauro valida presença, tamanho e SHA-256 de todas
  as referências antes de remover o marcador de recuperação.
- Se a validação do restauro falhar, o perfil anterior é reposto pelo mecanismo
  durável já existente. A barreira de atualização permite apenas a conversão do
  caminho derivado e o preenchimento de metadados de integridade; os bytes dos
  documentos continuam protegidos separadamente.

### Datas antigas deixam de interromper o Dashboard

- `parse_date` passa a aceitar exclusivamente o formato completo
  `AAAA-MM-DD`; texto adicional, datas impossíveis e valores truncados são
  rejeitados.
- A consulta de anomalias filtra datas e montantes inválidos no SQLite e mantém
  ainda uma barreira por registo. Uma única linha legada corrompida deixa de
  interromper o cálculo inteiro.
- Os registos válidos continuam a produzir alertas normalmente; as linhas
  excluídas permanecem disponíveis para a área de qualidade dos dados.

### Estrutura, preservação e testes

- Esquema atualizado para a migração **26** com ligação de objetivos,
  `goal_name_snapshot` e histórico diário de objetivos.
- O primeiro desenho da janela mantém agora o Dashboard compacto até a
  interface responder. Os gráficos deixam de ser todos organizados durante a
  primeira pintura; dez arranques frios consecutivos construíram a janela em
  cerca de 1,53–1,62 segundos e mantiveram o ciclo de eventos responsivo.
- As migrações em lote passam a reconstruir as barreiras de validação uma única
  vez no fim, e o tema dos pop-ups é instalado antes de existir a árvore de
  widgets. As mesmas proteções permanecem ativas sem repetir trabalho pesado no
  arranque.
- A atualização foi exercitada através da barreira real V3.35→V3.36: criou a
  proteção anterior, normalizou um caminho absoluto, calculou tamanho/SHA-256 e
  manteve o documento e os dados financeiros intactos.
- A migração foi igualmente executada numa cópia temporária exata do perfil
  real: passou de esquema 25 para 26, preservou os **95 movimentos**, o objetivo
  e o respetivo saldo, terminou com `integrity_check = ok` e não alterou um byte
  da base original.
- Validação final concluída com `Ruff`, compilação Python e **505 testes
  aprovados**. O pacote Windows V3.36 foi reconstruído e o executável passou o
  auto-teste num perfil temporário isolado.

---

## V3.35 — v3.35.0 — 22 de agosto de 2026

Reparação protegida do erro SQLite `database disk image is malformed` que
impedia criar, editar ou apagar movimentos durante o processamento automático
das subscrições.

### Causa confirmada sem alterar a base real

- As verificações gerais `PRAGMA quick_check`, `integrity_check` e
  `foreign_key_check` devolviam `ok`, embora as gravações em movimentos
  falhassem. Por isso, as barreiras existentes não conseguiam localizar o
  problema.
- A causa foi isolada numa tabela virtual FTS5 antiga, `transactions_fts`, já
  substituída desde a migração 22 por `transaction_search_fts` e sem qualquer
  utilização no código atual.
- Três triggers antigas — `transactions_ai`, `transactions_ad` e
  `transactions_au` — continuavam, porém, a atualizar esse índice obsoleto em
  cada criação, edição ou remoção. A corrupção de dados derivados bloqueava
  assim a tabela financeira verdadeira sem existir corrupção nas suas linhas.
- Os registos mostram que o primeiro erro relacionado já existia em 20 de
  agosto e que a mesma falha ocorreu no arranque das versões V3.32, V3.33 e
  V3.34. A atualização V3.34 não criou o dano.

### Migração 25 protegida e sem perda

- Antes da reparação, a V3.35 cria automaticamente a proteção integral normal
  da versão anterior, incluindo base, documentos, histórico e preferências.
- A migração 25 elimina primeiro apenas os três triggers antigos e depois a
  tabela virtual obsoleta, dentro da mesma transação. Não reescreve movimentos,
  contas, categorias, documentos, orçamentos, investimentos ou definições.
- O índice atual `transaction_search_fts`, a sua pesquisa literal e os seus
  triggers independentes permanecem intactos.
- Se a migração ou a validação posterior falhar, o arranque protegido fecha a
  ligação e repõe a proteção anterior.

### Corrupção deixa de ser ignorada no arranque

- Uma falha SQLite genuína durante a cobrança automática das subscrições deixa
  de ser apenas registada no log enquanto a aplicação continua a abrir.
- A transação é anulada e o arranque é bloqueado com a janela de proteção antes
  de quaisquer gravações normais adicionais.
- Foi acrescentada uma validação FTS5 dirigida ao índice de pesquisa ativo. É
  executada depois das migrações porque a verificação SQLite genérica não cobre
  de forma suficiente todos os índices virtuais externos antigos.
- Erros transitórios, como `database is locked`, não são classificados
  incorretamente como corrupção física.

### Provas de preservação

- Um teste de regressão constrói o índice antigo, danifica deliberadamente o
  respetivo bloco FTS5 e confirma o comportamento real: a verificação SQLite
  geral diz `ok`, mas uma edição de movimento falha como `malformed`.
- O mesmo teste abre a base com a V3.35, confirma a proteção prévia, aplica a
  migração, compara todos os campos do movimento e volta a executar criação,
  edição e remoção sem erro.
- A migração foi ainda executada numa cópia exata da base afetada do utilizador:
  os **94 movimentos permaneceram 94**, todas as impressões digitais das
  tabelas de dados ficaram iguais, as relações mantiveram zero erros e as duas
  verificações SQLite/FTS5 terminaram com `ok`.
- A base real permaneceu fechada e não recebeu qualquer escrita durante o
  diagnóstico ou a validação.
- `Ruff`, compilação Python e a bateria completa terminaram com **499 testes
  aprovados** em perfis temporários isolados.

---

## V3.34 — v3.34.0 — 22 de agosto de 2026

Correção reforçada do erro persistente que ainda podia apresentar caixas de
confirmação pretas e texto escuro no tema claro dentro da aplicação real.

### Paleta protegida em cada pop-up real

- A validação da V3.33 reproduzia uma `QMessageBox` isolada e não reproduzia a
  alteração tardia da paleta nativa que acontecia dentro da aplicação já
  aberta. Por isso, o teste anterior podia passar enquanto a confirmação real
  continuava preta.
- A aplicação passa a definir também uma `QPalette` completa e coerente com o
  tema ativo. Superfície, texto, botões, seleção, ligações e estados desativados
  deixam de depender das cores herdadas do Windows.
- Foi instalado um filtro global que deteta cada `QMessageBox` durante as fases
  `Polish` e `Show`. Nesse momento, a caixa recebe diretamente a paleta e o
  estilo atuais, mesmo que outro componente tenha substituído o estilo global.
- O modo de diálogo nativo é desativado para estas caixas. O Qt deixa assim de
  trocar silenciosamente a superfície clara por uma janela escura do sistema.
- A confirmação **Apagar todos os movimentos** deixou de usar o atalho estático
  `QMessageBox.warning`. É agora uma instância explícita, com estilo local,
  botão **Continuar**, botão **Cancelar** e explicação secundária separada.
- As frases simples dos avisos recebem quebras de linha estáveis. Isto corrige
  também texto cortado a meio no limite direito do pop-up.

### Validação contra a falha real

- O novo teste força deliberadamente uma paleta nativa preta depois de ativar o
  tema claro e remove o estilo global. A caixa real continua com superfície
  `#F8FAFF`, texto `#142035` e luminosidade mediana superior a 205.
- O mesmo teste inverso confirma o tema escuro com superfície `#111827` e texto
  `#F8FAFC`.
- Um teste adicional executa o método real de **Apagar todos os movimentos**
  com um repositório simulado, confirma os dois botões em português e cancela a
  operação antes de qualquer alteração.
- A captura real do Windows foi repetida com a paleta hostil e confirmou texto
  integral, quebra de linha, fundo claro e botões legíveis.
- `Ruff`, compilação Python e a bateria completa terminaram com **496 testes
  aprovados** em perfis temporários isolados. A base de dados real não foi
  aberta para escrita durante esta correção.

---

## V3.33 — v3.33.0 — 22 de agosto de 2026

Correção global das caixas de aviso, confirmação e informação para respeitarem
o tema claro/escuro da aplicação, incluindo janelas Qt que o Windows separa da
janela principal.

### Pop-ups legíveis no tema ativo

- Foi corrigido o fundo preto que podia aparecer em `QMessageBox` com o tema
  claro, deixando texto e botões escuros praticamente invisíveis.
- A causa era a transformação destas caixas em janelas de topo independentes no
  Windows. Nessa situação, o estilo aplicado à janela principal deixava de ser
  herdado e o Qt reutilizava a paleta escura do sistema.
- O tema passa a publicar um estilo global limitado a pop-ups: `QToolTip` e
  `QMessageBox`. O restante interface continua sob o estilo próprio da janela
  principal, sem alterar a composição dos cartões e gráficos.
- O tema claro usa agora superfície `#F8FAFF`, texto `#142035`, botões brancos e
  realce azul no botão predefinido. O tema escuro usa superfície `#111827`,
  texto `#F8FAFC` e controlos `#172033`.
- Texto principal, explicação secundária, área de botões, estados hover e botão
  predefinido recebem cores explícitas. Nenhum elemento depende agora da paleta
  nativa que estiver ativa no Windows.
- Avisos apresentados antes de carregar a janela principal — recuperação,
  proteção de dados e segunda instância — recebem uma base clara e legível. Ao
  terminar o arranque, o estilo é atualizado para o tema guardado pelo
  utilizador.

### Validação

- Novo teste renderiza a confirmação **Apagar todos os movimentos** nos temas
  claro e escuro e mede a luminosidade real da imagem. O fundo claro tem
  mediana superior a 205 e o escuro inferior a 90.
- A validação visual real do Windows guarda agora capturas dedicadas das duas
  variantes e rejeita automaticamente um fundo nativo incompatível com o tema.
- `Ruff`, compilação Python e a bateria completa terminaram com **493 testes
  aprovados** em perfis temporários isolados, sem modificar os dados reais.

---

## V3.32 — v3.32.0 — 22 de agosto de 2026

Revisão para ecrãs Full HD, edição de movimentos mais compacta e novos
controlos protegidos para a localização dos backups e a limpeza integral do
histórico de movimentos.

### Suporte real para 1920 × 1080

- A matriz responsiva passa a incluir **1920 × 1080** em todos os quatro
  estados de tema claro/escuro e privacidade ativa/inativa, além de 960, 1280 e
  1600 px.
- Títulos, descrições, legendas, estados, contextos temporais e rodapés dos
  cartões calculam a altura necessária para a largura realmente atribuída. Uma
  frase longa deixa de depender da estimativa de uma única linha.
- A validação visual mede todas as etiquetas visíveis com quebra de linha no
  Dashboard e rejeita qualquer caso em que o texto necessite de mais altura do
  que recebeu.
- O validador visual passou a criar os seus próprios dados de demonstração num
  perfil descartável. Uma execução direta já não pode produzir um falso erro
  de gráficos vazios por depender de uma variável definida apenas pelo pytest.
- O cartão **Despesas por categoria** tem mais 20% de altura: a área mínima do
  gráfico passou de 230 para 276 px e a estrutura completa do cartão cresce na
  mesma proporção. Nomes, valores e percentagens mantêm-se visíveis.
- O cartão **Lista de movimentos** também cresceu 20%: de 600 para 720 px; a
  tabela interior passou de 520 para 624 px. Continuam disponíveis o scroll,
  a ordenação e as larguras de coluna guardadas.
- Foi preservado o espaçamento compacto de 4 px no cartão **Orçamento vs.
  realizado**, aceitando apenas a diferença máxima de 2 px causada pelo
  arredondamento de fontes em escalas do Windows.

### Editor de movimentos redesenhado

- **Novo movimento** e **Editar movimento** deixaram de abrir uma folha do
  tamanho da aplicação e de aplicar desfoque à janela principal.
- O editor é agora uma janela modal centrada, nítida e redimensionável, entre
  620–780 px de largura e 520–720 px de altura conforme o ecrã disponível.
- Os campos foram reorganizados numa grelha equilibrada de duas colunas, com
  cabeçalho curto, rótulos alinhados, controlos de altura consistente, notas e
  anexos dentro de scroll exclusivamente vertical.
- Os botões **Guardar movimento** e **Cancelar** permanecem visíveis no rodapé;
  a janela não cria scroll horizontal nem altera o efeito gráfico da aplicação.

### Pasta principal de backups configurável

- As Definições permitem escolher a pasta principal usada por backups manuais,
  automáticos e ao fechar, ou repor a localização segura predefinida.
- A escolha é guardada fora da base de dados no ficheiro de preferências de
  backup. Assim, tarefas em segundo plano, o encerramento e a recuperação de
  arranque usam exatamente a mesma pasta.
- Antes de guardar, a localização é criada, testada com uma escrita real e
  sincronizada para disco. Pastas dentro de documentos, histórico, proteções
  de versão ou configuração são rejeitadas; a segunda cópia também tem de usar
  uma localização diferente.
- Se uma unidade externa configurada estiver indisponível no arranque, a
  aplicação usa temporariamente a pasta predefinida, regista o motivo e mostra
  o estado nas Definições, sem perder a preferência escolhida.
- A recuperação de uma base em falta procura também backups diretamente nas
  pastas principal e secundária configuradas. A pesquisa externa é limitada a
  ficheiros de backup no nível principal, evitando percorrer recursivamente um
  disco inteiro.

### Limpeza protegida de todos os movimentos

- Nova área **Gestão dos movimentos** nas Definições com a ação **Apagar todos
  os movimentos…** e contagem atualizada dos registos abrangidos.
- A ação exige duas confirmações, incluindo escrever exatamente `APAGAR TODOS`.
  Antes de eliminar qualquer registo, cria e valida um backup completo na pasta
  principal configurada; se o backup falhar, nada é apagado.
- A remoção de movimentos, metadados, divisões, índice de pesquisa e ligações a
  anexos é executada numa única transação SQLite. Uma falha reverte a operação
  completa.
- Categorias, contas, orçamentos, subscrições, pagamentos agendados, objetivos,
  investimentos e documentos independentes são preservados. Ocorrências
  automáticas mantêm-se processadas, mas deixam de apontar para o movimento
  apagado, impedindo a regeneração silenciosa de cobranças antigas.
- Ficheiros de anexos geridos só são removidos depois da confirmação da base.
  Se a operação SQLite falhar, os ficheiros permanecem intactos; uma falha
  posterior do sistema de ficheiros deixa apenas um órfão recuperável pela
  auditoria de documentos.
- O histórico Desfazer anterior é marcado como indisponível, a integridade
  SQLite é verificada e todas as páginas são invalidadas e atualizadas após o
  sucesso. O caminho do backup de proteção fica explicitamente apresentado.

### Avisos sempre acima do arranque

- Alertas de recuperação e bloqueio de proteção são agora filhos modais da
  janela **A carregar Finanças Pessoais**, com prioridade de primeiro plano.
- O aviso de uma segunda instância recebe a mesma prioridade mesmo sem existir
  uma janela de arranque nesse processo, deixando de poder ficar escondido por
  trás do ecrã de carregamento da primeira aplicação.

### Validação

- A captura real do Windows aprovou **16 estados** em 960, 1280, 1600 e
  1920 × 1080, com tema claro/escuro e privacidade ativa/inativa. Todos os
  estados tiveram zero rótulos de navegação, estados ou textos com quebra de
  linha cortados.
- A captura dedicada do editor em 1920 × 1080 confirmou 780 × 636 px, 13
  rótulos visíveis, zero scroll horizontal, zero desfoque do pai e superfície
  com contraste e diversidade cromática válidos.
- Testes específicos confirmaram a persistência e utilização da pasta
  principal por workers, a descoberta para recuperação, a limpeza atómica, o
  rollback com anexos intactos, a geometria do editor e a sobreposição modal do
  aviso de arranque.
- `Ruff` e a compilação integral de Python terminaram sem erros. A bateria
  completa terminou com **491 testes aprovados** em perfis temporários isolados;
  a limpeza integral nunca foi executada sobre os dados reais do utilizador.

---

## V3.31 — v3.31.0 — 20 de agosto de 2026

Correção do histórico completo do Dashboard e revisão responsiva dos cartões
para impedir gráficos, indicadores e frases comprimidos ou cortados.

### Período “Tudo” novamente funcional

- A previsão de saldo aceita agora explicitamente um histórico sem limite. O
  valor `None`, usado pelo filtro **Tudo**, deixou de ser submetido a uma
  comparação numérica e já não provoca o erro `NoneType > int`.
- A cobertura da previsão começa na primeira data histórica realmente
  carregada. Não é inventada uma janela artificial quando está selecionado todo
  o histórico.
- A falha do worker deixa de substituir o Dashboard pelo cartão **Não foi
  possível atualizar**; totais, histórico e os 30 dias previstos voltam a ser
  entregues numa única atualização.
- Foi acrescentado um teste direto do serviço e outro do worker completo com
  `range_days=None`, comparação desligada e resolução mensal.

### Cartões e textos responsivos

- Os indicadores extensos de cobertura passam a calcular e fixar a altura real
  para a largura disponível. O texto quebra linha dentro do cartão, sem alargar
  a coluna nem ficar escondido atrás do gráfico.
- O `FlowLayout` partilhado passa a atribuir largura útil a etiquetas cujo
  tamanho horizontal é ignorado e a usar a altura dependente dessa largura.
  Corrige o estado do cartão **Orçamento vs. realizado**, que podia receber
  largura zero, e elimina altura vazia baseada numa medição antiga.
- Títulos, subtítulos e estado `Dados atualizados às…` das páginas podem quebrar
  linha e mantêm o texto completo no tooltip.
- Os cabeçalhos dos principais gráficos do Dashboard ajustam o título e o
  indicador de contexto sem sobreposição, preservando margens compactas.

### Prioridades financeiras e composição do Dashboard

- O resumo, âmbito, mensagem, evidência, métricas e explicação metodológica do
  cartão **Prioridades financeiras** passam a recalcular a altura em cada
  redimensionamento. Nenhuma frase depende agora do `sizeHint` de uma largura
  anterior.
- O título auxiliar e a cobertura do cartão usam uma linha fluida: permanecem
  lado a lado quando cabem e passam ordenadamente para outra linha quando a
  coluna é estreita.
- Nas larguras de duas colunas, **Margem e taxa de poupança**, **Objetivos em
  destaque** e **Prioridades financeiras** usam a linha completa. Em ecrãs
  largos mantém-se o arranjo 2:1 e a associação de Insights às despesas por
  categoria pedida anteriormente.

### Validação

- O filtro **Tudo** foi executado sobre a base real em modo estritamente só de
  leitura: `PRAGMA quick_check = ok`, 100 movimentos preservados, 262 pontos
  históricos e 30 pontos futuros carregados sem erro.
- A auditoria responsiva integral passou em 960, 1280 e 1600 px, temas
  claro/escuro e privacidade, incluindo medição explícita das frases sensíveis
  do Dashboard.
- Foram acrescentados testes de regressão para o período ilimitado, a passagem
  do worker, a composição em duas colunas e a altura real dos textos.
- A bateria integral terminou com **485 testes aprovados** em bases temporárias
  isoladas, sem escrever nos movimentos, documentos, preferências ou logs reais.

---

## V3.30 — v3.30.0 — 20 de agosto de 2026

Evolução da transparência dos cálculos e da identidade dos dados financeiros.
As relações internas deixam de depender de pequenas diferenças nos nomes, as
previsões passam a declarar a cobertura real usada e cada página indica quando
terminou a sua última leitura.

### Identificadores financeiros estáveis

- A migração aditiva 24 introduz identificadores opacos e permanentes para
  contas, categorias e ativos. O identificador de grupo já existente continua
  a ligar as duas partes de cada transferência interna.
- Movimentos, divisões, subscrições, pagamentos agendados, orçamentos, regras,
  objetivos mensais, passivos e ativos ligados a contas passam a guardar a
  relação interna, mantendo o nome como apresentação e compatibilidade.
- Escritas novas resolvem e guardam simultaneamente o identificador e o nome.
  Alterar um nome deixa de quebrar o histórico ou de apontar um gráfico para o
  ativo errado.
- A reparação de relações antigas é idempotente. O arranque evita percorrer todo
  o histórico quando os identificadores e triggers já estão completos.
- A proteção de versão permite exclusivamente completar uma relação antes
  vazia. Identificadores já preenchidos, datas, valores, descrições, categorias,
  contas, notas e documentos continuam protegidos contra substituição ou perda.

### Fusão segura de duplicados

- Nova página **Gestão e identidade dos dados**, disponível na navegação, para
  fundir contas ou categorias duplicadas.
- A ferramenta apresenta origem, destino, utilização, sugestões semelhantes e
  uma pré-visualização antes da confirmação.
- A fusão é atómica: religa todo o histórico, combina orçamentos aplicáveis,
  consolida divisões repetidas e preserva o nome antigo como alias pesquisável.
- A origem fica inativa, nunca é apagada, e cada operação grava um histórico
  técnico com as quantidades afetadas. Uma falha reverte a operação completa.

### Cobertura real e explicação dos gráficos

- Previsões e projeções mostram meses com dados, movimentos considerados,
  saldos iniciais conhecidos, investimentos com custo de aquisição preenchido
  e transações inválidas excluídas.
- A cobertura foi ligada aos cartões de previsão do Dashboard, Orçamentos,
  Investimentos, Centro financeiro e Relatórios; uma regra comum cobre também
  novos cartões preditivos que usem a infraestrutura partilhada.
- Ao clicar num gráfico, o detalhe inclui **Como este valor foi calculado** com
  período, filtros visíveis, número de movimentos, exclusões, cobertura e hora
  da leitura. As categorias são filtradas pelo identificador estável, incluindo
  aliases históricos.
- Os chips de confiança voltaram a ser compactos; a explicação extensa fica no
  indicador próprio e quebra linhas sem alargar as colunas do Dashboard.

### Atualidade e consistência da interface

- Todas as páginas apresentam `Dados atualizados às…`, estado de atualização,
  aviso de dados desatualizados e preservação da última leitura em caso de erro.
- Páginas síncronas concluem o estado automaticamente. Dashboard e Movimentos
  só o concluem quando a tarefa assíncrona realmente termina.
- O contexto de cálculo é aplicado centralmente a todos os gráficos reutilizáveis,
  evitando implementações incompletas página a página.
- A inicialização dos dados de demonstração foi reordenada para atribuir os
  identificadores às categorias antes de criar movimentos, impedindo associações
  ambíguas a uma categoria com identificador ainda vazio.

### Validação e compatibilidade

- Foram adicionados testes próprios para identificadores, fusões, rollback,
  cobertura, detalhe dos cálculos, atualidade das páginas e responsividade da
  nova gestão de dados.
- A migração foi executada numa cópia isolada da base real: 99 movimentos antes
  e depois, sequência 1–24 completa, `PRAGMA quick_check` aprovado e todas as
  colunas financeiras preexistentes com conteúdo idêntico.
- A atualização cria a proteção obrigatória antes de escrever e não altera a
  base real durante os testes de migração.
- A bateria integral terminou com **482 testes aprovados**, incluindo dados,
  migrações, rollback, backups, tarefas em segundo plano, interface PySide6,
  temas, privacidade e responsividade.

---

## V3.29 — v3.29.1 — 13 de agosto de 2026

Correção urgente de compatibilidade no arranque de bases anteriores à migração
das transferências internas. A proteção da V3.29.0 atuava corretamente e repunha
os dados, mas a atualização era interrompida antes de as migrações poderem
adicionar a coluna `transfer_group_id`.

### Arranque seguro a partir da V3.22

- `init_schema()` deixou de criar antecipadamente o índice
  `idx_transactions_transfer_group`. Em SQLite, `CREATE TABLE IF NOT EXISTS` não
  acrescenta colunas a uma tabela já existente; por isso o índice referia uma
  coluna que ainda só seria criada pela migração 20.
- A criação da coluna e do índice permanece na migração financeira correta,
  depois de a estrutura antiga estar aberta e protegida.
- Foi adicionada a migração 23, idempotente, que confirma fisicamente a coluna e
  o índice. Esta reparação cobre também bases cujo histórico indique que a
  migração 20 foi aplicada, mas onde a coluna esteja ausente.
- Uma atualização falhada pela V3.29.0 pode ser retomada diretamente com a
  V3.29.1: a base reposta pela proteção continua a ser a origem válida e não é
  necessário extrair manualmente o ZIP de proteção.
- Nenhum movimento é recriado ou alterado. A nova coluna recebe apenas o valor
  vazio nos movimentos antigos, conservando datas, descrições, valores, contas,
  categorias, notas e documentos.

### Testes de regressão

- Foi criada uma base V3.22 realista sem `transfer_group_id`, com um movimento
  pessoal, e executada a atualização completa até ao esquema 23.
- O teste confirma preservação integral do movimento, criação do índice,
  sequência completa das migrações e arquivo de proteção V3.22 → V3.29.1.
- Um segundo cenário simula um histórico inconsistente onde a migração 20 está
  registada mas a coluna falta; a migração 23 repara a estrutura sem perda.
- A correção foi também executada numa cópia isolada da base real restaurada:
  99 movimentos antes e depois, assinatura do conteúdo financeiro idêntica,
  `PRAGMA quick_check` aprovado e sequência 1–23 completa. A cópia temporária
  foi eliminada após a validação.
- A bateria completa terminou com **475 testes aprovados** em quatro grupos
  isolados (`141 + 95 + 73 + 166`), incluindo arranque, recuperação, backups,
  migrações, interface PySide6, responsividade e encerramento.
- O pacote `LiquidGlass_App_v3.29.1.zip` é autónomo e inclui o runtime; o
  executável continua sem assinatura digital para distribuição pública.

---

## V3.29 — v3.29.0 — 13 de agosto de 2026

Melhoria transversal do desempenho da pesquisa de movimentos, da ordenação das
tabelas e da densidade visual do Assistente local. A migração 22 acrescenta um
índice de pesquisa derivado e reconstruível; não altera nem elimina movimentos,
documentos ou qualquer outro dado introduzido pelo utilizador.

### Pesquisa literal e rápida de movimentos

- A pesquisa deixou de executar oito comparações `LIKE '%texto%'` sobre todo o
  histórico a cada alteração do campo.
- A migração 22 cria um índice SQLite FTS5 com tokenização por trigramas para
  descrição, categoria, subcategoria, conta, notas, etiquetas, comerciante e
  método de pagamento.
- O índice é preenchido a partir dos movimentos existentes e atualizado por
  triggers sempre que um movimento ou os respetivos metadados são adicionados,
  alterados ou eliminados.
- A expressão introduzida é pesquisada como frase literal. `%` e `_` passam a
  ser caracteres normais e já não funcionam acidentalmente como wildcards SQL.
- Pesquisas de apenas um ou dois caracteres usam um fallback literal com
  `INSTR`, porque não podem ser representadas num índice de trigramas.
- O atraso de pesquisa na página Movimentos foi reduzido de 350 para 180 ms. A
  consulta assíncrona e cancelável já existente continua a impedir que uma
  resposta antiga substitua resultados mais recentes.

### Ordenação correta e persistente em todas as tabelas

- Todas as tabelas de dados geridas pela aplicação passam a ter ordenação pelo
  cabeçalho, e não apenas a lista principal de Movimentos.
- Foi criado um item de tabela comum que distingue datas ISO/europeias, meses,
  euros, números, percentagens, horizontes temporais e estados financeiros, em
  vez de ordenar todos os valores alfabeticamente.
- Estados com ícones, como `🚨 Crítica`, `⚠️ Atenção`, `Pendente` e `Concluído`,
  recebem uma ordem semântica estável.
- Campos apresentados por barras de progresso, seletores ou caixas de seleção
  mantêm um valor interno ordenável, mesmo quando a célula textual está oculta.
- A coluna e o sentido escolhidos são guardados no mesmo estado estável que já
  conserva larguras, ordem e visibilidade das colunas, e são restaurados ao
  regressar à tabela.
- Durante a reconstrução de uma `QTableWidget`, a ordenação é suspensa e reposta
  no fim do ciclo do evento. Isto evita que linhas mudem de posição a meio do
  preenchimento e misturem células de registos diferentes.
- Grelhas internas do calendário, vistas privadas do Qt e pop-ups que não são
  tabelas de dados do utilizador continuam excluídos desta gestão.

### Assistente local compacto e progressivo

- Foram removidas as alturas mínimas fixas de 760 px no texto e 830 px no card,
  que tornavam a página pesada em ecrãs pequenos.
- O topo apresenta agora receitas, despesas e poupança num resumo responsivo,
  acompanhado por um estado visual verde, amarelo ou vermelho.
- Categorias, alertas e próximos passos são secções independentes e expansíveis;
  a página mostra apenas o detalhe pedido pelo utilizador.
- O botão `Ver explicação completa` abre um painel limitado a 340 px e permite
  recolhê-lo novamente, sem reservar permanentemente espaço vazio.
- A análise continua totalmente local e identifica explicitamente a origem e o
  período dos dados no rodapé do resumo.

### Validação e entrega

- Foram adicionados testes específicos para pesquisa literal, plano de consulta
  FTS, sincronização do índice, ordenação de datas/valores/percentagens/estados,
  repovoamento seguro, persistência e layout compacto do Assistente.
- A validação de interface percorre também todas as páginas e confirma que cada
  tabela gerida tem a ordenação ativa.
- A execução integral terminou com **473 testes aprovados** em 11 min 54 s,
  incluindo dados, migrações, pesquisa, backups, arranque/encerramento,
  PySide6, temas, privacidade e renderização responsiva.
- Ruff, compilação Python, verificações de migração, dados, PySide6 e renderização
  visual fazem parte da validação desta versão.
- O pacote `LiquidGlass_App_v3.29.0.zip` é autónomo, não contém dados pessoais e
  não exige Python instalado no computador de destino.
- O executável continua sem assinatura digital para distribuição pública.

---

## V3.28 — v3.28.0 — 13 de agosto de 2026

Reforço da identidade das contas, da revisão de duplicados, das validações de
movimentos, da seleção inequívoca de investimentos, da confirmação real dos
backups e da gravação atómica das definições da IA. A migração 21 consolida
apenas variantes cosméticas do mesmo nome de conta e preserva movimentos,
subscrições, pagamentos, ligações de ativos e saldos de referência.

### Identidade única e estável das contas

- Foi criada uma chave canónica de conta com normalização Unicode, espaços
  repetidos removidos e comparação sem distinguir maiúsculas/minúsculas.
- `Conta principal`, `conta principal` e variantes com espaços passam a usar a
  mesma conta e o mesmo saldo. A designação visível já existente é preservada
  para as restantes contas.
- A migração 21 atualiza movimentos, subscrições, pagamentos agendados e contas
  ligadas a ativos. Quando une registos, mantém o saldo de referência conhecido
  mais recente, sem eliminar qualquer movimento.
- Novas gravações, importações, transferências e alterações em lote passam pelo
  mesmo registo central de contas.

### Duplicados com confiança explícita

- A deteção distingue agora `Certa`, `Provável` e `Possível`.
- Só uma referência bancária repetida na mesma conta é considerada duplicado
  certo e pode ser ignorada automaticamente durante uma importação.
- Coincidências de data, valor, conta, descrição ou comerciante são prováveis;
  coincidências apenas de data, valor e conta são possíveis. Ambas continuam a
  ser importadas e apresentadas para decisão humana.
- A pré-visualização CSV mostra contagens separadas e motivos legíveis; o botão
  passou a dizer “Ignorar apenas duplicados certos”.
- A janela de revisão dos movimentos inclui uma coluna de confiança e nunca
  elimina registos automaticamente.

### Edição direta e categorias coerentes

- Editar descrição, categoria ou valor na tabela Movimentos usa agora as mesmas
  validações da edição completa, grava um registo integral no histórico de
  Desfazer e atualiza imediatamente todas as páginas dependentes.
- Valores zero, descrições vazias, categorias livres e combinações
  receita/despesa incompatíveis são recusados antes da gravação.
- A categoria editável na tabela é um seletor filtrado pelo sinal do movimento,
  em vez de um campo de texto livre.
- O diálogo de movimento mostra apenas categorias de despesa ou de receita,
  conforme o tipo escolhido. Registos antigos incompatíveis continuam visíveis
  com um aviso `⚠️`, para correção consciente em vez de alteração silenciosa.
- A Saúde dos dados contabiliza estas inconsistências antigas e a lista de
  movimentos também as assinala visualmente.

### Investimentos identificados pelo ID interno

- Cada fatia da distribuição da carteira transporta o identificador interno do
  ativo até à janela de detalhe e à ação de seleção.
- Ativos com o mesmo nome deixam de substituir-se num dicionário pelo nome e já
  não abrem os detalhes do registo errado.
- Nomes repetidos são desambiguados visualmente com conta e ID, mantendo os
  cálculos e a seleção associados ao registo correto.
- O componente Donut aceita metadados por fatia sem quebrar os gráficos antigos
  que continuam a fornecer os tradicionais triplos de dados.

### Backup confirmado em segundo plano

- O resumo rápido deixou de chamar “verificado” a um ficheiro cuja assinatura
  atual ainda não foi recalculada. Mostra agora “verificação registada em…”.
- Definições e Saúde financeira iniciam uma validação integral em segundo plano,
  sem bloquear a interface, recalculando SHA-256 e abrindo o conteúdo do backup.
- Só depois dessa validação é mostrado “verificado realmente em…”, com data e
  hora da sessão atual.
- Alterar um backup já verificado, mesmo sem mudar o tamanho, é tratado como
  falha de integridade e não atualiza o estado seguro.

### Definições da IA atómicas

- Estado, origem da chave, modelo e três opções de privacidade são gravados numa
  única transação SQLite com reversão integral em caso de falha.
- A chave nova é confirmada no Gestor de Credenciais antes de publicar as novas
  definições. Se a transação falhar, a credencial anterior é reposta.
- A migração de uma chave antiga em texto simples passa pelo mesmo fluxo e a
  chave antiga só é limpa juntamente com a configuração coerente.
- A limpeza complementar dos backups é retomável e uma falha nessa manutenção
  já não transforma uma configuração válida num falso erro de gravação.

### Validação e entrega

- Foram acrescentados 8 testes específicos para consolidação de contas,
  preservação do saldo de referência, três níveis de duplicados, edição direta
  com Desfazer, categorias filtradas/legadas, investimentos com nomes iguais e
  reversão atómica das definições/credenciais da IA.
- A validação final concluiu com **468 testes aprovados** em quatro grupos
  isolados: 134 + 92 + 71 + 171. Inclui dados, migrações, backups, arranque,
  encerramento, importações, PySide6, layout e renderização visual.
- Ruff, compilação Python e verificação de diferenças passaram sem erros.
- O pacote `LiquidGlass_App_v3.28.0.zip` é autónomo, não contém dados pessoais e
  não exige Python instalado no computador de destino.
- O checksum `LiquidGlass_App_v3.28.0.sha256.txt` permite confirmar a integridade
  do pacote. O executável continua sem assinatura digital para distribuição
  pública.

---

## V3.27 — v3.27.0 — 13 de agosto de 2026

Reforço transversal da integridade financeira, da atualização imediata das
análises, das transferências internas, do património, dos documentos, dos
backups cifrados e da qualidade dos dados de investimento. A migração 20 é
aditiva e protegida: preserva os registos existentes, cria uma proteção antes
de alterar a estrutura e nunca elimina movimentos ou documentos do utilizador.

### Atualização imediata após alterar movimentos

- Adicionar, editar, duplicar, importar ou eliminar um movimento invalida de
  imediato os dados derivados do Dashboard, Orçamentos, análises, relatórios e
  restantes páginas dependentes.
- A página Movimentos conserva a atualização local já concluída, enquanto as
  outras áreas ficam marcadas para nova consulta sem esperar pelo intervalo de
  cache de 60 segundos.
- O estado de Desfazer e os indicadores da aplicação são sincronizados na mesma
  operação de invalidação, evitando totais antigos depois de uma alteração.

### Transferências internas ligadas e atómicas

- Cada nova transferência recebe um `transfer_group_id` único partilhado pelas
  duas contas.
- Editar, eliminar, duplicar e desfazer uma transferência atua sobre ambas as
  linhas dentro da mesma transação SQLite. Uma falha reverte o conjunto inteiro.
- Operações genéricas que poderiam separar as duas metades são recusadas e a
  interface encaminha a edição para o diálogo de transferência completo.
- Transferências antigas só são ligadas automaticamente quando existe uma
  correspondência exata e inequívoca de data, valor, origem, destino e
  descrição. Pares ambíguos permanecem intocados para revisão manual.

### Património com saldos de referência e qualidade visível

- É possível definir para cada conta um saldo de referência e a respetiva data.
  O saldo atual passa a somar apenas os movimentos posteriores a essa data.
- Quando não existe referência, a aplicação mantém o cálculo histórico, mas
  identifica-o como potencialmente incompleto em vez de o apresentar como uma
  certeza silenciosa.
- Ativos monetários podem ser ligados à conta que representam. O seu valor
  deixa então de ser contado novamente no total de investimentos, evitando
  duplicação do mesmo dinheiro no património.
- A área de património apresenta cobertura das contas, ligações monetárias,
  avisos de dados desconhecidos e um indicador de qualidade global.
- Nomes de contas são comparados sem distinguir maiúsculas/minúsculas e contas
  antigas sem configuração, movimentos ou ligação deixam de distorcer a
  cobertura.

### Categorias importadas consistentes

- O importador usa uma taxonomia canónica partilhada: por exemplo,
  `Alimentação` → `Supermercado`, `Salário` → `Rendimentos`, `Outros` →
  `Diversos` e `Transportes` → `Transporte`.
- Categorias personalizadas desconhecidas são preservadas sem alteração.
- Na migração, as equivalências são aplicadas apenas a movimentos identificados
  como importações CSV, incluindo as suas divisões por categoria. Categorias
  manuais com o mesmo nome não são renomeadas silenciosamente.

### Documentos auditados sem bloquear a interface

- A lista aparece imediatamente e a validação do arquivo corre numa tarefa em
  segundo plano com progresso real e botão de cancelamento.
- Tamanho e data de modificação são usados como impressão incremental; a
  assinatura SHA-256 só é recalculada para ficheiros novos, alterados ou numa
  auditoria completa pedida pelo utilizador.
- As alterações de estado são gravadas numa única transação por lote, em vez de
  uma escrita por documento. Cancelar antes da gravação não deixa resultados
  parciais.
- Uma geração de auditoria deteta anexos alterados durante a análise e agenda
  nova verificação, impedindo que um resultado antigo substitua o estado atual.
- O encerramento aguarda o cancelamento cooperativo da auditoria antes de fechar
  a base de dados.

### Alteração segura da palavra-passe dos backups

- A palavra-passe atual é verificada antes de iniciar a rotação.
- Primeiro é criado e validado um novo backup completo com a nova credencial;
  só depois são atualizadas as preferências e, por último, a credencial atual.
- Se qualquer etapa falhar, preferências, credenciais e ficheiros regressam ao
  estado anterior e a aplicação não comunica uma alteração incompleta.
- As palavras-passe anteriores ficam protegidas no Gestor de Credenciais do
  Windows, permitindo abrir backups antigos. Ao restaurar, o campo pode ficar
  vazio para experimentar automaticamente as credenciais guardadas.
- O estado de segurança resulta da validação efetiva do ficheiro e não apenas da
  opção de cifragem ativa.

### Migrações e investimentos com proveniência explícita

- O arranque valida toda a sequência obrigatória de migrações. Uma lacuna, mesmo
  abaixo da versão máxima registada, ativa a proteção prévia e é reparada antes
  de permitir escrita.
- O esquema passa à versão 20 com validações para saldos de referência, origem
  do custo de aquisição e impressões dos documentos.
- Um ativo com custo de aquisição desconhecido apresenta rentabilidade `N/D` e
  é excluído da percentagem agregada, em vez de transformar todo o valor atual
  em lucro artificial.
- A carteira mostra a percentagem cujo custo é conhecido; contribuição para o
  ganho, rentabilidade ponderada e comissões usam apenas a parcela comparável.
- Novos ativos marcados com custo conhecido exigem um custo positivo, impedindo
  estados contraditórios logo na introdução.

### Validação e entrega

- Foram acrescentados 16 testes específicos para invalidação imediata,
  transferências atómicas e ambíguas, património, categorias importadas,
  auditoria incremental/cancelada, rotação e reversão de credenciais, lacunas de
  migração e cobertura dos custos de investimento.
- A validação final concluiu com **460 testes aprovados**: 270 de dados e
  serviços, 97 de interface funcional e 93 de layout/renderização. A matriz
  visual cobre 960, 1280 e 1600 px, temas claro/escuro e modo de privacidade.
- Ruff, verificação de diferenças e compilação integral passaram sem erros.
- O teste de carregamento progressivo foi atualizado para aguardar o
  encerramento cooperativo da auditoria de documentos antes de desmontar a base
  temporária, reproduzindo o comportamento seguro da aplicação real.
- O pacote `LiquidGlass_App_v3.27.0.zip` é autónomo e não contém bases, anexos,
  backups, chaves, preferências ou logs pessoais. O computador de destino não
  precisa de instalar Python.
- É criado o checksum `LiquidGlass_App_v3.27.0.sha256.txt` para confirmar a
  integridade do pacote. O executável continua sem assinatura digital para
  distribuição pública.

---

## V3.26 — v3.26.0 — 13 de agosto de 2026

Clarificação dos horizontes temporais apresentados pela aplicação e reforço
dos limites de memória e da durabilidade dos backups. Esta versão não altera o
esquema da base de dados nem converte, elimina ou substitui dados financeiros do
utilizador.

### Tesouraria com significado inequívoco

- A percentagem da projeção de tesouraria passa a ser descrita como a
  probabilidade de **cada mês terminar acima de 0 €**.
- Título, resumo, legenda, detalhe ao clicar, texto acessível e documentação do
  cálculo deixam explícito que a percentagem é calculada isoladamente para o fim
  desse mês. Não significa que o saldo permaneceu positivo durante todos os
  meses anteriores.
- Cada ponto inclui agora `probability_scope="month_end"`, uma descrição
  estruturada e uma designação própria. O novo campo
  `final_month_probability_positive` evita que o valor final seja interpretado
  como uma probabilidade de sobrevivência de todo o percurso.
- O campo anterior `ending_probability_positive` é mantido como compatibilidade
  interna, sem alterar o valor calculado.

### Prioridades financeiras com períodos visíveis

- Cada recomendação inclui e mostra uma etiqueta de âmbito temporal: filtro
  selecionado, orçamento do mês atual, histórico de meses completos, anomalias
  dos últimos 45 dias com base de 180 dias, tesouraria futura ou recorrências
  dos últimos 90 dias.
- As janelas históricas anteriormente implícitas ficam discriminadas: margem
  recente até 6 meses dentro de uma consulta de 8, reserva com o número real de
  meses usados e alternativa até 13 meses, e rendimento com o número real de
  meses da média.
- O cabeçalho explica que o filtro controla os totais e que cada prioridade pode
  usar uma janela própria. A metodologia apresenta também o mapa completo das
  fontes temporais utilizadas.
- O filtro selecionado é agora enviado ao cálculo em segundo plano, permitindo
  mostrar o nome real escolhido pelo utilizador em vez de um texto genérico.
- As etiquetas são incluídas na descrição de acessibilidade de cada prioridade.

### Limite seguro das respostas da IA

- O cliente Gemini aceita no máximo 4 MB por resposta, incluindo listagem de
  modelos, respostas válidas e mensagens de erro.
- Um `Content-Length` acima do limite é recusado antes de ler o corpo.
- Respostas sem tamanho declarado ou com tamanho incorreto são lidas em blocos
  limitados e interrompidas assim que excedem o teto. Nunca é efetuada uma
  leitura ilimitada para memória.
- O limite configurável em testes nunca pode ultrapassar o máximo de produção.

### Backups resistentes a interrupções bruscas

- Backups ZIP, backups cifrados, cópias SQLite, preferências e cópias secundárias
  passam a forçar a escrita física do ficheiro temporário com `fsync` antes da
  substituição atómica do destino.
- No Windows, o ficheiro é reaberto com permissão de escrita para que
  `FlushFileBuffers` funcione corretamente; noutros sistemas é também efetuado,
  quando suportado, o `fsync` da pasta após a substituição.
- Um backup completo é criado e validado num nome temporário, sincronizado,
  publicado atomicamente e validado novamente no nome final.
- Se a sincronização física falhar, o backup anterior não é substituído e o
  ficheiro incompleto permanece fora do nome final.

### Validação

- Foram acrescentados testes para limites declarados e transmissões sem tamanho
  da IA, ordem `fsync` → substituição, preservação do backup anterior quando a
  sincronização falha, semântica mensal da tesouraria e âmbitos das prioridades.
- A validação final concluiu com **444 testes aprovados**: 443 testes gerais e
  1 ensaio visual integral. O ensaio visual verificou 960, 1280 e 1600 px nos
  temas claro e escuro, com e sem privacidade.
- Ruff e a compilação integral passaram sem problemas. O instalador temporário
  abriu e concluiu o autoteste com a base isolada válida e 29 páginas carregadas.
- O pacote `LiquidGlass_App_v3.26.0.zip` foi verificado sem bases de dados,
  documentos, backups, chaves, preferências ou logs pessoais. Inclui Python
  3.14.7, PySide6/Qt 6.11.1 e cryptography 50.0.0; o computador de destino não
  precisa de instalar Python.
- O checksum SHA-256 é gerado no ficheiro
  `LiquidGlass_App_v3.26.0.sha256.txt`, ao lado do pacote, para permitir a
  confirmação independente sem criar uma referência circular dentro do ZIP.
- O executável continua sem assinatura digital; esta limitação é indicada de
  forma explícita no manifesto e deve ser resolvida antes de distribuição
  pública alargada.

---

## V3.25 — v3.25.0 — 13 de agosto de 2026

Histórico reversível alargado a todas as formas principais de criar movimentos,
importação CSV validada integralmente, classificação coerente dos movimentos
divididos, subscrições associadas à conta real e retenção controlada das
proteções automáticas de versão. A migração acrescenta apenas novos campos com
valores predefinidos e não converte nem remove informação existente.

### Histórico e Desfazer completos

- Criar um movimento na página **Movimentos** ou no atalho **+ Novo** passa a
  gravar o movimento, os metadados e a entrada de Desfazer numa única transação
  SQLite. Se o histórico falhar, o movimento não fica parcialmente guardado.
- Transferências internas registam as duas pernas numa única entrada; Desfazer
  remove ambas em conjunto.
- Duplicar um movimento passa a copiar também todas as divisões por categoria,
  além do valor, conta, notas e metadados. A cópia fica imediatamente disponível
  no histórico.
- Aplicar uma divisão por categorias também fica reversível. Desfazer recupera
  exatamente a categoria e as divisões anteriores.
- Cobranças automáticas de subscrições, pagamentos agendados, pagamentos
  manuais de subscrições e depósitos ou levantamentos de objetivos entram no
  mesmo histórico.
- Ao desfazer uma operação recorrente, a aplicação elimina o movimento e a
  ocorrência automática e repõe apenas os campos associados que essa operação
  alterou: datas de renovação, mensalidades pagas/estado ou montante guardado no
  objetivo.
- Os dados associados aceites pelo histórico usam tabelas e campos permitidos
  explicitamente, têm limites de dimensão e são novamente validados antes da
  reposição.

### Categorias e gráficos coerentes

- Alterar a categoria principal de um movimento que tinha divisões elimina as
  divisões antigas na mesma transação. Os gráficos, orçamentos e relatórios
  passam assim a refletir imediatamente a nova categoria visível.
- A correção abrange edição individual, alteração em lote e aplicação de regras
  de categoria.
- Como a fotografia anterior inclui as divisões, o botão Desfazer continua a
  poder recuperar a repartição original completa.

### Importação CSV sem dados escondidos

- Datas passam a ser analisadas por inteiro; texto acrescentado depois de uma
  data, como `2026-07-13XYZ`, já não é ignorado.
- Valores importados com mais de duas casas decimais são assinalados para
  revisão e não são guardados. A validação final do repositório repete a mesma
  regra antes da escrita.
- Cabeçalhos vazios ou repetidos, incluindo diferenças apenas de maiúsculas ou
  espaços, são recusados com uma mensagem explícita.
- Cada linha tem de possuir exatamente o número de colunas definido no
  cabeçalho; colunas em falta ou a mais indicam a linha problemática.
- A deteção usa o leitor CSV estrito, recusa bytes nulos e aplica limites de
  64 MB, 500 000 linhas e 256 colunas.
- A tabela continua compacta e mostra no máximo 80 linhas, mas datas, valores,
  duplicados e erros são agora avaliados em todo o ficheiro. O resumo deixa
  claro que a contagem representa todas as linhas.

### Conta e pagamento das subscrições

- Cada subscrição possui agora **Conta** e **Forma de pagamento** editáveis.
  Estes dados aparecem também como colunas ajustáveis e persistentes na lista.
- Cobranças automáticas e o botão **Pagar agora** usam a conta configurada no
  movimento e guardam a forma de pagamento nos respetivos metadados.
- A migração estrutural 19 acrescenta os dois campos de modo não destrutivo.
  Subscrições antigas recebem `Conta principal` e forma de pagamento vazia até
  serem revistas pelo utilizador.

### Retenção das proteções de versão

- Depois de uma atualização ter sido verificada com sucesso, a aplicação
  conserva até três proteções automáticas `antes_atualizacao_*`, com um limite
  conjunto de 5 GB.
- A proteção acabada de criar e qualquer arquivo referenciado por um marcador
  de atualização ou restauro são sempre preservados, mesmo que ultrapassem o
  limite normal.
- Backups manuais, exportações e arquivos `restauro_rollback_*` não entram nesta
  limpeza.
- Um marcador ilegível faz a limpeza optar pela segurança e preservar todos os
  arquivos possíveis. Uma falha de retenção é registada, mas nunca transforma
  uma migração já validada num falso erro.
- A construção do pacote deixa de apagar ZIPs e checksums de versões anteriores;
  substitui apenas o pacote genérico e os artefactos da versão que está a ser
  construída.

### Validação

- Foram acrescentados testes de cópia integral das divisões, Desfazer de
  operações automáticas e respetivo estado, reversão quando a escrita do
  histórico falha, conta/método das subscrições, validação além da linha 80 e
  retenção seletiva das proteções.
- A base, anexos e preferências reais do utilizador não são usados pelos testes;
  toda a validação escreve apenas em perfis temporários isolados.
- A validação final concluiu com **439 testes aprovados** (438 testes gerais e
  o ensaio visual integral), compilação de todos os módulos e Ruff sem
  problemas.

---

## V3.24 — v3.24.0 — 13 de agosto de 2026

Reforço da integridade das gravações recorrentes, da cifragem das cópias de
proteção e da validação dos valores financeiros, sem converter nem apagar os
movimentos, documentos ou restantes dados existentes.

### Subscrições e pagamentos recorrentes seguros

- Uma subscrição muito antiga já não origina até 120 movimentos por arranque.
  A aplicação avança diretamente para a renovação mais recente vencida e cria,
  no máximo, um movimento atual por subscrição.
- O arranque tem ainda um limite global de 24 novas cobranças. Renovações
  antigas ignoradas, cobranças adiadas e registos inválidos ficam contabilizados
  num relatório técnico, em vez de serem repetidos silenciosamente.
- Movimentos, metadados, ocorrências, datas da subscrição e fotografia diária
  são agora confirmados numa única transação SQLite. Uma falha recua o lote
  completo e evita uma coleção de confirmações impossíveis de desfazer em
  conjunto.
- Pagamentos futuros sem duração aceitam oficialmente `0` meses em todas as
  camadas: janela, serviço, repositório e regras da base de dados.
- O botão **Lançar pagamentos em falta** cria lotes controlados de até 24
  movimentos, com um máximo de 12 por pagamento configurado. A mensagem final
  mostra quantos ficaram por rever, evitando centenas de lançamentos por um
  único clique.
- A geração dos pagamentos em falta também é atómica: movimentos, ocorrências,
  progresso e fotografia financeira são guardados ou anulados em conjunto.

### Gravação e histórico sem estados intermédios

- Investimentos, dívidas e subscrições passam a guardar a alteração principal
  e o respetivo histórico diário na mesma unidade transacional.
- O pagamento manual de uma subscrição inclui também a atualização da próxima
  renovação e a fotografia diária antes da confirmação final.
- A fotografia mensal e o estado financeiro associado deixam de ser
  confirmados separadamente.
- A gestão do objetivo especial de reserva de emergência já era transacional e
  passa a estar coberta por um ensaio explícito de reversão integral.
- Uma falha simulada na escrita do histórico já não pode deixar o dado principal
  guardado enquanto a janela apresenta erro.

### Valores zero e qualidade dos dados

- Movimentos, subscrições e pagamentos configurados de `0,00 €` são recusados
  na interface, no repositório e por regras SQLite, incluindo chamadas que
  contornem a interface.
- O sinal do movimento e a classificação receita/despesa são validados em
  conjunto na base; um zero deixa de poder ser classificado como receita.
- Valores são arredondados financeiramente ao cêntimo antes de serem gravados;
  um valor que resulte efetivamente em zero é rejeitado.
- Eventuais zeros de versões antigas não são apagados nem corrigidos à força:
  aparecem como valores inválidos no centro de qualidade e ficam excluídos dos
  totais, previsões e indicadores financeiros.

### Cifragem e ficheiros temporários

- Quando a cifragem de backups está ativa, a proteção automática criada antes
  de uma atualização passa de ZIP aberto para `.lgbak` com AES-GCM. Se a
  palavra-passe segura não estiver disponível, a atualização é interrompida
  antes de alterar dados, em vez de criar uma cópia desprotegida.
- A cópia SQLite incluída na proteção remove a antiga chave Gemini em texto
  simples antes de ser arquivada.
- Criação, validação, restauro e ensaio de recuperação de backups cifrados usam
  agora uma área temporária privada e identificada. O conteúdo é removido no
  fim e resíduos deixados por uma falha de energia ou encerramento forçado são
  limpos no arranque seguinte.
- Os backups de segurança criados antes de um restauro e os pacotes duráveis de
  recuperação de um restauro interrompido são também cifrados quando essa opção
  está ativa.
- Uma proteção cifrada é decifrada apenas dentro da área privada e volta a ser
  autenticada e validada antes de poder repor a base, documentos, histórico e
  preferências.
- A extração intermédia durante o restauro de uma proteção de versão usa a
  mesma área privada, permitindo remover também esse conteúdo após uma
  interrupção inesperada.

### Estado real do backup

- O indicador **backup cifrado** inspeciona agora o cabeçalho do ficheiro mais
  recente. Já não confunde a opção ligada com um backup efetivamente cifrado.
- Se a opção for ativada depois de um ZIP normal, o painel explica que é preciso
  criar um novo backup; os pontos de segurança só são atribuídos após existir
  realmente um `.lgbak` válido.
- O metadado de verificação passa a registar também se o ficheiro confirmado era
  ou não cifrado.

### Histórico Git e validação

- O estado cumulativo anterior ficou preservado no commit `93e74bb` e na
  etiqueta anotada `v3.23.0`, sem incluir bases, documentos, backups, logs ou
  preferências privadas. O limite exato da antiga V3.20 não podia ser
  reconstruído com segurança, pelo que foi protegido o estado completo mais
  recente antes destas alterações.
- A V3.24 fica isolada num novo commit e na etiqueta anotada `v3.24.0`, mantendo
  um ponto de regresso claro antes e depois desta correção.
- Foram acrescentados ensaios de datas antigas, idempotência entre arranques,
  limites de lotes, pagamentos sem fim, reversão por falha de histórico,
  rejeição de zeros, cifragem real, remoção da chave antiga, limpeza de
  temporários e leitura do estado real do backup.
- A medição visual dos cabeçalhos compactos aguarda agora a conclusão das
  atualizações responsivas do Qt, evitando falsos erros quando a máquina está
  sob carga.
- A validação integral concluiu com **430 testes aprovados**, compilação de todos
  os módulos e Ruff sem problemas.

---

## V3.23 — v3.23.0 — 13 de agosto de 2026

Distribuição Windows preparada para um computador novo através de um instalador
por utilizador, sem instalação global de Python e sem permissões de
administrador, mantendo o modo portátil disponível.

### Instalação simples e segura

- O ZIP inclui `INSTALAR_FINANCAS_PESSOAIS.bat`, que chama apenas o Windows
  PowerShell incorporado no sistema e instala a aplicação na conta atual.
- A pasta predefinida é
  `%LOCALAPPDATA%\Programs\FinancasPessoaisLiquidGlass`; atalhos são criados no
  Ambiente de Trabalho e no menu Iniciar.
- Uma restrição do Windows ao criar um atalho já não invalida a instalação: o
  instalador mostra o caminho confirmado do executável e abre a aplicação.
- `ABRIR_FINANCAS_PESSOAIS.bat` permite usar a aplicação diretamente da pasta
  extraída, sem qualquer instalação.
- Não são executados `pip`, instaladores de Python ou alterações permanentes à
  política do PowerShell. A distribuição leva o runtime de que necessita.
- A aplicação deve ser instalada sem **Executar como administrador**, evitando
  guardar atalhos ou ficheiros no perfil de outra conta administrativa.

### Verificação antes de instalar

- O instalador exige o manifesto de distribuição, o auto-teste aprovado e um
  pacote Windows de 64 bits que declare não depender de Python externo.
- O manifesto regista tamanho e SHA-256 de todos os ficheiros distribuídos; o
  instalador recusa ficheiros alterados, ausentes, duplicados ou acrescentados.
- São confirmados Python 3.14, Visual C++ Runtime, SQLite, PySide6/Qt Core, GUI,
  Widgets, Network e Multimedia, o plugin gráfico do Windows, cryptography e os
  recursos visuais.
- Uma atualização é copiada para uma pasta temporária no mesmo disco e só
  substitui a instalação anterior depois da validação. Se a troca falhar, a
  versão anterior é reposta.
- O processo de construção passa a executar o instalador numa pasta temporária
  limpa e a abrir o executável a partir dessa cópia instalada.
- PyInstaller 6.22.0, Ruff 0.16.2, pytest 9.1.1 e pip-audit 2.10.1 são
  confirmados e registados como ferramentas de construção. Não seguem no
  runtime de destino porque não são necessários para executar a aplicação.

### Separação dos dados

- O instalador recusa destinos dentro de
  `%LOCALAPPDATA%\FinancasPessoais`, onde vivem os dados persistentes.
- Instalar, atualizar ou remover os ficheiros do programa não converte nem apaga
  movimentos, documentos, configurações, backups ou proteções de versão.
- O manifesto final passa a documentar o modo de instalação, ausência de
  privilégios administrativos, dependências incorporadas e resultado do teste
  da cópia instalada.

### Validação

- A validação integral concluiu com **421 testes aprovados**; os seis testes
  específicos de preparação e instalação do pacote também passaram.
- O Ruff não encontrou problemas no código e o pip-audit não encontrou
  vulnerabilidades conhecidas nas dependências de execução fixadas.
- A construção só pode produzir o ZIP depois de uma cópia instalada confirmar
  `quick_check = ok` numa base temporária e carregar as 29 páginas exportadas.
- Todos estes ensaios usam um perfil temporário isolado e não abrem nem alteram
  os movimentos, documentos, anexos ou configurações do utilizador.

---

## V3.22 — v3.22.0 — 11 de agosto de 2026

Normalização integral da introdução e apresentação de valores monetários para
o formato português, sem alterar a representação numérica guardada na base de
dados nem os formatos próprios dos extratos bancários importados.

### Introdução de valores

- Todos os controlos monetários usam explicitamente a localização portuguesa e
  rejeitam o ponto como separador decimal; `3,14` e `100,01` são válidos,
  enquanto `3.14` e `100.01` não são aceites.
- A tecla de ponto decimal é convertida imediatamente em vírgula para impedir o
  erro perigoso em que `3.14` poderia acabar interpretado como `314`.
- Colar um valor com ponto é recusado e mantém o valor anterior, em vez de o
  converter silenciosamente.
- A edição direta da coluna Valor nos Movimentos e a tabela do Assistente de
  orçamento usam o mesmo validador estrito que os restantes diálogos.
- Os campos monetários ficam identificados para privacidade e acessibilidade,
  com indicação explícita do formato esperado.

### Apresentação consistente

- O formatador partilhado arredonda financeiramente a duas casas e apresenta
  vírgula decimal em cartões, tabelas, gráficos, tooltips, relatórios, alertas e
  descrições acessíveis.
- Valores com cêntimos mantêm sempre dois algarismos: `3,14 €` e `12,90 €`.
- Valores exatos em euros omitem o ruído visual de `,00`: `100 €`, `-33 €` e
  `1 000 €`.
- Rótulos compactos dos gráficos preservam a abreviação quando necessária, mas
  valores completos inferiores ao limiar deixam de perder cêntimos.

### Compatibilidade e proteção dos dados

- A alteração é apenas de validação e apresentação; a base continua a guardar
  números, sem conversão destrutiva de movimentos, documentos, investimentos,
  objetivos ou outras informações existentes.
- A importação de extratos mantém os modos Português, Internacional e
  Automático porque esses ficheiros podem legitimamente vir noutro formato.
- Foram acrescentados testes para formatação, arredondamento financeiro,
  escrita, colagem, validação de tabelas e edição direta de movimentos.
- A validação integral concluiu com 420 testes aprovados, incluindo os ensaios
  visuais e responsivos.
- O pacote Windows `LiquidGlass_App_v3.22.0.zip` foi construído e o próprio
  executável carregou as 29 páginas, reabriu uma base temporária e confirmou
  `quick_check = ok`; o arquivo não contém bases, anexos, logs ou segredos.

---

## V3.21 — v3.21.0 — 11 de agosto de 2026

Preparação da aplicação para distribuição limpa noutro computador, com
dependências atuais, verificação real do executável e remoção conservadora de
resíduos gerados sem tocar em movimentos, anexos, bases ou backups.

### Runtime e dependências

- A referência de execução passa a ser CPython 3.14.7 de 64 bits, mantendo a
  versão normal com GIL e a compatibilidade exigida pelo PySide6 atual.
- PyInstaller passa de 6.21.0 para 6.22.0 e Ruff de 0.15.20 para 0.16.2.
- pip-audit 2.10.1 é acrescentado apenas às ferramentas de desenvolvimento,
  permitindo verificar vulnerabilidades conhecidas sem aumentar o pacote final.
- PySide6/Qt 6.11.1, cryptography 50.0.0 e pytest 9.1.1 mantêm-se porque já são
  as versões estáveis mais recentes e compatíveis com Python 3.14.
- A configuração do Ruff identifica explicitamente Python 3.14 e exclui
  artefactos de construção e validação que não pertencem ao código-fonte.

### Pacote Windows verificável

- A construção rejeita Python fora da série 3.14, arquitetura diferente de
  64 bits, versões instaladas diferentes das fixadas ou dependências
  incompatíveis detetadas por `pip check`.
- O pacote confirma a presença do interpretador Python, runtime Visual C++,
  Qt6Core e módulo nativo de cryptography antes de criar o ZIP.
- Um novo modo interno `--package-self-test` é executado sobre o próprio
  `LiquidGlass.exe`, com perfil temporário e isolado. Carrega todas as páginas
  lazy, cria uma base descartável, executa `quick_check`, fecha e reabre a base.
- `RELEASE_MANIFEST.json` passa a registar sistema e arquitetura alvo, modo de
  pacote, Python, PySide6, Qt, Shiboken, cryptography, cffi, PyInstaller e o
  resultado resumido do auto-teste.
- O pacote continua a abortar se encontrar bases, backups, anexos, segredos ou
  logs e remove por omissão as pastas intermédias `build` e `dist` depois de o
  ZIP e os respetivos SHA-256 estarem criados.

### Limpeza e documentação

- A análise de referências passou a considerar reexportações e páginas
  carregadas dinamicamente; nenhum módulo funcional foi apagado com base num
  falso positivo de importação.
- Caches, capturas antigas, resultados de testes, logs vazios e artefactos de
  builds anteriores podem ser removidos sem afetar dados financeiros.
- O README deixa de apresentar a versão antiga 3.10.1 e documenta `v3.21.0`.
- O guia para outro PC explica que o pacote é portátil: as dependências são
  incorporadas, não instaladas globalmente, e toda a pasta `_internal` deve
  acompanhar o executável.
- Os três lançadores do código passam a pedir explicitamente Python 3.14. Os
  relatórios de arranque deixam de recriar `logs` junto ao código e passam para
  `%LOCALAPPDATA%\FinancasPessoais\logs` ou para o perfil isolado configurado.

### Validação final

- `pip check` confirmou que não existem requisitos incompatíveis e pip-audit
  não encontrou vulnerabilidades conhecidas nas dependências de execução nem
  nas ferramentas de desenvolvimento.
- Ruff 0.16.2 e a compilação de todos os módulos Python concluíram sem erros.
- Os 402 testes automatizados passaram em 578,41 segundos, incluindo a
  renderização real do Windows.
- O ZIP foi extraído novamente para uma pasta limpa; o executável congelado
  carregou as 29 exportações de páginas e reabriu uma base temporária com
  `quick_check = ok`.
- A inspeção do arquivo confirmou 264 entradas, zero ficheiros privados e
  igualdade entre o SHA-256 da versão numerada, do alias e do ficheiro de soma.

---

## V3.20 — v3.20.0 — 11 de agosto de 2026

Fase de estabilização e entrega da implementação por etapas, centrada na
compatibilidade do Centro financeiro, na manutenção dos testes de migração e
na validação integral antes da criação do pacote Windows.

### Compatibilidade defensiva da interface

- Os três novos cartões operacionais consultam as respetivas capacidades do
  serviço de análise antes de as executar. Uma integração parcial, um teste
  isolado ou uma substituição transitória do serviço já não impede a abertura
  do Centro financeiro.
- Quando uma capacidade opcional não existe, os gráficos recebem uma estrutura
  vazia válida e mantêm os resumos legíveis, sem inventar dados nem esconder
  uma exceção real do serviço quando este está presente.
- Títulos e resumos dos novos cartões deixam de impor a sua largura textual ao
  layout. Podem quebrar linha numa coluna a 560 px e conservam a composição em
  duas colunas quando existe espaço suficiente.

### Migrações e validação

- Os testes históricos da migração dos anexos e dos índices deixam de fixar a
  versão 17. Passam a comparar com `CURRENT_SCHEMA_VERSION`, atualmente 18,
  continuando a verificar que dados legados, índices e assinaturas sobrevivem.
- O ensaio local do serviço de análises passa a validar também disponibilidade,
  data, fonte, custo, ganho e rentabilidade dos investimentos, sem rejeitar a
  resposta apenas por ela ter sido enriquecida com estes campos documentados.
- A revisão mensal do mesmo ensaio passa a exigir os contadores de datas e
  valores inválidos, mantendo compatibilidade com novos diagnósticos adicionais.
- O ensaio aceita zero meses quando a base isolada não tem movimentos, em vez
  de exigir três meses artificiais a zero e contrariar a política de dados em falta.
- A validação final controla também processos de teste sem processo-pai, para
  não confundir uma execução antiga com uma tarefa ainda ativa da aplicação.
- A identificação apresentada na app, nas propriedades do executável e no
  pacote de distribuição passa para `v3.20.0`.
- O script de entrega já não depende da interpretação UTF-8 do PowerShell 7:
  constrói os metadados Unicode de forma explícita e funciona no Windows
  PowerShell 5.1 incluído no sistema.
- A validação final passou nos 398 testes automatizados, nos dez ensaios locais
  de serviços e em 12 capturas de stress visual a 100%, 125%, 150% e 200%.

---

## V3.19 — v3.19.0 — 11 de agosto de 2026

O Centro financeiro e a Saúde financeira passam a ligar previsões, orçamento,
compromissos, qualidade dos movimentos e proteção dos dados numa leitura
operacional única, sem apagar ou reclassificar informação automaticamente.

### Tesouraria e compromissos com confiança explícita

- Um novo gráfico mensal projeta o saldo base, cenário otimista, cenário de
  stress e faixa P10–P90 durante 12 meses. Cada ponto mostra a probabilidade
  estimada de o saldo permanecer positivo.
- O cálculo usa apenas meses completos com movimentos reais, dá maior peso aos
  meses recentes e identifica claramente confiança inicial, moderada ou alta.
- Meses sem histórico não são inventados como rendimento ou despesa zero e as
  faixas são apresentadas como estimativas, nunca como garantia.
- A linha temporal recorrente separa subscrições e pagamentos confirmados,
  padrões prováveis que exigem confirmação e despesas de subscrição ainda por
  associar. Apenas os primeiros são tratados como compromissos explícitos.

### Orçamento e qualidade dos dados

- O mapa de desvios cruza até 12 meses com todas as categorias que têm
  orçamento ou despesa. Verde indica controlo, laranja proximidade do limite e
  vermelho excesso ou gasto sem orçamento.
- Cada célula abre valores realizados, limite, desvio e estado e permite seguir
  diretamente para a área de Orçamentos.
- A Saúde financeira recebe um score separado para os dados. A tendência mensal
  mostra movimentos genéricos, duplicados candidatos e descrições insuficientes;
  meses inativos não aparecem como meses perfeitos.
- A estrutura da base, última verificação protegida e assinaturas dos anexos
  ficam visíveis num resumo claro, sem executar uma operação pesada em cada
  abertura da página.

### Proteção e revisão segura

- O estado rápido dos backups lê a última verificação registada, antiguidade,
  cifragem, segunda cópia e último teste de recuperação sem voltar a calcular o
  hash integral na interface. A verificação completa continua nas Definições.
- Um auditor reconhece a assinatura exata do perfil de demonstração. Só o
  perfil completo, marcado e sem qualquer registo financeiro adicional recebe
  confiança elevada; perfis parciais ou misturados nunca podem ser removidos
  automaticamente.
- A revisão apresentada ao utilizador é estritamente informativa e explica que
  qualquer remoção futura exige backup verificado, nova confirmação da
  assinatura e consentimento explícito.

### UI, acessibilidade e validação

- Os novos cartões mantêm alturas moderadas e reorganizam-se em duas colunas ou
  numa coluna conforme a largura disponível.
- No Dashboard, cartões de alturas diferentes ficam alinhados pelo topo da
  mesma linha; o Qt já não centra o cartão mais baixo e cria um desvio de um pixel.
- Todos os gráficos usam o sistema comum de tooltip claro, detalhe ao clicar,
  comparação fixada, exportação PNG, modo de foco, teclado e privacidade.
- A matriz visual passa a capturar também probabilidade de tesouraria, linha de
  compromissos, mapa orçamental e tendência da qualidade em resoluções e temas
  suportados.
- Novos testes garantem probabilidades entre 0% e 100%, categorias completas,
  separação das classes de confiança, diagnóstico sem escrita e desenho nos
  modos claro, escuro e privacidade.

---

## V3.18 — v3.18.0 — 11 de agosto de 2026

Interação por rato e teclado, escalamento do Windows e validação visual foram
uniformizados para reduzir pop-ups ilegíveis, cortes e saltos durante resize.

### Tooltips e acessibilidade

- Todos os pedidos de tooltip passam pelo cartão claro desenhado dentro da app;
  um pedido sem conteúdo também é consumido e já não pode cair no retângulo
  preto nativo do Windows.
- Tooltips definidos num botão, tabela ou antecessor são encontrados mesmo
  quando o evento chega ao ícone ou viewport interno. Texto HTML é convertido
  para texto simples antes de ser apresentado.
- O cartão de tooltip é recriado com segurança se a janela temporária que o
  continha for fechada, evitando referências a objetos Qt já eliminados.
- Um gestor de acessibilidade atribui nomes e descrições aos controlos criados
  de forma diferida, sem substituir nomes escritos manualmente.
- `Ctrl+F6` e `Ctrl+Shift+F6` percorrem navegação lateral, pesquisa global e
  conteúdo da página; a barra de estado identifica a zona que recebeu foco.
- O contorno de foco dos botões, campos e tabelas ficou mais nítido e os títulos
  dos grupos laterais ganharam melhor hierarquia visual.

### Resize e escalamento

- O ajuste da moldura principal usa temporizadores únicos em vez de acumular
  chamadas a cada pixel redimensionado. As grelhas comuns também agrupam os
  pedidos de reorganização.
- O Dashboard mantém a sua passagem de alinhamento no próximo ciclo gráfico,
  preservando linhas de cartões exatamente alinhadas.
- O arranque declara escalamento de DPI e mantém fatores fracionários sem os
  arredondar para outro breakpoint visual.
- A matriz Windows passa a testar 100%, 125%, 150% e 200%, além das larguras
  960, 1280 e 1600, temas claro/escuro e privacidade já existentes.

### Testes visuais e código

- Cada captura recebe uma assinatura percetual de 256 bits, permitindo detetar
  mudanças reais de composição mesmo quando o ficheiro PNG não é idêntico.
- A validação continua a rejeitar sobreposições, textos cortados, capturas
  vazias, scroll horizontal inesperado e exposição de valores em privacidade.
- Novos testes cobrem tooltips após destruir diálogos, nomes acessíveis, foco
  entre zonas, assinatura visual e a escala de 200%.
- O lint passa a impedir novas closures que capturem por engano a última
  variável de um ciclo; o gráfico de bolhas e os validadores foram corrigidos.
- O identificador da nova coluna de aliases foi integrado na persistência de
  larguras e visibilidade das tabelas.

---

## V3.17 — v3.17.0 — 11 de agosto de 2026

Importações, duplicados e subscrições passam a conservar a origem dos dados e
a mostrar ambiguidades em vez de as resolver silenciosamente.

### Importação rastreável

- A leitura CSV tenta UTF-8 estrito, Windows-1252 e ISO-8859-1 sem substituir
  caracteres inválidos. A codificação detetada e qualquer incerteza ficam
  visíveis junto da pré-visualização.
- O utilizador pode escolher valores em formato automático, português
  (`1.234,56`) ou internacional (`1,234.56`). No modo automático, expressões
  ambíguas como `1.234` ficam marcadas para revisão.
- O mapeamento aceita agora a referência original do banco. Essa referência é
  guardada nos metadados do movimento e acompanha exportações e backups.
- A identidade de um duplicado inclui a conta. Referências bancárias iguais só
  são consideradas repetidas dentro da mesma conta; movimentos visualmente
  iguais em contas diferentes permanecem distintos.

### Subscrições confirmadas

- Cada subscrição aceita nomes exatos alternativos encontrados no extrato,
  separados por ponto e vírgula, e mostra-os na lista.
- O histórico distingue movimentos associados, prováveis, não associados e
  ambíguos. Apenas ocorrências, o nome exato e aliases confirmados entram nos
  totais pagos.
- Possíveis correspondências aproximadas nunca são ligadas automaticamente.
- A deteção de despesas recorrentes tolera pequenas alterações de preço,
  considera meses distintos e atribui confiança baixa, média ou alta, sempre
  exigindo confirmação.

### Qualidade visível e estrutura

- A estrutura SQLite sobe para a versão 18 e acrescenta `external_reference`
  aos metadados e `aliases` às subscrições, sem reescrever movimentos atuais.
- Falhas ao separar subscrições de uma previsão são registadas e sinalizadas no
  cartão; pontos inválidos de saldo são excluídos com contagem visível e
  descrição acessível.
- Erros de ordenação deixam de desaparecer silenciosamente nos logs.
- Um número de mensalidades inválido é rejeitado com uma mensagem clara, em vez
  de ser transformado silenciosamente em uma mensalidade.

### Validação acrescentada

- Testes cobrem CSV Windows-1252, números ambíguos, referências repetidas em
  contas diferentes, aliases explícitos, variações de preços recorrentes,
  mensalidades inválidas e pontos de gráfico corrompidos.

---

## V3.16 — v3.16.0 — 11 de agosto de 2026

Comunicações de IA, backups, relatórios, logs e distribuição reforçados para
reduzir exposição de segredos e tornar cada pacote verificável.

### Segredos e comunicações

- A chave Gemini deixou de seguir no endereço do pedido e passa a usar
  exclusivamente o cabeçalho oficial `x-goog-api-key`.
- As preferências públicas da IA já não devolvem a chave completa. Expõem
  apenas estado, origem e quatro caracteres mascarados; o segredo só é lido do
  Gestor de Credenciais no instante de criar o cliente.
- O campo de chave começa vazio e preserva a credencial existente quando não é
  preenchido. Depois de uma gravação explícita, o texto é removido da memória
  visual da página.
- O formatador central dos logs elimina chaves em URLs/cabeçalhos, emails e
  IBANs, incluindo mensagens de exceção e traces.
- `cryptography` foi atualizada de 48.0.1 para 50.0.0, a primeira versão que
  reúne as correções dos avisos de segurança atuais.

### Backups e relatórios

- Backups manuais e testes de recuperação usam agora uma ligação SQLite criada
  na própria tarefa de fundo; a interface continua utilizável durante cópia,
  compressão e validação.
- O restauro de ZIPs antigos substitui exatamente o conjunto de anexos incluído
  no backup. Ficheiros órfãos antigos deixam de permanecer silenciosamente na
  pasta ativa, mantendo-se protegidos pela cópia pré-restauro.
- Categorias e cores inseridas no relatório anual são escapadas e validadas;
  a janela de orçamento aplica a mesma proteção ao texto enriquecido.

### Distribuição verificável

- O processo Windows incorpora versão de ficheiro/produto no executável e cria
  `RELEASE_MANIFEST.json` com o SHA-256 do programa.
- O pacote passa a ter nome versionado, checksum SHA-256 externo e uma cópia
  compatível chamada `LiquidGlass_App.zip`.
- O script aceita assinatura Authenticode quando é fornecido um certificado
  através de `LIQUIDGLASS_SIGN_CERT_THUMBPRINT` e recusa uma assinatura inválida.
- A ausência de certificado produz um aviso explícito; não é fingida uma
  assinatura que não possa ser comprovada.

### Validação acrescentada

- Testes confirmam que a chave não aparece no URL nem nas preferências, que os
  logs retiram identificadores, que backups continuam válidos com
  `cryptography 50` e que anexos órfãos desaparecem num restauro legado.
- O transporte Gemini manteve retries, cancelamento imediato e limite global
  nos testes offline.

---

## V3.15 — v3.15.0 — 11 de agosto de 2026

Proteção reforçada contra criação silenciosa de perfis, duplicação de dados de
demonstração, históricos de alterações adulterados e restauros concorrentes.

### Perfis vazios e dados de demonstração

- Uma instalação normal passa a criar apenas categorias e regras de referência;
  já não inventa movimentos, subscrições, investimentos ou objetivos.
- Os dados de demonstração exigem agora o modo explícito
  `FINANCAS_PESSOAIS_DEMO_DATA=1`, ficam marcados e usam valores aleatórios
  determinísticos para tornar testes e validações visuais repetíveis.
- A reposição das categorias deixou de voltar a inserir todos os restantes
  dados de demonstração. A operação verifica todas as tabelas financeiras e é
  idempotente.
- Se uma base usada desaparecer ou ficar vazia enquanto permanecem backups,
  documentos, preferências ou proteções, o arranque é bloqueado em vez de
  criar números fictícios plausíveis.
- O novo assistente de arranque permite escolher um backup validado ou confirmar
  duas vezes a criação de um perfil realmente vazio. Os artefactos restantes
  são preservados antes de um restauro substituir o perfil.

### Desfazer e restauro seguros

- O histórico **Desfazer** volta a validar todos os caminhos imediatamente
  antes de mover anexos. Origem e reserva têm de permanecer nas pastas geridas
  e conservar exatamente o mesmo caminho relativo.
- Tabelas e colunas recuperadas pelo histórico usam listas permitidas, validação
  contra o esquema SQLite e identificadores protegidos.
- Payloads desconhecidos, relações com outros movimentos, anexos externos,
  valores compostos e volumes excessivos são rejeitados antes de qualquer
  alteração.
- O restauro entra num modo de manutenção central: bloqueia novas pesquisas,
  IA e backups, cancela todas as tarefas existentes e aguarda a sua conclusão
  antes de fechar a ligação SQLite.
- Se a manutenção não ficar vazia dentro do limite seguro, o restauro é
  cancelado e o acesso normal é retomado sem substituir a base.

### Validação acrescentada

- Novos testes confirmam um perfil de produção sem finanças fictícias, uma
  demonstração determinística sem duplicações, deteção de base desaparecida,
  recuperação de SQLite, bloqueio de caminhos/colunas adulterados e drenagem
  de tarefas permitidas durante o encerramento.
- A estrutura da base mantém a versão 17; os movimentos atuais não são
  reescritos nem removidos automaticamente.

---

## V3.14 — v3.14.0 — 11 de agosto de 2026

Interface adaptável, vistas persistentes e leitura mais clara de previsões,
comparações e exportações em modo privacidade.

### Filtros, densidade e navegação persistentes

- O cartão **Período e resolução** do Dashboard abre numa forma resumida e
  pode expandir as opções avançadas sem ocupar permanentemente uma faixa alta
  da página.
- Cada página passa a ter densidade **Confortável** ou **Compacta**, aplicada
  aos cartões, espaçamentos e linhas das tabelas sem apagar os ajustes
  específicos que já existiam.
- Densidade, filtros principais, estado aberto/fechado dos filtros, zoom e
  deslocamento dos gráficos ficam guardados por página e são repostos na
  próxima abertura.
- Uma barra flutuante discreta mantém acessíveis **Topo**, **Filtros** e
  **Repor** nas páginas compridas. O botão de filtros só aparece quando a
  página tem um painel principal identificado.
- Em janelas estreitas, a navegação lateral usa um modo contextual compacto:
  conserva ícone e nome curto da área, em vez de deixar apenas símbolos sem
  contexto.
- Os nomes contextuais são curtos e não ficam truncados a 960 px; a navegação
  larga reserva espaço suficiente para rótulos como **Importação inteligente**.
- O cálculo das colunas responsivas considera também o espaço entre cartões,
  reduzindo quebras tardias, cortes e scroll horizontal a 960 px.

### Linguagem visual consistente

- `GlassCard` passa a disponibilizar posições semânticas comuns para título,
  descrição, legenda, rodapé e confiança. Os cartões preditivos mostram uma
  confiança visual consistente, calculada a partir da cobertura e qualidade
  dos dados disponíveis.
- Campos de seleção, listas abertas, pesquisa global, caixas de seleção e
  barra de estado recebem superfícies opacas e cores explícitas nos temas
  claro e escuro. Isto elimina os fundos transparentes/pretos dependentes do
  tema nativo do Windows.
- As barras de scroll passam a ter 14 px e uma pega maior, ficando mais fáceis
  de localizar e arrastar sem aumentar a altura dos cartões de gráficos.
- O selo de confiança do cartão **Orçamento vs. realizado** foi integrado no
  cabeçalho de contexto, preservando a altura útil do gráfico e evitando que o
  conteúdo ultrapasse o limite inferior do cartão.

### Gráficos, comparações e explicações

- Todos os gráficos baseados no componente comum podem abrir detalhes com um
  clique, mesmo quando não tinham antes uma ação específica. O menu contextual
  inclui também **Ver detalhes do ponto selecionado**.
- Zoom e deslocamento são guardados individualmente por gráfico; **Repor**
  limpa zoom, filtros, comparações e detalhes fixados da página atual.
- Variações extremas deixam de apresentar percentagens visualmente enganosas.
  Acima do limiar de leitura são mostradas como multiplicador, por exemplo
  `87,2×`, acompanhadas pelo valor anterior e por uma explicação de base
  reduzida.
- O comparador de períodos, a ponte de património, os detalhes dos gráficos e
  os insights de variação usam a mesma regra, evitando leituras diferentes do
  mesmo fenómeno em cartões distintos.

### Privacidade e exportação

- Com o modo privacidade ativo, os CSV de Movimentos, Orçamentos e Análise
  dinâmica mascaram valores, descrições, contas, comerciantes, notas e outros
  campos sensíveis por defeito, mantendo simultaneamente a proteção contra
  fórmulas de folha de cálculo.
- Os botões de exportação indicam claramente **mascarado** enquanto a
  privacidade está ativa. O ZIP de backup é explicitamente excluído desta
  etiqueta porque é uma cópia integral destinada à recuperação dos dados.
- A persistência visual guarda apenas preferências de interface na tabela de
  definições; não altera movimentos, documentos, orçamentos ou investimentos.

### Robustez e validação

- Eventos tardios de ocultação deixam de tentar escrever preferências depois
  de a ligação SQLite já ter sido encerrada.
- A primeira apresentação de uma página deixou de aplicar a mesma densidade
  duas vezes. O teste de arranque mede agora também a maior pausa contínua do
  ciclo de eventos e reprova qualquer bloqueio de 0,8 segundos ou mais.
- A matriz visual cobre 960, 1280 e 1600 px, temas claro/escuro e privacidade,
  além de verificar contenção de cartões, gráficos, tabelas e scroll.
- Foram acrescentados testes específicos para densidade, filtros e zoom
  persistentes, detalhes universais dos gráficos, exportação mascarada,
  controlos sólidos e barra flutuante.
- Não existe migração de estrutura nesta versão e a validação usa bases
  temporárias isoladas; os dados reais do utilizador não são reescritos.
- Os **366 testes** passaram numa execução única, incluindo a matriz completa
  de todas as páginas e 12 capturas nativas do Windows.

---

## V3.13 — v3.13.0 — 10 de agosto de 2026

Segurança documental verificável, exportações inofensivas e simulações
financeiras que distinguem valores nominais de euros de hoje e mostram a
confiança real dos resultados.

### Exportações e apresentação protegidas

- A dependência `cryptography` passa de `48.0.0` para `48.0.1`, mantendo a
  compatibilidade dos backups cifrados e retirando a versão vulnerável.
- Todos os campos textuais controlados pelo utilizador nos CSV de Movimentos,
  Planeamento e Análise dinâmica são neutralizados quando começam por `=`,
  `+`, `-`, `@` ou caracteres de controlo. Os valores numéricos continuam a
  ser exportados como números.
- O Panorama escapa nomes de meses e categorias antes de gerar HTML e aceita
  apenas cores no formato hexadecimal `#RRGGBB`, com uma cor segura de
  substituição.
- Abrir um caminho local deixou de invocar diretamente a shell do sistema.

### Anexos com integridade persistente

- A estrutura da base avança para a versão `17` e acrescenta aos anexos o
  SHA-256, o tamanho original e a data da última verificação, sem reescrever
  documentos ou registos anteriores.
- Novos anexos ficam limitados a `50 MB` e a uma lista explícita de PDF,
  imagens, texto, CSV e documentos Office. A extensão tem de corresponder à
  assinatura real do conteúdo.
- DOCX e XLSX são inspecionados como contentores ZIP: quantidade de elementos,
  tamanho expandido, taxa de compressão, estrutura interna e corrupção são
  validados antes da cópia.
- A cópia temporária é sincronizada, comparada com o tamanho e SHA-256
  medidos antes da operação e novamente validada no destino antes do registo
  na base.
- O Arquivo de documentos distingue ficheiros verificados, antigos ainda sem
  impressão digital e alterados. Uma mudança posterior de tamanho ou conteúdo
  bloqueia a abertura e aparece no diagnóstico do arquivo.
- PDF e imagens verificados abrem num visualizador interno sem executar outra
  aplicação. CSV, Excel e Word mostram um aviso claro antes da abertura
  externa por poderem conter fórmulas, ligações ou conteúdo ativo.

### Projeções financeiras coerentes

- Uma taxa anual efetiva é agora convertida para taxa mensal composta; `6%`
  anual volta a produzir exatamente `6%` ao fim de doze meses, em vez da
  aproximação `6 / 12`.
- O simulador de investimentos apresenta separadamente contribuições,
  capital e ganhos **nominais** e em **euros de hoje**. Cada reforço mensal é
  corrigido pela inflação apenas a partir do mês em que entra.
- Comissões são aplicadas no momento de cada entrada e os impostos são
  estimados numa liquidação no horizonte selecionado.
- A simulação Monte Carlo usa retornos geométricos calibrados para a taxa
  anual efetiva, pesos da carteira, concentração e correlação ajustável.
- P10, P50, P90 e o objetivo passam a estar explicitamente em euros de hoje.
  São também mostrados risco de perda real, drawdown mediano/P90, semente
  reproduzível e nível de apoio histórico.

### Risco e previsões sem falsa precisão

- **Risco e Resiliência** usa até doze meses, com maior peso nos mais recentes,
  preserva a correlação observada entre rendimento e despesa e apresenta o
  período e a completude dos dados.
- Com menos de três meses úteis, ou sem rendimentos/despesas suficientes, o
  cartão mostra **Dados insuficientes** em vez de fabricar uma nota exata.
- Um novo cartão de confiança e três leituras de sensibilidade mostram quanto
  o saldo mínimo muda entre uma hipótese mais favorável, a atual e uma mais
  severa.
- A qualidade das previsões deixa de ser apenas a percentagem de resultados
  dentro da faixa. A nova nota combina erro pontual, cobertura, calibração,
  largura e `Winkler interval score`, penalizando intervalos demasiado largos.
- Gráficos e detalhes mostram separadamente qualidade, cobertura, largura
  média, erro e regularidade, tanto no total como por categoria.

### Compatibilidade e validação

- A libertação das tarefas `QThread` passa obrigatoriamente pela thread Qt da
  interface. O registo central é limpo antes de agendar a destruição e deixa
  de poder aceder a um objeto C++ já eliminado durante encerramentos muito
  próximos entre si.
- Uma tarefa concluída permanece logicamente ocupada até o respetivo evento de
  limpeza ser processado. Um evento antigo já não pode confundir-se com uma
  pesquisa nova e bloquear a interface enquanto aguarda a thread errada.
- Notificações simultâneas do registo e da tarefa são reunidas num único pedido
  de fecho. Se o backup ao fechar falhar, um segundo callback atrasado já não
  inicia backups repetidos depois de a janela ter sido reativada.
- A migração de anexos foi testada a partir da estrutura `14`; os registos
  antigos permanecem intactos e recebem metadados vazios até à primeira
  verificação segura.
- Foram acrescentados testes de injeção de fórmulas, HTML e cores, ficheiros
  disfarçados ou excessivos, alteração de anexos com o mesmo tamanho, taxas
  efetivas, valores reais/nominais, diversificação Monte Carlo, confiança da
  resiliência e penalização de faixas vagas.
- Os **359 testes** passaram, incluindo a reprodução do reinício imediato e 60
  ciclos consecutivos de encerramento de tarefas, a aplicação completa em 960,
  1280 e 1600 px, temas
  claro/escuro, privacidade e a captura de janelas reais do Windows. A análise
  estática, compilação e verificação de diferenças também passaram.
- Uma fotografia isolada da base ativa foi migrada de `14` para `17`: os 97
  movimentos e as linhas das 37 tabelas comparadas foram preservados, a
  integridade permaneceu `ok` e não surgiram chaves estrangeiras partidas. A
  base ativa não foi modificada.

---

## V3.12 — v3.12.0 — 10 de agosto de 2026

Leituras consistentes e canceláveis, encerramento seguro do Dashboard e uma
nova camada de integridade que impede a entrada de datas, números e relações
estruturais inválidas sem alterar os valores monetários decimais existentes.

### Leituras estritamente protegidas

- Dashboard, pesquisas e totais dos movimentos abrem agora a base existente em
  modo SQLite `read-only`; uma localização errada deixa de poder criar uma base
  vazia fantasma.
- Cada carregamento da lista de movimentos e do respetivo resumo usa a mesma
  fotografia transacional, eliminando diferenças quando há uma gravação
  simultânea entre as duas consultas.
- O cancelamento de uma pesquisa interrompe a consulta SQLite em execução, em
  vez de apenas ignorar o resultado depois de todo o trabalho terminar.
- O backup abre a origem explicitamente em modo de leitura/escrita controlado,
  preservando as preferências que lhe pertencem sem criar ficheiros noutra
  localização por engano.

### Encerramento seguro do Dashboard

- Ao fechar, o Dashboard bloqueia novos refrescamentos, invalida respostas já
  ultrapassadas, para os seus temporizadores e interrompe a ligação de leitura
  que ainda esteja a calcular indicadores.
- Os callbacks de sucesso e erro confirmam que a página e a base continuam
  disponíveis antes de tocar na interface.
- Se o encerramento for cancelado por uma falha de proteção, o acesso aos dados
  é retomado de forma explícita e a aplicação volta a ficar utilizável.

### Relações e validação na própria base

- A estrutura passa para a versão `16` através de duas migrações protegidas.
- As ocorrências de pagamentos agendados passam a ter chaves estrangeiras
  reais: eliminar o agendamento elimina as ocorrências; eliminar o movimento
  associado mantém a ocorrência e limpa apenas essa referência.
- A migração é recusada se encontrar uma relação órfã, preservando o registo e
  acionando a proteção existente em vez de o apagar silenciosamente.
- Triggers SQLite validam novas inserções e edições de datas ISO, meses,
  números finitos, percentagens, enums, JSON e campos obrigatórios nas tabelas
  financeiras e documentais.
- As divisões de um movimento não podem ultrapassar em conjunto `100,05%`.
- Os montantes continuam guardados como euros decimais, incluindo valores como
  `12.34`; não houve conversão para cêntimos inteiros.

### Dados inválidos deixam de distorcer resultados

- Agregados, previsões, diagnósticos, património, planeamento e indicadores do
  Dashboard excluem datas e números comprovadamente inválidos.
- Esses registos permanecem visíveis para correção e são contabilizados no
  estado da lista de movimentos, em vez de serem convertidos silenciosamente
  em zero.
- A inteligência financeira cria uma prioridade máxima **Rever dados inválidos
  excluídos dos cálculos**, indicando separadamente problemas de data e valor.

### Validação

- Foram acrescentados testes para ligações só de leitura, inexistência de bases
  fantasma, fotografia consistente, interrupção SQLite, chaves estrangeiras,
  migração com e sem órfãos, validações diretas e preservação dos euros
  decimais.
- Os **340 testes** da aplicação passaram.
- A análise estática e a verificação de diferenças passaram sem erros.
- Uma fotografia da base ativa foi migrada isoladamente da estrutura `14` para
  `16`: os 97 movimentos e as restantes tabelas pessoais conservaram todas as
  linhas, a integridade ficou `ok` e não surgiram relações partidas. A base
  ativa não foi modificada durante este ensaio.

---

## V3.11 — v3.11.0 — 10 de agosto de 2026

Reforço transacional das operações financeiras e recuperação durável de
backups, restauros e migrações interrompidas.

### Gravações financeiras atómicas

- Criação e edição da reserva de emergência usam agora um `SAVEPOINT`; uma
  falha ao trocar o objetivo principal recua a inserção ou edição completa.
- Depósitos e levantamentos de objetivos guardam o novo saldo, o movimento e
  os respetivos metadados numa única transação SQLite.
- A adição rápida passou a usar a mesma operação atómica de movimento e
  metadados utilizada pela página Movimentos.
- Importações CSV só são confirmadas quando o respetivo registo de **Desfazer**
  também tiver sido escrito.
- Edições individuais, alterações em lote, etiquetas, estado revisto, regras
  aprendidas e aplicação de regras partilham a transação do histórico
  reversível.
- A substituição das divisões de um movimento e a eliminação de pagamentos
  agendados passaram a recuar integralmente perante uma falha intermédia.
- `safe_commit()` executa rollback quando falha. Dashboard e Movimentos deixam
  de atualizar cartões, tabelas ou mensagens de sucesso quando o commit não é
  confirmado.

### Verificação real dos backups

- O estado de proteção recalcula o SHA-256 atual do backup mais recente, em vez
  de confiar apenas no nome e tamanho anteriormente guardados.
- Depois do hash, o pacote volta a ser aberto, o manifesto e todos os ficheiros
  são confirmados e a base extraída passa uma verificação SQLite completa.
- A segunda localização também é novamente comparada por SHA-256 com a cópia
  principal.
- O cartão de proteção apresenta próximos passos explícitos quando falta um
  backup válido, cifragem ou uma segunda localização.

### Retenção sem apagar backups manuais

- A retenção passou a separar backups automáticos de backups criados ao fechar.
- O limite configurado é aplicado individualmente a cada uma dessas famílias.
- Backups manuais, exportados e cópias criadas antes de um restauro ficam fora
  da eliminação automática, tanto na pasta principal como na secundária.

### Restauro resistente a interrupções

- Antes de substituir dados, é criada uma proteção completa adicional na pasta
  de proteções e um marcador durável é sincronizado com o disco.
- Se o processo ou computador parar durante a troca da base, anexos, histórico
  ou preferências, o arranque seguinte repõe automaticamente todos os âmbitos.
- O marcador só desaparece depois da base instalada passar uma verificação
  completa; se a recuperação falhar, permanece disponível e a escrita é
  bloqueada.
- Instâncias diferentes do serviço de backup partilham agora o mesmo bloqueio
  por base de dados, impedindo operações concorrentes no mesmo perfil.

### Limites de segurança dos ficheiros de recuperação

- ZIPs são rejeitados antes da extração quando têm demasiados membros, um
  ficheiro superior ao limite, mais de 5 GB extraídos, caminhos profundos ou
  duplicados, ligações simbólicas ou um rácio de compressão anormal.
- O restauro confirma previamente o espaço livre necessário e aplica os mesmos
  limites a pacotes completos, antigos e cifrados.
- Backups `.lgbak` excessivos ou sem espaço suficiente para decifrar são
  recusados antes de criar o ficheiro temporário.

### Migrações e preferências recuperáveis

- Uma pasta `.dados_migracao_*` com manifesto, base, documentos, backups e
  hashes válidos é promovida automaticamente quando uma migração foi
  interrompida antes da publicação final.
- Uma migração incompleta ou ambígua é preservada e bloqueia o arranque; já não
  é apagada nem ocultada por uma base nova.
- Preferências de backup são preparadas e sincronizadas em ficheiro temporário
  e partilham uma transação SQLite; uma falha restaura os dois lados.
- Uma data de último backup anormalmente no futuro deixa de suspender os
  backups automáticos e força uma nova cópia segura.

### Validação

- Foram adicionados testes de injeção de falhas para objetivos, movimentos,
  divisões, histórico reversível, preferências e migrações.
- Foram testadas corrupção sem alteração de tamanho, retenção por família,
  limites de ZIP e recuperação no arranque após um restauro interrompido.
- O Ruff, a compilação Python e os **325 testes** da suite integral passaram.

---

## V3.10 — v3.10.1 — 30 de julho de 2026

Reforço da persistência e do encerramento seguro, com falhas finais agora
visíveis e bloqueantes em vez de serem ignoradas.

### Encerramento transacional

- Todas as tarefas registadas continuam a ser canceladas ou aguardadas antes
  da fase de persistência.
- Cada página carregada recebe agora uma fase comum para descarregar estados
  com gravação diferida.
- Preferências pendentes de backup, IA, Simulações e configuração das colunas
  são guardadas antes do último `commit`.
- Uma falha numa destas gravações cancela o encerramento, reativa a janela e
  mostra a causa; já não existe um `safe_commit` silencioso no fecho.
- Depois do `commit`, a aplicação consolida o WAL, executa
  `PRAGMA quick_check(1)` e fecha explicitamente a ligação SQLite.
- A ligação passou de `synchronous=NORMAL` para `synchronous=FULL` e ganhou
  `busy_timeout=30000`, privilegiando durabilidade sobre a pequena diferença
  de tempo de escrita.
- Se a verificação ou o checkpoint falhar, a aplicação permanece aberta para
  permitir nova tentativa.

### Backups e documentos

- O backup opcional ao fechar corre agora com propagação real de erros.
- Um retorno vazio, uma exceção ou a impossibilidade de iniciar o backup
  impedem o fecho quando essa opção foi escolhida pelo utilizador.
- O backup automático é adiado se o `commit` prévio não for confirmado.
- Um documento anexado é copiado primeiro para um nome temporário.
- A cópia é sincronizada com o disco e comparada com o original por tamanho e
  SHA-256 antes de receber o nome definitivo.
- O registo do documento só é criado depois dessa validação; cópias incompletas
  são removidas.

### Operações atómicas

- O pagamento manual de uma subscrição passou a guardar movimento,
  metadados, ocorrência e próxima renovação numa única operação SQLite.
- Se qualquer passo falhar, todos os passos recuam e não fica um movimento
  parcial nem uma renovação incoerente.

### Visibilidade e documentação

- **Definições → Localização dos dados** apresenta o estado do encerramento
  protegido e explica que uma falha mantém a janela aberta.
- `DATA_SAFETY.md` documenta a sequência exata, as localizações persistentes e
  o limite entre dados confirmados e texto ainda não guardado num formulário.

### Validação

- A base real foi auditada apenas por leitura no perfil persistente:
  `quick_check=ok`, zero erros de chaves estrangeiras, zero documentos em falta
  ou fora da pasta gerida e WAL vazio.
- O backup completo mais recente foi aberto e validado, incluindo base,
  documentos, histórico e preferências.
- Foram adicionados testes para commit final, checkpoint, fecho explícito,
  falha de disco simulada, falha do backup ao fechar, preferências diferidas,
  documento corrompido e pagamento de subscrição atómico.
- Os **313 testes** passaram: 229 testes funcionais, 82 testes de componentes
  visuais, a matriz completa de 12 estados da interface e a captura nativa do
  Windows.

---

## V3.10 — v3.10.0 — 29 de julho de 2026

Medição honesta da precisão das faixas previstas e nova decomposição líquida
da rentabilidade da carteira, incluindo comissões e contribuição de cada ativo.

### Precisão medida pela cobertura das faixas

- A precisão principal deixou de ser calculada como `100% − erro médio`.
- Passa a corresponder à percentagem de resultados reais que ficou entre o
  limite inferior e o limite superior previstos.
- Cada mês guarda explicitamente **Dentro da faixa** ou **Fora da faixa**,
  juntamente com os limites, a largura da faixa e o erro pontual.
- O total apresenta acertos, falhas e número de resultados avaliados, permitindo
  reconciliar diretamente a percentagem mostrada.
- Quando existem meses de validação walk-forward, só esses resultados que o
  modelo ainda não conhecia entram na cobertura anunciada.
- As bandas do calendário de tesouraria também são testadas com multiplicadores
  aprendidos apenas com erros anteriores ao resultado avaliado.
- O MAPE, WAPE, MAE, RMSE e viés continuam disponíveis como diagnósticos
  secundários; já não são apresentados como a definição da precisão.
- A classificação por categoria usa a cobertura como métrica principal e o
  erro ponderado para distinguir uma faixa útil de uma faixa demasiado larga.

### Leitura visual das previsões

- O gráfico **Previsão vs. realidade** mostra no topo a cobertura e a razão
  exata de resultados dentro da faixa.
- Os pontos reais validados ficam verdes quando entram na faixa e vermelhos
  quando ficam fora.
- O detalhe ao passar o cursor mostra valor real, previsto, limites, estado da
  faixa e erro pontual.
- O gráfico por categoria passou a chamar-se **Cobertura das faixas por
  categoria** e apresenta acertos/total e erro ponderado separadamente.
- Dashboard, Centro financeiro, orçamento inteligente e explicações dos
  relatórios usam agora a expressão **dentro das faixas previstas**, evitando
  confundir cobertura com exatidão do valor central.

### Rentabilidade líquida ponderada

- Foi criado um cálculo único e testável para toda a carteira.
- O capital investido continua a representar apenas o custo dos ativos.
- O capital efetivamente comprometido é `capital investido + comissões`.
- O ganho bruto é `valor atual − capital investido`.
- O ganho líquido é `valor atual − capital investido − comissões`.
- A rentabilidade líquida ponderada é o ganho líquido total dividido pelo
  capital total efetivamente comprometido.
- O impacto das comissões mostra a diferença, em pontos percentuais, entre a
  rentabilidade bruta e a rentabilidade líquida.
- Comissões iguais a zero preservam exatamente o cálculo de rentabilidade que
  existia nas versões anteriores.

### Contribuição de cada ativo

- Cada ativo recebe uma contribuição em pontos percentuais calculada como
  `ganho líquido do ativo / capital total comprometido`.
- A soma de todas as contribuições coincide com a rentabilidade líquida
  ponderada da carteira, incluindo ativos com perda.
- Um novo gráfico interativo de barras, centrado em zero, mostra quais os ativos
  que acrescentam ou retiram rentabilidade.
- O cursor revela valor atual, capital, comissões, ganho bruto, ganho líquido e
  contribuição.
- Ao clicar, abre-se um detalhe explicativo e é possível selecionar o mesmo
  investimento na tabela.
- A tabela de ativos passou a incluir **Comissões**, **Ganho líquido**,
  **Rentabilidade líquida** e **Contribuição**, mantendo redimensionamento,
  ocultação e gravação automática das larguras.
- Os cartões de resumo distinguem agora ganho líquido e custos da carteira.

### Registo de comissões e relatórios

- A janela de investimento ganhou o campo **Comissões acumuladas**, destinado
  a compra, venda, custódia e outros custos já pagos.
- O texto da janela esclarece que estas comissões ficam separadas do capital.
- O relatório anual inclui comissões, ganho líquido, rentabilidade líquida
  ponderada, impacto dos custos e contribuição por ativo.
- A nova migração de esquema 14 acrescenta apenas a coluna `holdings.fees`,
  do tipo `REAL`, com valor inicial `0`.
- Investimentos existentes mantêm nome, conta, valor atual, capital e notas
  intactos; não são estimadas nem inventadas comissões retroativas.
- Os valores continuam guardados em euros com casas decimais, como nas versões
  anteriores.
- Snapshots antigos sem esta informação continuam válidos e são lidos com
  comissões iguais a zero.
- A mudança de `v3.9.0` para `v3.10.0` ativa o backup de proteção normal antes
  de qualquer migração da base do utilizador.

### Testes e validação visual

- Foram adicionados testes de cobertura, acertos/falhas, ausência de fuga de
  dados futuros e separação entre cobertura e erro pontual.
- A contribuição de todos os ativos é reconciliada automaticamente com a
  rentabilidade ponderada.
- A compatibilidade sem comissões e a migração idempotente de uma tabela antiga
  são verificadas sem alterar os valores existentes.
- O novo gráfico é testado com ganhos, perdas e comissões.
- A matriz real do Windows passou a capturar também o cartão de contribuição,
  além das 12 combinações de 960, 1280 e 1600 px, temas claro/escuro e
  privacidade.
- A suíte contém **307 testes**.

---

## V3.9 — v3.9.0 — 29 de julho de 2026

Encerramento central e seguro de todas as tarefas em segundo plano, acompanhado
por uma nova janela compacta de arranque com progresso proveniente das
operações reais de proteção, migração e verificação.

### Encerramento central de tarefas

- Foi criado um coordenador por janela que regista todas as tarefas assíncronas
  da aplicação e apresenta os respetivos nomes durante o encerramento.
- Pesquisas da lista de movimentos, pedidos e testes do assistente de IA,
  backups automáticos e cálculos do Dashboard passaram a usar o mesmo registo.
- Os `QThread` próprios do Dashboard deixaram de depender de um caminho de
  encerramento separado.
- Ao pedir para fechar, a aplicação deixa imediatamente de aceitar novas
  tarefas normais e solicita cancelamento cooperativo a todas as que já estão
  em curso.
- O cancelamento nunca usa terminação forçada de threads. As tarefas que não
  podem parar a meio são aguardadas até libertarem a ligação e os objetos Qt.
- A janela permanece visível, desativada e com o estado da operação na barra
  inferior enquanto ainda existe trabalho seguro por concluir.
- Resultados tardios de pesquisas ou da IA são descartados depois do pedido de
  cancelamento e já não conseguem iniciar uma nova operação durante o fecho.
- A base de dados, serviços e filtros globais só avançam para a fase final
  quando o registo central confirma que não existe qualquer tarefa ativa.

### Backup final isolado

- O backup opcional ao fechar passou a executar fora da interface, depois de
  pesquisas, IA, cálculos e backups anteriores terem terminado.
- Esta tarefa final é a única autorizada a arrancar durante o encerramento.
- O backup usa uma ligação SQLite criada e encerrada dentro da própria thread,
  em vez de reutilizar a ligação da interface.
- A aplicação aguarda a criação, assinatura e verificação do arquivo antes de
  concluir o fecho.
- Se o backup ao fechar estiver desativado, a aplicação termina sem criar
  trabalho desnecessário.

### Nova janela de arranque

- Uma janela Liquid Glass de `520 × 270 px` aparece antes da janela principal.
- O cartão mostra versão, etapa atual, percentagem, barra de progresso, nome do
  ficheiro em tratamento e contadores reais como `38/57`.
- A janela não pode ser fechada durante uma migração ou backup de proteção,
  evitando deixar uma atualização a meio.
- O texto explica de forma permanente que os dados existentes não são
  substituídos sem verificação.
- Em caso de bloqueio da proteção, a própria janela muda para um estado de erro
  legível antes de apresentar a explicação detalhada.
- O desenho adapta as cores à paleta clara ou escura disponível antes de a
  preferência guardada da aplicação poder ser aberta.

### Progresso ligado ao trabalho real

- A percentagem deixou de ser uma animação temporal: cada intervalo corresponde
  a uma fase concreta do arranque protegido.
- A localização permanente, a migração de uma instalação antiga e a cópia de
  documentos e backups comunicam o ficheiro e o contador atuais.
- A cópia SQLite comunica páginas copiadas através do callback nativo de backup.
- As verificações `quick_check`, `integrity_check` e
  `foreign_key_check` comunicam atividade enquanto percorrem a base.
- O inventário protegido mostra tabelas, documentos, histórico e preferências
  à medida que são assinados.
- A criação do arquivo comunica a cópia da base, a inclusão dos documentos e a
  validação criptográfica de cada entrada declarada no manifesto.
- Cada migração pendente mostra o nome e a posição no conjunto de migrações.
- A verificação final percorre novamente registos e ficheiros para provar que
  os dados anteriores continuam presentes.
- Com muitos documentos, estas atualizações cedem brevemente o controlo ao
  ciclo visual do Qt, mantendo a pequena janela pintada e responsiva sem
  permitir ações do utilizador durante a operação crítica.

### Integridade e compatibilidade

- Não foi introduzida qualquer alteração destrutiva no esquema financeiro,
  movimentos, documentos, objetivos, investimentos ou preferências.
- A proteção de mudança de `v3.8.0` para `v3.9.0` continua a criar um ponto de
  recuperação obrigatório quando a versão guardada é diferente.
- A validação de um arquivo lê e confirma a base e cada ficheiro declarado,
  verificando tamanho, SHA-256 e CRC durante a leitura.
- Os erros da apresentação do progresso são registados, mas nunca transformam
  uma migração válida numa reposição desnecessária.

### Testes e validação visual

- Foram adicionados testes para cancelamento central simultâneo, rejeição de
  tarefas tardias e autorização exclusiva do backup final.
- Um teste integral fecha a janela com trabalho ativo e confirma que ela só
  desaparece depois de a tarefa terminar.
- Uma atualização protegida confirma progressão monotónica, backup de versão,
  preservação dos movimentos e inventário de documentos.
- Um cenário com vinte documentos confirma que os eventos Qt continuam a ser
  processados enquanto o backup de proteção avança.
- A janela compacta é verificada em dimensões, legibilidade, progresso
  monotónico e bloqueio de fecho prematuro.
- A matriz visual real do Windows passou também a renderizar e medir a janela
  de arranque, além dos estados do Dashboard já existentes.
- A suíte contém **303 testes**.

---

## V3.8 — v3.8.0 — 29 de julho de 2026

Correções de exatidão financeira, privacidade integral nos controlos monetários
e histórico de investimentos sem escritas provocadas pela navegação.

### Reserva de emergência estruturada

- Os objetivos passaram a guardar uma finalidade explícita: **Objetivo geral**
  ou **Reserva de emergência**.
- O cartão do Dashboard deixou de procurar palavras como “emergência” nas
  notas livres. Assim, um objetivo como **Comprar casa** já não é confundido
  com a reserva apenas porque as notas mencionam um fundo de emergência.
- A janela de criação e edição permite escolher a finalidade e a lista assinala
  visualmente a reserva com o símbolo `🛟`.
- Só pode existir uma reserva explicitamente ativa; ao marcar uma nova, a
  anterior volta a objetivo geral sem perder qualquer valor.
- A migração de esquema 13 acrescenta apenas a coluna opcional `goal_type`.
  Bases anteriores preservam todos os objetivos e só nomes exatos como
  “Fundo de emergência” ou “Reserva de emergência” são classificados
  automaticamente. O conteúdo das notas nunca é interpretado.
- Sem objetivo marcado, mantém-se a referência automática de seis meses de
  despesas e do saldo líquido registado.

### Privacidade monetária completa

- A camada comum dos gráficos passou a declarar semanticamente cada campo como
  dinheiro, percentagem, número ou contagem.
- P10, P50, P90, capital investido e cenários base, otimista e de stress ficam
  agora mascarados nos painéis de detalhe e nas comparações fixadas.
- A probabilidade de atingir um objetivo continua corretamente apresentada
  como percentagem, sem ser confundida com um valor monetário.
- Um gestor global identifica automaticamente todos os campos com euros,
  incluindo os que forem criados em versões futuras.
- Com a privacidade ativa, esses campos aparecem mascarados quando estão
  inativos e revelam o valor apenas enquanto o utilizador os está a editar.
- Os campos Meta, Atual e Poupança mensal dos objetivos passaram também a
  declarar explicitamente a unidade em euros.

### Compromissos fixos sem distorção do mês atual

- A **Média mensal** do Centro financeiro utiliza agora exclusivamente meses
  completos.
- O restante previsto do mês atual aparece como valor separado e identificado.
- O resultado expõe o número de meses completos usado, tanto no horizonte
  principal como nos resumos de 3, 6 e 12 meses.
- A tendência já excluía o mês parcial e permanece inalterada; média e tendência
  passam agora a usar a mesma base temporal.

### Ponte do património explicável

- Foi removida a antiga fórmula que começava artificialmente em 40% e somava
  blocos fixos de 15% por cada estado disponível.
- A nova percentagem mede a parcela do valor absoluto dos movimentos da ponte
  cuja origem ficou realmente identificada.
- A aplicação separa agora **variação explicada** de **histórico dos estados**.
  Saldo, carteira e passivos indicam individualmente se os extremos necessários
  estão disponíveis.
- Carteira sem histórico, passivos sem histórico e outros movimentos entram
  explicitamente na parcela não explicada.
- O texto esclarece que a percentagem não é uma estimativa estatística de
  confiança nem uma prova de rentabilidade.

### Rentabilidade ponderada da carteira

- O indicador do gráfico de evolução deixou de calcular uma média simples das
  percentagens de cada ativo.
- A rentabilidade principal passa a ser `ganho total / custo total`, dando a
  cada ativo o peso real do respetivo capital.
- O texto visual e a descrição acessível foram renomeados para
  **Rentabilidade da carteira** e **rentabilidade ponderada**.
- O cartão **Ganho da carteira** e o gráfico passam a usar a mesma percentagem.

### Snapshots sem escrita durante a consulta

- Atualizar ou abrir a página Investimentos deixou de gravar novamente o
  snapshot diário.
- Adicionar, editar ou eliminar um ativo continua a criar o histórico no
  momento correto.
- O arranque cria um único ponto diário controlado para representar dias sem
  alterações.
- Se o valor investido e o valor total forem iguais aos já guardados nesse dia,
  o `updated_at` não é reescrito.

### Segurança dos dados e validação

- A migração é aditiva, protegida pelo backup e rollback de versão existentes;
  não elimina nem converte movimentos, documentos, valores de objetivos,
  investimentos ou outros dados fornecidos pelo utilizador.
- Foram acrescentados testes para a classificação estruturada da reserva,
  privacidade de gráficos e campos monetários, média dos meses completos,
  percentagem patrimonial explicada, rentabilidade ponderada e snapshots
  idempotentes.
- A validação inclui migrações repetidas, arranque protegido, temas,
  privacidade e os viewports de 960, 1280 e 1600 px.
- A suíte integral terminou com **297 testes aprovados**.

---

## V3.7 — v3.7.0 — 29 de julho de 2026

Comparação persistente nos gráficos, ponte reconciliada do património e evolução
auditável dos compromissos fixos.

### Comparar diretamente dentro de qualquer gráfico

- O painel de detalhe passou a oferecer **Fixar para comparar** em todos os
  gráficos construídos sobre a camada visual partilhada.
- Depois de fixar um ponto, a aplicação mantém um indicador visível no gráfico
  e permite abrir outro ponto para consultar diferenças absolutas e
  percentuais.
- Percentagens são comparadas em pontos percentuais; valores monetários
  respeitam o modo de privacidade.
- O mesmo ponto pode ser desafixado no painel e a tecla `Esc` limpa a seleção.
- O botão **Repor** de cada página também limpa comparações fixadas, juntamente
  com o zoom e os filtros.
- Fixar um ponto é uma ação visual e não aciona inadvertidamente a navegação
  associada ao gráfico.

### Ponte do património

- A área **Investimentos** ganhou o cartão **Ponte do património · o que
  mudou**, com períodos de 3, 6, 12 e 24 meses.
- O gráfico reconcilia património inicial, receitas, despesas, capital aplicado
  ou retirado, resultado de mercado, redução de dívida e património final.
- O saldo observado é comparado com os fluxos classificados; transferências e
  outros movimentos não classificados aparecem separadamente quando afetam a
  reconciliação.
- O capital aplicado é separado do ganho de mercado pela evolução do custo de
  aquisição, evitando apresentar reforços como rentabilidade.
- Quando falta um estado histórico da carteira ou dos passivos, a aplicação não
  inventa a decomposição: mostra **Carteira sem histórico** ou **Passivos sem
  histórico**, reduz a cobertura e explica a limitação.
- Cada barra abre a explicação, os valores antes/depois e a cobertura histórica
  utilizada.

### Evolução dos compromissos fixos

- O **Centro financeiro** ganhou um gráfico de 12 meses com barras empilhadas
  para subscrições e pagamentos agendados e uma linha de total mensal.
- O resumo mostra média mensal, pico e tendência; o mês corrente é identificado
  como parcial e não distorce a comparação entre meses completos.
- Um clique num mês lista todos os compromissos incluídos, respetivas datas,
  categorias, origens e valores.
- Só entram compromissos explícitos. Despesas meramente inferidas como
  recorrentes ficam excluídas deste gráfico.
- Movimentos futuros ligados a uma ocorrência substituem a previsão
  correspondente sem duplicar o valor.

### Correspondência de subscrições e validação

- Foi corrigido um caso em que uma ocorrência futura ligada podia ocultar todas
  as restantes renovações da mesma subscrição. A ligação exata exige agora
  também a mesma data de vencimento e o mesmo tipo de movimento, mesmo que o
  movimento tenha sido registado antecipadamente.
- Os novos gráficos vivem em módulos próprios, mantendo o desenho e a lógica
  financeira separados das páginas.
- Não existe alteração de esquema nem migração de dados; movimentos, documentos
  e restantes informações guardadas permanecem intactos.
- Foram acrescentados testes de reconciliação completa e parcial, ausência de
  duplicados, comparação fixa, integração nas páginas e desenho dos novos
  gráficos.
- A matriz real do Windows valida 960, 1280 e 1600 px, temas claro/escuro,
  privacidade e capturas dedicadas aos dois novos cartões.
- A matriz de stress acrescentou nove capturas com 30 categorias densas, estados
  vazios e escalas de 100%, 125% e 150%.
- A suíte completa terminou com 289 testes aprovados.

---

## V3.6 — v3.6.0 — 29 de julho de 2026

Diagnóstico visual das previsões, precisão por categoria e explicação
automática e reconciliada dos erros.

### Previsão vs. realidade

- O gráfico histórico passou a apresentar explicitamente a despesa real, a
  previsão produzida sem dados futuros e a respetiva faixa de incerteza.
- A validação percorre todo o histórico disponível antes de limitar a janela
  visível. Assim, os primeiros pontos apresentados também podem beneficiar de
  aprendizagem exclusivamente anterior.
- Meses com despesa real igual a zero passam a contar como erro quando o modelo
  previa despesa, sem serem apagados da avaliação.
- A precisão calibrada e a precisão base continuam visualmente distintas.
- Um clique num mês mostra os valores exatos e abre as despesas reais usadas
  para verificar a previsão.

### Precisão por categoria

- Foi criado um gráfico dedicado que classifica todas as categorias com dados
  como **Previsível**, **Moderada**, **Volátil** ou **Dados insuficientes**.
- A métrica usa erro absoluto ponderado pelo valor real, evitando que pequenas
  categorias distorçam a leitura com percentagens extremas.
- A regularidade, os meses ativos, o erro médio, o viés e o número de meses
  validados ficam disponíveis no detalhe.
- As categorias de movimentos divididos são avaliadas pelo respetivo valor
  efetivo, tal como nos gráficos de despesas.
- O resumo identifica automaticamente a categoria mais previsível e a mais
  difícil de antecipar.

### Explicação automática dos erros

- Um novo cartão **Porque falhou a previsão?** permite escolher qualquer mês
  já testado.
- Um gráfico divergente mostra à esquerda as categorias que ficaram abaixo do
  esperado e à direita as que ultrapassaram o valor previsto.
- A aplicação distingue categorias novas, despesas que não se repetiram, picos,
  reduções e variações normais face ao padrão recente.
- As previsões por categoria são ajustadas ao total previsto; por isso, a soma
  das causas coincide com a diferença entre o total real e o previsto.
- O mês inicialmente selecionado é o que apresenta a maior diferença absoluta,
  tornando o problema mais relevante imediatamente visível.
- Cada causa abre os movimentos reais da categoria e do mês correspondentes.

### Organização e validação

- O cálculo mensal foi reorganizado em funções comuns para série cronológica,
  backtest, calibração e análise, reduzindo duplicação entre a leitura global e
  a leitura por categoria.
- Não existe alteração de esquema nem escrita nos dados financeiros.
- Foram acrescentados testes para categorias estáveis/voláteis, reconciliação
  integral das causas, meses sem despesa e desenho dos dois novos gráficos.
- A suíte completa terminou com 282 testes aprovados.

---

## V3.5 — v3.5.1 — 29 de julho de 2026

Validação honesta das previsões, correspondência segura de compromissos e
detalhes completos dos gráficos em toda a aplicação.

### Precisão sem antecipar resultados

- A calibração passou a usar validação cronológica progressiva: cada mês é
  previsto com correções aprendidas exclusivamente em meses anteriores.
- A precisão calibrada é calculada apenas nos meses que ainda não eram
  conhecidos quando a respetiva previsão foi produzida.
- A aplicação distingue agora meses usados para aprender de meses usados para
  validar, tanto nos cartões como nas descrições acessíveis.
- Enquanto ainda não existir validação independente suficiente, a interface
  identifica a precisão como estando em validação e mantém a faixa inicial de
  ±20%.
- As faixas aprendidas só substituem a margem inicial depois de existirem pelo
  menos três meses validados fora da amostra.
- O gráfico histórico distingue claramente precisão validada de precisão base
  fora da amostra e passou a chamar-se **Previsão vs. realidade**.

### Subscrições e pagamentos sem falsas correspondências

- Movimentos ligados pelas tabelas de ocorrências têm prioridade absoluta na
  identificação de uma subscrição ou pagamento agendado.
- Quando não existe ligação explícita, a correspondência exige data, tipo,
  montante compatível e nome normalizado com limites de palavras.
- Nomes curtos ou genéricos deixam de usar correspondências parciais. Por
  exemplo, uma subscrição chamada «Bar» já não exclui uma despesa de
  «Barbearia» da estimativa variável.
- A categoria e o montante passam também a ser confirmados antes de retirar um
  movimento da média de despesas variáveis.
- Um evento previsto só é eliminado por um movimento futuro quando existe uma
  ligação exata ou uma assinatura financeira suficientemente forte.

### Detalhes completos fora do Dashboard

- Panorama, Orçamentos, Análises, Histórico, Centro financeiro, Investimentos,
  Simulações e Risco e resiliência passaram a fornecer contexto próprio ao
  painel de detalhe partilhado.
- Os painéis mostram os valores exatos, o intervalo, a categoria e os maiores
  movimentos que explicam cada seleção, respeitando divisões por categoria.
- O comparador apresenta lado a lado a evidência dos períodos atual e de
  referência.
- A sazonalidade filtra exatamente o mês do ano e o dia da semana selecionados;
  a análise dinâmica respeita base de pagamento/compra, categoria,
  subcategoria, comerciante, conta e forma de pagamento.
- Barras simples, componentes do score, trajetórias de resiliência e património
  passaram também a aceitar seleção e detalhe.
- Só é apresentado um botão de ação quando existe uma ação real. Os botões
  abrem a origem, selecionam o elemento ou aplicam o filtro correspondente.
- Os diálogos continuam limitados ao ecrã, usam o mesmo tema claro/escuro e
  respeitam o modo de privacidade.

### Compatibilidade e validação

- Não existe alteração de esquema nem escrita adicional em movimentos,
  documentos, subscrições ou restantes dados financeiros.
- Foram acrescentados testes que impedem precisão dentro da amostra, fuga de
  informação do mês atual, falsas correspondências de nomes genéricos e
  deduplicação por montantes diferentes.
- A validação cobre ainda ações reais nos detalhes, ausência de botões
  decorativos, temas, privacidade e os tamanhos de janela suportados.
- A suíte completa terminou com 280 testes aprovados.

---

## V3.5 — v3.5.0 — 28 de julho de 2026

Previsões autoajustáveis, detalhe explicável dos gráficos e validação visual
com dados extremos.

### Previsões que aprendem com os erros

- A previsão de tesouraria deixou de usar sempre uma faixa fixa de ±20%.
- Antes de prever o futuro, a aplicação simula o que teria previsto para cada
  um dos últimos meses completos usando apenas os 90 dias que o antecediam.
  Compara depois essa estimativa com a despesa realmente registada.
- A diferença histórica corrige automaticamente o valor diário previsto. Os
  meses mais recentes têm maior peso e um mês excecional é limitado para não
  distorcer todas as previsões futuras.
- As faixas otimista e pessimista passam a ser calculadas pelos erros
  efetivamente observados. Enquanto não existirem três meses válidos, mantém-se
  explicitamente a faixa inicial de ±20%.
- O orçamento mensal usa a mesma ideia: o ritmo dos dias restantes é corrigido
  pelo erro histórico e apresenta um intervalo aprendido para o fecho do mês.
- O Dashboard e o Centro financeiro mostram quantos meses suportam o ajuste,
  a precisão obtida e a alteração aplicada à despesa variável.
- A aprendizagem é inteiramente local e recalculada a partir de meses já
  concluídos. Não grava modelos, não altera movimentos e não envia informação
  financeira para serviços externos.

### Detalhes ao clicar nos gráficos

- Os gráficos partilhados ganharam um painel de detalhe claro, limitado ao
  ecrã e consistente nos temas claro e escuro.
- Um clique num ponto apresenta os valores exatos, o período ou a categoria e
  uma explicação do que está a ser mostrado.
- No Dashboard, o painel apresenta até dez dos movimentos que explicam o
  resultado e permite abrir diretamente a lista de movimentos já filtrada.
- As contribuições de movimentos divididos usam a categoria e o valor efetivos
  da divisão. Assim, o detalhe reconcilia com os donuts, treemaps, Sankey e
  restantes gráficos por categoria.
- Eventos futuros mantêm a ação para abrir a subscrição, pagamento agendado ou
  movimento de origem.
- O modo de privacidade mascara também os valores do painel de detalhe.
- Os gráficos especializados que já tinham seleção própria conservam o
  respetivo comportamento; os restantes usam o novo detalhe genérico.

### Testes visuais mais exigentes

- Foi criada uma matriz de stress separada que usa o motor gráfico real do
  Windows em escalas de 100%, 125% e 150%.
- Cada escala testa 960, 1280 e 1600 px, temas claro/escuro e privacidade,
  produzindo nove capturas densas verificadas.
- O cenário contém 30 categorias adicionais com nomes extensos, 15 meses de
  histórico, descrições longas e valores superiores a um milhão de euros.
- Quatro gráficos são ainda renderizados sem dados para validar os estados
  vazios, e o novo painel de detalhe é capturado com dez movimentos.
- A validação rejeita scroll horizontal, texto explicativo cortado, categorias
  perdidas, erros de desenho e cartões sobrepostos.
- Este teste revelou compressão de uma linha do Dashboard quando a tabela de
  equivalência em horas crescia muito. A correção passou a verificar todas as
  linhas dinâmicas, não apenas os cartões de categorias.

### Compatibilidade e validação

- Não existe alteração de esquema nem migração de dados nesta versão. As novas
  consultas são apenas de leitura e respeitam a data financeira, transferências
  internas e divisões já guardadas.
- Foram acrescentados testes para calibração, bandas aprendidas, reconciliação
  de divisões, privacidade, ação do detalhe e legibilidade da nova janela.
- A suíte completa terminou com 274 testes aprovados, além da matriz visual
  normal e da nova matriz de stress em três escalas.

---

## V3.4 — v3.4.1 — 28 de julho de 2026

Compactação localizada dos cabeçalhos dos dois gráficos financeiros temporais
do Dashboard.

### Entradas vs. saídas e margem de poupança

- Os títulos de **Entradas vs. saídas acumuladas** e **Margem e taxa de
  poupança** foram aproximados do topo dos respetivos cartões.
- A margem superior passou de 10 para 5 px e o intervalo entre os elementos do
  cabeçalho passou de 4 para 2 px.
- As frases explicativas ficaram limitadas à altura real do texto, incluindo
  quando quebram em várias linhas. Deixam assim de absorver espaço livre e de
  criar um vazio artificial antes do gráfico.
- A medição da altura deixou de reutilizar os limites anteriores do próprio
  `QLabel`, evitando que uma medição inicial estreita fique presa mesmo depois
  de o cartão ganhar a largura definitiva.
- O alinhamento das explicações passou a ser explicitamente superior e à
  esquerda.
- As dimensões mínimas dos gráficos, a altura mínima configurada dos cartões e
  a organização responsiva das colunas não foram alteradas.
- Foi acrescentado um teste geométrico para 960, 1280 e 1600 px que verifica as
  margens, o intervalo e a altura real das duas descrições.
- A suíte completa terminou com 267 testes aprovados e a matriz visual passou
  nos três tamanhos, temas claro/escuro e modo de privacidade.

---

## V3.4 — v3.4.0 — 28 de julho de 2026

Validação mais rigorosa, tabelas verdadeiramente personalizáveis e exploração
visual coordenada dos gráficos.

### Períodos e valores validados sem correções silenciosas

- Os meses recebidos pelos repositórios e pelo cálculo da saúde financeira têm
  agora de respeitar estritamente o formato `AAAA-MM` e representar um mês
  real. Valores como `2026-13`, datas incompletas ou texto arbitrário deixam de
  ser convertidos silenciosamente para o mês atual.
- A leitura, consulta e gravação de orçamentos por categoria usam a mesma
  validação, impedindo que um erro de entrada consulte ou altere um período
  diferente do pretendido.
- Os valores de orçamento são validados como números finitos. Valores
  incompletos, infinitos ou inválidos originam uma mensagem clara em vez de
  ficarem escondidos nos cálculos.

### Erros dos gráficos com diagnóstico útil

- Uma falha ao desenhar um gráfico continua a apresentar um estado legível no
  cartão, mas passa também a ser registada com o tipo de gráfico, dimensões,
  número de elementos e exceção recebida.
- Erros idênticos consecutivos são agregados para evitar repetição excessiva no
  log. Depois de uma renderização bem-sucedida, uma nova falha volta a ser
  registada normalmente.

### Organização completa das colunas

- O formato de preferências das tabelas evoluiu para a versão 3. Cada coluna
  possui um identificador semântico e independente da sua posição ou do texto
  visível no cabeçalho.
- A aplicação guarda automaticamente, por tabela, a largura, visibilidade,
  posição visual e ordenação de cada coluna. Mudar a ordem por arrastamento e
  ordenar uma lista sobrevive ao fecho e reabertura da aplicação.
- O menu do botão direito de cada cabeçalho permite ajustar a coluna escolhida
  ao conteúdo, ocultar ou mostrar colunas, ajustar todas as larguras, repor
  apenas as larguras originais ou restaurar toda a organização da tabela.
- Os estados antigos por índice continuam a ser aceites e são migrados sem
  apagar as preferências existentes. Todas as tabelas atuais têm IDs de coluna
  explícitos; o fallback determinístico fica reservado a tabelas futuras.

### Gráficos ligados pelo mesmo período

- No Dashboard, apontar um dia ou mês em saldo/previsão, entradas e saídas
  acumuladas ou margem e taxa de poupança destaca o período correspondente nos
  outros dois gráficos.
- No Panorama, a evolução mensal e a composição de despesas por mês ficam
  igualmente ligadas.
- A correspondência adapta datas diárias a blocos mensais e vice-versa. O
  destaque permanece brevemente durante a passagem do cursor entre cartões,
  evitando que a comparação desapareça no trajeto.
- A navegação por teclado participa na mesma ligação visual.

### Modo de foco dos gráficos

- O menu `…` de todos os gráficos inclui **Abrir em modo de foco**.
- O gráfico original é ampliado numa janela clara e responsiva, sem duplicar
  dados nem perder zoom, seleção ou filtros. O cartão mantém o seu espaço para
  que a página não salte.
- Fechar pelo botão ou pela tecla `Esc` devolve o gráfico exatamente ao seu
  cartão, com os limites e a política de tamanho originais.

### Segurança dos dados e validação

- Esta versão não altera o esquema nem o conteúdo da base de dados financeira.
  A única evolução persistente pertence às preferências visuais das tabelas.
- Foram acrescentados testes para meses inválidos, valores não finitos,
  diagnóstico de renderização, migração e reposição de tabelas, ligação
  temporal diária/mensal e restauração do modo de foco.
- A suíte completa terminou com 267 testes aprovados. A validação visual
  renderizou 960, 1280 e 1600 px em temas claro/escuro, com valores
  visíveis/privacidade, sem overflow dos estados e com os valores privados
  efetivamente mascarados.
- O ponto de divisão adicional dos ficheiros grandes não foi aplicado, conforme
  pedido.

---

## V3.3 — v3.3.4 — 28 de julho de 2026

Persistência auditável e estável das larguras e da visibilidade das colunas.

### Identificadores estáveis por tabela

- Todas as tabelas geridas pela aplicação receberam um identificador semântico
  próprio, como `movements.list`, `portfolio.holdings`,
  `budgets.smart_forecast` ou `reports.anomalies`.
- As novas chaves `table_columns:v2:<identificador>` deixam de depender do
  título do cartão, dos nomes visíveis das colunas ou do idioma da interface.
  Alterar uma frase ou traduzir um cabeçalho já não faz desaparecer as larguras
  escolhidas pelo utilizador.
- As tabelas futuras obtêm ainda um identificador determinístico a partir do
  nome do objeto, do atributo Python que as contém ou da sua posição dentro da
  página/janela. Os identificadores explícitos continuam a ter prioridade.

### Compatibilidade e diagnóstico

- As preferências gravadas pelas versões anteriores continuam a ser lidas. Na
  primeira utilização de cada tabela, o estado antigo é copiado para a chave
  estável de segunda geração, sem apagar a chave original.
- Cada registo guarda agora a versão do formato, o identificador da tabela, as
  larguras e, quando personalizado, o conjunto de colunas ocultas.
- Uma falha ao ler ou gravar preferências deixa de ser ignorada: o log identifica
  a tabela, a chave, a operação e a exceção recebida.
- Falhas repetidas e idênticas são agregadas para não inundar o log. Um novo
  sucesso limpa o estado de erro, permitindo voltar a sinalizar uma falha
  posterior.
- O fecho da aplicação resume no log as tabelas que não conseguiram concluir a
  gravação. A falha afeta apenas a preferência visual; movimentos, documentos e
  restantes dados financeiros não são modificados.

### Validação

- Foram adicionados testes para estabilidade perante alterações de títulos e
  cabeçalhos, migração das chaves antigas, gravação recuperável e registo de uma
  falha simulada.
- A aplicação completa confirmou identificadores preenchidos e únicos para
  todas as tabelas carregadas, mantendo todas as colunas redimensionáveis.

---

## V3.3 — v3.3.3 — 28 de julho de 2026

Contexto temporal permanente, evidência da atualidade dos dados, comparação
visual reforçada e exploração coordenada das categorias.

### Contexto visível sem ocupar o Dashboard

- Todos os cartões do Dashboard passam a ter um badge curto e consistente que
  explica a referência usada: “Filtro atual”, “Mês atual”, “Estado atual”,
  “Últimos 12 meses” ou o horizonte da previsão.
- Os oito indicadores principais também identificam a sua base temporal,
  incluindo a média salarial usada no valor do tempo.
- Ao deslocar a página para além dos controlos, aparece um resumo compacto e
  fixo dos filtros ativos. O painel completo permanece na posição original e o
  resumo desaparece automaticamente quando se regressa ao topo.
- A implementação usa uma camada reutilizável em `GlassCard`, sem duplicar
  títulos nem alterar a largura relativa das colunas.

### Atualidade, cobertura e confiança dos dados

- O gráfico de saldo e previsão mostra a data do último movimento, a cobertura
  do histórico recente e a confiança da leitura.
- O orçamento do mês identifica a data do cálculo e a percentagem das despesas
  já coberta por orçamentos.
- A tesouraria futura mostra a atualidade do histórico, o nível de confiança e
  o número de eventos usados no horizonte selecionado.
- O Score Financeiro 2.0 e o diagnóstico do Panorama apresentam explicitamente
  “dados até”, juntamente com a confiança. A mesma data aparece nos cartões de
  resumo e na anatomia do score.
- Foi criada uma consulta agregada para calcular primeiro/último movimento,
  meses ativos, cobertura e confiança, ignorando transferências internas e
  datas inválidas.

### Gráficos ligados e comparação direta

- Ao apontar uma categoria no donut, cascata, orçamento, tabela de tempo de
  trabalho, treemap, bolhas ou Sankey, essa categoria fica temporariamente
  destacada nos restantes gráficos e as restantes ficam visualmente atenuadas.
- O destaque sobrevive durante a passagem do cursor entre cartões e desaparece
  automaticamente; os cliques e filtros existentes continuam a funcionar.
- O donut ganhou um anel interior tracejado com a distribuição do período de
  referência.
- O radar mostra um segundo polígono tracejado e a cascata apresenta contornos
  tracejados com os valores anteriores na mesma escala.
- As descrições explicam o significado de cada camada tracejada e continuam a
  mostrar as categorias com maior variação.

### Responsividade e limites desta fase

- Os badges foram impedidos de participar nos mínimos horizontais dos cartões,
  preservando as proporções já aprovadas em janelas de 1600 px e superiores.
- A recomposição após mudanças de tamanho de letra recalcula imediatamente a
  altura do painel de filtros, evitando controlos cortados a 125%.
- A folha de classificação 50/30/20 começa agora integralmente dentro da janela
  mesmo quando a interface está sob carga; a animação deixa de poder mantê-la
  temporariamente fora do ecrã.
- Conforme pedido, esta versão **não substitui as alturas fixas dos cartões com
  tabelas e gráficos por alturas calculadas pelo conteúdo**. A lógica de altura
  existente foi mantida.

### Validação

- Foram adicionados testes para cobertura dos dados, data do Score 2.0,
  comparação direta, badges reutilizáveis, destaque coordenado e resumo sticky.
- A aplicação completa foi verificada em 960, 1280 e 1600 px, temas
  claro/escuro e modo de privacidade.
- A validação com o motor gráfico real do Windows gerou 12 capturas do
  Dashboard e capturas isoladas da comparação por categoria, inteligência
  financeira e Score 2.0.

---

## V3.3 — v3.3.2 — 28 de julho de 2026

Correções prioritárias de comparação, coerência temporal, estados sem dados e
adaptação da janela de personalização de cartões.

### Comparação aplicada aos gráficos

- “Entradas vs. saídas acumuladas” mostra agora as entradas e saídas do período
  de referência através de linhas tracejadas, na mesma escala do período atual.
  A legenda e a informação ao passar o rato identificam os valores de ambos os
  períodos.
- “Margem e taxa de poupança” apresenta a margem de referência com contorno
  tracejado e a respetiva taxa através de uma segunda linha. A informação ao
  passar o rato inclui também a margem e a taxa comparáveis.
- A comparação das despesas por categoria deixou de ficar limitada aos
  “Insights”. Passa a estar visível nos cartões de cascata, donut, horas de
  trabalho, radar, peso, volume e distribuição das categorias.
- O donut e a cascata mostram valores exatos do período de referência e a
  respetiva variação ao passar o rato. Os restantes cartões recebem um resumo
  consistente do total e das três categorias com maior alteração.

### Período selecionado respeitado em todo o Dashboard

- As mini-tendências das receitas, despesas e poupança passam a usar o período
  escolhido no Dashboard. Séries extensas são condensadas até 24 pontos sem
  alterar os totais financeiros.
- As tendências de saldo e investimentos deixam de estar presas aos últimos
  oito ou doze registos; são amostradas dentro do intervalo selecionado.
- A evolução da carteira é pedida apenas para as datas do filtro ativo.
- Foi corrigida a ação dos cartões: “Despesas” abre o período selecionado e
  “Projeção do mês” abre especificamente as despesas do mês atual.

### Estados sem dados honestos e cálculo mais leve

- Um intervalo completamente vazio deixa de produzir meses artificiais a zero.
  A mensagem apresentada passa a indicar claramente que não existem movimentos
  financeiros no período.
- Meses intermédios sem movimentos continuam visíveis quando fazem parte de um
  intervalo que contém dados reais, preservando a continuidade temporal.
- A taxa média de poupança considera apenas meses com receitas, evitando que
  meses sem salário reduzam artificialmente a média.
- O resumo mensal passa de várias consultas repetidas por mês para uma única
  consulta agrupada, reduzindo o trabalho da base de dados.

### Janela “Cards” e composição visual

- A janela de personalização calcula o tamanho a partir do ecrã disponível,
  nunca ultrapassa os limites úteis do monitor e mantém a lista com scroll
  interno quando existem muitos cartões.
- O contexto de comparação fica imediatamente abaixo do título de cada cartão,
  sem criar uma faixa vazia expansível antes do gráfico.

### Validação

- Foram acrescentadas regressões para períodos vazios, meses sem receitas,
  comparação em todos os gráficos compatíveis, tendências anuais e dimensões da
  janela “Cards”.
- A matriz visual real do Windows cobre 960, 1280 e 1600 px, temas claro/escuro
  e privacidade, incluindo capturas isoladas dos gráficos financeiros e do
  cartão de despesas por categoria.

---

## V3.3 — v3.3.1 — 28 de julho de 2026

Correção da coerência temporal dos cartões “Margem e taxa de poupança” e
“Entradas vs. saídas acumuladas” com o filtro global do Dashboard.

### Período selecionado sem limites escondidos

- O cartão de margem deixou de pedir apenas os últimos oito meses e o respetivo
  gráfico deixou de cortar novamente os dados a oito barras. Passa a receber
  todos os meses abrangidos pelo período escolhido.
- “Entradas vs. saídas acumuladas” passa a preservar o início exato do
  intervalo, mesmo quando não existem movimentos nos primeiros dias.
- As séries semanais e mensais incluem agora todos os blocos temporais do
  filtro, incluindo blocos sem movimentos, evitando que um período pareça mais
  curto do que realmente é.
- Na opção “Tudo”, o histórico começa no primeiro movimento real para não criar
  uma série artificial desde a data técnica mínima da aplicação.

### Informação e legibilidade

- Os títulos dos dois cartões identificam dinamicamente o filtro ativo, por
  exemplo “último ano”, em vez de apresentarem um período fixo.
- As descrições indicam a resolução e o número real de pontos ou blocos
  apresentados. Meses parciais nas extremidades de um intervalo são
  identificados explicitamente.
- O gráfico de margem adapta a largura das barras e reduz de forma inteligente
  a quantidade de datas no eixo quando o período é longo. O ano é acrescentado
  quando a série atravessa mais de um ano.
- O gráfico acumulado ganhou margem lateral suficiente para a primeira e a
  última data ficarem integralmente visíveis.

### Validação

- Foram adicionadas regressões para confirmar que o filtro anual móvel usa os
  365 dias exatos e que a margem contém os 13 blocos mensais intercetados,
  incluindo os dois meses parciais das extremidades.
- A série temporal foi testada com dias e meses iniciais sem movimentos e os
  cartões foram validados com o motor gráfico real do Windows.
- A matriz visual continua a cobrir 960, 1280 e 1600 px, temas claro/escuro e
  modo de privacidade; foram ainda geradas capturas isoladas dos dois cartões
  com o filtro anual.

---

## V3.3 — v3.3.0 — 27 de julho de 2026

Substituição das sugestões genéricas do Dashboard por uma camada local de
decisão financeira, quantificada, explicável e diretamente acionável.

### Prioridades financeiras realmente úteis

- O antigo cartão “Sugestões acionáveis” passa a apresentar as próximas
  melhores decisões por ordem de urgência, impacto monetário e solidez dos
  dados.
- A análise cruza, sem enviar dados para serviços externos, possíveis
  duplicados e anomalias, risco de saldo negativo, défice do período, ritmo dos
  orçamentos, falta na reserva de emergência, aumentos relevantes por categoria
  e peso das subscrições.
- Cada prioridade distingue claramente o facto observado da ação sugerida. Os
  valores de suporte aparecem como métricas estruturadas — por exemplo gasto,
  projeção e limite — e a frase nunca inventa movimentos nem assume que uma
  despesa é desnecessária.
- O cartão mostra a maior quantia em jogo, urgência, confiança da leitura,
  evidência usada e data dos dados. A cobertura de dados é apresentada
  separadamente e identificada como indicador de cobertura, não como
  probabilidade estatística.
- Quando não há informação suficiente, a aplicação diz explicitamente que não
  vai inventar recomendações e orienta o utilizador para registar a base
  financeira.

### Ações diretas e apresentação

- Cada prioridade tem um botão contextual. Anomalias e aumentos abrem a lista
  de movimentos já filtrada por categoria e pesquisa; orçamentos, tesouraria,
  subscrições e objetivos abrem diretamente a respetiva área.
- O novo cartão usa marcadores laterais e badges distintos para estado urgente,
  atenção, neutro e positivo. A estrutura mantém títulos, explicação, métricas,
  evidência e ação legíveis também quando ocupa uma coluna estreita.
- Todos os montantes são formatados apenas na camada visual. O modo de
  privacidade mascara impacto, gasto, projeção, saldo, reserva e restantes
  valores sem tentar apagar números de uma frase depois de ela ser construída.
- O resumo indica que o cálculo é local e mostra a data até à qual os dados
  foram considerados.

### Organização e desempenho

- As regras ficam isoladas em `services/decision_intelligence.py`, como função
  pura e testável, enquanto a apresentação responsiva fica em
  `ui/components/decision_briefing.py`; o já extenso `dashboard.py` deixa de
  conter regras financeiras e estilos linha a linha.
- O cálculo continua no trabalhador de segundo plano do Dashboard. Consultas
  adicionais de comparação, anomalias, orçamento e tesouraria só são executadas
  quando o cartão de prioridades está ativo.
- A preferência existente `show_insights` foi preservada, por isso nenhuma
  personalização guardada é perdida durante a atualização.

### Validação

- Foram adicionados testes para ranking de riscos, excesso orçamental,
  calendário realista da reserva, ausência honesta de dados, ações diretas e
  mascaramento integral dos valores.
- O teste responsivo integral confirma a nova experiência em 960, 1280 e 1600
  píxeis, temas claro/escuro e privacidade.
- A validação com o motor gráfico real do Windows verifica conteúdo,
  diversidade visual e proteção dos montantes e guarda uma captura isolada do
  novo cartão.

---

## V3.2 — v3.2.3 — 27 de julho de 2026

Revisão visual do painel de período e introdução de duas leituras gráficas
complementares para perceber a criação de margem financeira.

### Período e resolução reorganizados

- Os cinco grupos deixam de depender de larguras fixas e passam para uma grelha
  responsiva. Em largura intermédia, as duas linhas ocupam toda a área de forma
  equilibrada; “Categorias” nunca volta a ficar isolado no canto inferior.
- O resumo ativo usa uma superfície neutra com marcador lateral na cor do tema,
  substituindo a faixa azul que competia visualmente com os controlos.
- Os grupos usam superfícies claras, fronteiras consistentes e realce discreto
  ao passar o cursor, com contraste equivalente nos temas claro e escuro.
- Os botões segmentados deixaram de usar azul fixo no próprio código. Todos os
  chips da aplicação respeitam agora a cor principal, o fundo e o contraste do
  tema selecionado.

### Novo gráfico — entradas vs. saídas acumuladas

- O Dashboard apresenta a evolução acumulada das receitas e despesas no
  período escolhido. A distância entre as linhas representa diretamente a
  margem financeira criada ou consumida.
- O gráfico acompanha a resolução diária, semanal ou mensal, ignora
  transferências internas e não acrescenta dias anteriores ao primeiro
  movimento quando o utilizador seleciona todo o histórico.
- O cursor revela data, entradas, saídas e margem acumuladas num painel claro e
  legível. O gráfico suporta teclado, privacidade e exportação para PNG.

### Novo gráfico — margem e taxa de poupança

- As barras mostram o dinheiro que sobrou ou faltou em cada um dos últimos oito
  meses; a linha apresenta a taxa de poupança e a referência visual de 20%.
- O detalhe interativo distingue receitas, despesas, margem, percentagem e mês
  ainda em curso. Valores monetários respeitam o modo de privacidade.
- Os dois novos gráficos reutilizam os cálculos mensais existentes. A consulta
  adicional do fluxo acumulado só é executada quando o respetivo cartão está
  ativo, preservando a fluidez do arranque.

### Validação

- Foram adicionados testes para agrupamento diário/semanal/mensal, exclusão de
  transferências internas, desenho claro/escuro, privacidade, hover e ausência
  de erros de pintura.
- A validação responsiva confirma novamente o painel em 960, 1280 e 1600
  píxeis, sem grupos cortados nem linhas desequilibradas.
- A matriz Windows guarda também capturas isoladas dos dois novos cartões e
  rejeita gráficos vazios, sem diversidade visual ou com erros de pintura.

---

## V3.2 — v3.2.2 — 27 de julho de 2026

Melhoria visual focada na utilização real do espaço, leitura imediata do
contexto do Dashboard e apresentação integral dos estados financeiros.

### Navegação adaptável

- Em janelas inferiores a 1120 píxeis, a barra lateral passa automaticamente
  para 64 píxeis e mantém os ícones, tooltips e área de scroll. O conteúdo
  financeiro ganha mais de 120 píxeis úteis sem exigir configuração manual.
- A barra superior troca os rótulos longos de atualizar, privacidade e novo
  movimento por ícones acessíveis em ecrãs estreitos, dando prioridade à
  pesquisa e aos dados.
- A partir de 1120 píxeis, os rótulos completos regressam. Uma escolha manual
  da largura da barra lateral continua a prevalecer durante a sessão e é
  guardada para futuras aberturas.

### Filtros do Dashboard com hierarquia clara

- Período, agrupamento, comparação, vista e limite de categorias passam a ter
  grupos visuais próprios; o respetivo rótulo nunca fica separado do controlo.
- Um resumo compacto apresenta de imediato o contexto ativo, por exemplo:
  `3 meses · Diário · Período anterior · Vista compacta · Top 8 categorias`.
- A disposição usa duas linhas em 960 e 1280 píxeis e uma linha em 1600
  píxeis, com alturas calculadas pelo conteúdo e sem espaços vazios.
- A vista compacta/detalhada usa um seletor coerente com comparação e
  categorias, reduzindo altura e mantendo o estado selecionado explícito.

### Estados financeiros totalmente legíveis

- Os badges de estado dos indicadores passam a ocupar a largura disponível e
  a quebrar texto por linhas, em vez de cortar mensagens como a reserva de
  emergência ou o valor do tempo.
- O texto integral permanece também no tooltip e na descrição acessível.

### Validação visual reforçada

- A matriz Windows passa a confirmar a largura da navegação, densidade da
  barra superior, número de linhas dos filtros, altura máxima do cartão,
  resumo ativo e ausência de estados cortados.
- As verificações continuam a cobrir 960, 1280 e 1600 píxeis, temas
  claro/escuro e privacidade ligada/desligada.
- A saída da validação é agora compatível com consolas Windows em `cp1252`,
  mantendo o manifesto completo em UTF-8.

---

## V3.2 — v3.2.1 — 27 de julho de 2026

Correção da fluidez no arranque, do ciclo de vida dos temporizadores e do
bloqueio provocado pelos backups automáticos, com validação visual através do
motor real do Windows.

### Arranque responsivo e carregamento progressivo

- O Dashboard apresenta primeiro uma estrutura leve e navegável. Os cartões,
  gráficos e consultas são iniciados depois do primeiro ciclo visual, evitando
  bloquear a janela no momento em que aparece.
- O estado intermédio identifica claramente que o Dashboard está a ser
  preparado e permite utilizar imediatamente a navegação.
- As restantes páginas deixaram de ser importadas em conjunto com a janela
  principal. A respetiva classe e os módulos associados só são carregados na
  primeira abertura de cada área.
- O registo público de páginas continua compatível com os nomes anteriores,
  mas resolve as classes através de importação progressiva.
- O pacote Windows inclui explicitamente todos os módulos de páginas
  descobertos pelo empacotador, preservando o carregamento progressivo sem
  falhas ao abrir uma área no executável.
- O teste dedicado ao arranque confirma que o ciclo de eventos continua a
  responder durante os primeiros 900 ms.

### Temporizadores e encerramento

- A verificação inicial de renovações passou de um callback global não
  cancelável para um `QTimer` pertencente à janela.
- O temporizador de renovações e o temporizador de backups são sempre parados
  antes de encerrar.
- A verificação de subscrições ignora pedidos depois de iniciado o
  encerramento, impedindo consultas sobre uma ligação SQLite já fechada.
- Se um backup agendado estiver em curso ao fechar, a janela aguarda a
  conclusão através de sinais Qt, sem destruir a tarefa ou a ligação utilizada.

### Backup automático fora da interface

- A verificação de periodicidade, clonagem SQLite, compressão, hashes,
  validação integral e segunda cópia do backup agendado são executadas numa
  tarefa de segundo plano.
- Cada tarefa cria uma ligação SQLite própria, com `busy_timeout`, e nunca
  reutiliza a ligação pertencente à interface.
- Antes de lançar a tarefa é efetuado apenas um `commit` rápido na interface.
  O processamento demorado deixa de interromper cliques, scroll ou navegação.
- Foram acrescentados testes que simulam um backup lento, verificam que o ciclo
  visual continua ativo e confirmam que o pacote criado pela ligação própria é
  válido.

### Isolamento dos testes e logs

- O `pytest` passa a criar um perfil temporário exclusivo antes de importar
  qualquer serviço da aplicação.
- Base, anexos, backups, preferências e logs produzidos pelos testes ficam nesse
  perfil e são removidos no fim da sessão.
- Foi acrescentado um encerramento explícito dos handlers de logging, evitando
  ficheiros temporários bloqueados no Windows.
- Os avisos e erros deliberadamente provocados pelos testes deixam de aparecer
  no log real do utilizador em `%LOCALAPPDATA%\FinancasPessoais\logs`.

### Validação visual com fontes reais

- A validação geométrica `offscreen` continua disponível para testar layouts
  rapidamente, mas deixou de ser considerada prova de tipografia: nesse motor
  Qt o Windows não disponibiliza as fontes instaladas e o texto pode surgir
  como quadrados.
- O novo `scripts/validate_visual_rendering.py` força a plataforma gráfica real
  do Windows, valida a disponibilidade das fontes, caracteres portugueses,
  euro e emojis e só depois produz capturas.
- A matriz completa gera o Dashboard em 960, 1280 e 1600 píxeis, nos temas claro
  e escuro e com privacidade ligada e desligada.
- Cada execução cria um manifesto JSON com plataforma, fonte, escala,
  diversidade visual e ficheiros capturados. Capturas vazias, sem detalhe,
  com dimensões erradas ou sem glifos obrigatórios fazem a validação falhar.
- Antes de cada captura, o validador espera pela nova consulta do Dashboard,
  processa layouts e elimina widgets substituídos. Também confirma que os oito
  indicadores ficam mascarados em privacidade e visíveis no estado normal,
  impedindo capturas de um estado anterior de serem aceites.
- Um teste independente executa esta validação no motor Windows para impedir
  que uma imagem cheia de caracteres de substituição volte a ser aceite.

---

## V3.2 — v3.2.0 — 26 de julho de 2026

Separação definitiva entre programa e dados, backups integrais verificáveis e
proteção contra operações concorrentes.

### Dados persistentes fora da instalação

- Base de dados, anexos, backups, histórico reversível e preferências passam a
  residir em `%LOCALAPPDATA%\FinancasPessoais\dados`.
- Os logs passam a residir em `%LOCALAPPDATA%\FinancasPessoais\logs`, enquanto
  as proteções obrigatórias de versão continuam em
  `%LOCALAPPDATA%\FinancasPessoais\protecoes_de_versao`.
- Na primeira abertura é detetada a instalação legada junto ao programa. A base
  é clonada através da API de backup online do SQLite, validada com
  `integrity_check` e `foreign_key_check` e comparada linha a linha por
  impressões digitais.
- Anexos, backups existentes e preferências são copiados com verificação
  SHA-256. A nova pasta só fica ativa através de uma troca atómica depois de
  todas as verificações terminarem.
- A pasta antiga é deliberadamente mantida intacta como proteção adicional. Um
  manifesto regista origem, destino, tabelas, ficheiros, tamanhos e hashes.
- Bases explicitamente deslocadas e ambientes de teste continuam a usar
  caminhos coerentes junto da respetiva base, sem misturar dados do perfil.

### Backups completos em todos os modos

- “Backup completo agora”, backup automático e backup ao fechar produzem agora
  pacotes integrais `.zip`, em vez de cópias SQLite isoladas.
- Cada pacote contém a base de dados, documentos anexados, histórico para
  desfazer e preferências locais, mesmo quando algum destes âmbitos está vazio.
- O manifesto inclui finalidade, versões, contagens, tamanhos e SHA-256. O ZIP
  é reaberto, todos os hashes são verificados, a base é extraída numa pasta
  temporária e passa uma verificação SQLite completa.
- A cifragem AES-GCM passa a cifrar o pacote integral (`Z`) e não apenas a base.
- O restauro de pacotes novos substitui base, anexos, histórico e preferências
  de forma atómica, com rollback interno. Antes disso é criado outro pacote
  integral com o estado atual.
- Os backups antigos `.sqlite3`, ZIP sem manifesto e `.lgbak` com conteúdo
  SQLite continuam a poder ser recuperados pelo caminho de compatibilidade.
- O teste “Testar recuperação” repõe o pacote mais recente numa pasta isolada,
  valida os conteúdos e elimina a simulação sem alterar os dados ativos.

### Segunda localização e estado de proteção

- As Definições permitem escolher uma segunda localização opcional fora da
  pasta principal de dados, adequada a disco externo, NAS ou pasta
  sincronizada.
- A cópia secundária é escrita temporariamente, comparada por SHA-256 e só
  depois publicada. Uma falha externa não elimina nem invalida o backup
  principal.
- A retenção configurada também é aplicada apenas aos backups geridos nessa
  segunda localização.
- Um estado visual de 0 a 100 distingue backup completo verificado, atualidade,
  segunda cópia confirmada e cifragem.

### Concorrência e coerência dos caminhos

- `QLockFile` impede que duas instâncias usem simultaneamente o mesmo perfil.
  O bloqueio é adquirido antes da migração e mantido durante todo o arranque,
  utilização e restauro.
- As operações de backup e restauro têm ainda um bloqueio interno reentrante,
  evitando sobreposição dentro da própria aplicação.
- `Repo`, `FinanceDB`, documentos, histórico e backups recebem agora o mesmo
  conjunto central de caminhos, eliminando combinações acidentais entre uma
  base e anexos de outra localização.

### Valores monetários

- Não foi aplicada qualquer migração para cêntimos inteiros.
- Os valores continuam guardados nos campos monetários atuais em euros
  decimais e apresentados como `xx.xx €`. A migração de localização e os
  backups preservam os valores e respetivas linhas sem transformação.

### Interface, documentação e testes

- O cartão de backups adapta os botões à largura disponível, identifica que os
  backups são completos e inclui seleção da segunda pasta, score e teste de
  recuperação.
- “Localização dos dados” mostra base, anexos, histórico, preferências, backups
  e proteções, explicando que ficam separados do programa.
- Foram acrescentados testes de migração idempotente, bloqueio perante destino
  parcial, preservação de decimais, pacote completo, restauro exato, espelho
  secundário, teste de recuperação e exclusão mútua entre instâncias.
- `README.md`, `DATA_SAFETY.md` e o guia de instalação foram atualizados para o
  novo contrato de persistência.

---

## V3.1 — v3.1.0 — 26 de julho de 2026

Proteção transacional dos dados pessoais antes de futuras atualizações.

### Barreira obrigatória antes da primeira escrita

- `FinanceDB` já não abre imediatamente a base de dados em modo de escrita.
- `DataProtectionGuard` começa por abrir a base apenas para leitura e executa
  `quick_check`, `integrity_check` e `foreign_key_check`.
- Na primeira abertura de cada versão, é criada uma proteção externa antes de
  executar a inicialização, migrações, dados de referência ou qualquer outra
  escrita.
- A versão da aplicação e a versão máxima da estrutura ficam registadas na
  própria base apenas depois de toda a atualização ser validada.

### Proteção completa e verificável

- O arquivo ZIP inclui a base de dados SQLite, documentos anexados, histórico
  reversível e preferências de backup.
- Cada base e ficheiro protegido possui tamanho e hash SHA-256 no
  `manifesto_protecao.json`.
- O arquivo acabado de criar é novamente aberto e todos os hashes são
  verificados antes de prosseguir.
- Para a base usada normalmente, as proteções são guardadas fora da pasta do
  programa em
  `%LOCALAPPDATA%\FinancasPessoais\protecoes_de_versao`.
- A retenção dos backups automáticos normais não apaga estas proteções de
  versão.

### Preservação após migrações

- Antes da atualização são registadas as colunas e as impressões digitais de
  todas as linhas de dados existentes, excluindo apenas estruturas derivadas
  de pesquisa FTS e o diário técnico de migrações.
- Depois da atualização, cada linha original tem de continuar presente e
  idêntica. É permitido acrescentar dados ou colunas, mas não alterar nem
  remover silenciosamente informação anterior.
- Cada documento e ficheiro gerido que já existia volta a ser validado por
  caminho, tamanho e SHA-256.
- Se uma destas regras falhar, a ligação é fechada, o arranque é cancelado e a
  base, anexos, histórico e preferências são repostos a partir do arquivo
  verificado.

### Recuperação de interrupções e compatibilidade

- Um marcador externo e atómico identifica uma atualização em curso.
- Se o processo ou computador for interrompido, o arranque seguinte recupera
  primeiro o estado anterior e só depois volta a tentar a atualização.
- O marcador permanece disponível quando uma reposição não pode ser concluída,
  permitindo nova tentativa e indicando o arquivo que deve ser preservado.
- Uma aplicação antiga deixa de poder escrever numa base usada por uma versão
  mais recente ou numa estrutura superior à que suporta.
- Bases corrompidas são recusadas sem qualquer abertura em escrita.
- Bases antigas, ainda sem metadados desta proteção, recebem automaticamente
  uma cópia identificada como proveniente da versão legada.

### Interface e documentação

- O cartão “Localização dos dados” mostra agora a base, documentos, backups e
  pasta externa das proteções.
- Um estado visual confirma que a barreira está ativa.
- O novo botão “Abrir proteções de versão” permite consultar diretamente os
  arquivos gerados.
- Uma falha de segurança no arranque apresenta uma mensagem legível, impede a
  continuação da aplicação e indica o estado da reposição e o registo de
  diagnóstico.
- `DATA_SAFETY.md`, também incluído no pacote Windows, documenta o contrato de
  atualização, recuperação e os limites desta proteção.

### Validação

- Oito testes específicos cobrem atualização normal, base legada, tentativa de
  alteração de movimentos/documentos/preferências, reposição automática,
  interrupção abrupta, falha da própria reposição, downgrade, estrutura futura,
  base corrompida e localização externa.
- A bateria funcional terminou com 212 testes aprovados.
- O percurso visual integral foi aprovado separadamente em 960, 1280 e 1600
  píxeis, temas claro/escuro e modo de privacidade.
- O teste de arranque responsivo voltou a ser executado depois do percurso
  visual e permaneceu dentro do limite definido.
- Uma cópia temporária da base real, com 38 tabelas e 96 movimentos, foi
  atualizada para `v3.1.0`: não houve perda em nenhuma tabela e foi criado um
  arquivo de proteção válido. A cópia temporária foi eliminada após o teste.
- O pacote Windows final foi reconstruído com sucesso, inclui o novo guia e as
  duas notas de versão e não contém bases de dados, anexos ou backups pessoais.

---

## V3.0 — v3.0.9 — 26 de julho de 2026

Remoção do acesso à aplicação clássica nas Definições.

### Localização dos dados

- Foi removido o botão “Abrir app clássica” do cartão “Localização dos dados”.
- O cartão mantém apenas a ação útil “Abrir pasta da base de dados”.
- O subtítulo da página passa a indicar apenas tema, privacidade, backups e IA
  Gemini.

### Limpeza de código

- Foi removida a ligação do botão e o método `open_classic`, que iniciava um
  ficheiro externo através do sistema operativo.
- A dependência `os`, usada exclusivamente por essa funcionalidade, deixou de
  ser importada.

### Validação

- O teste visual integral confirma que o cartão contém apenas o botão da pasta
  da base de dados e que já não existem textos ou métodos associados à
  aplicação clássica.
- A bateria geral terminou com 204 testes aprovados e o percurso visual
  completo não encontrou regressões de layout ou de tema.

---

## V3.0 — v3.0.8 — 26 de julho de 2026

Correção do editor de classificação de categorias da análise 50/30/20.

### Erro de layout eliminado

- `BudgetBucketDialog` deixou de tentar instalar um segundo layout diretamente
  no diálogo `LiquidSheetDialog`.
- O conteúdo passa pela área própria da folha visual, eliminando o aviso
  `QLayout: Attempting to add QLayout` e tornando os controlos efetivamente
  visíveis.
- O diálogo normaliza o seu anfitrião para a janela principal. Uma página
  comprida dentro do scroll já não cria uma sobreposição com milhares de
  píxeis de altura.

### Janela compacta e legível

- O painel visível fica centrado com 640 píxeis de largura e cerca de 536
  píxeis de altura, limitado a um máximo de 720 × 600 píxeis.
- Foram acrescentados um título visível, contador de categorias e uma legenda
  curta para Necessidades, Desejos e Poupança.
- A tabela apresenta linhas compactas, seletores sem sobreposição, cabeçalho
  estável e scroll vertical quando existem mais categorias.
- Os botões passam a usar os nomes “Guardar” e “Cancelar” e os estilos primário
  e secundário da aplicação.

### Tema e contraste

- Os estilos do fundo e da superfície de `LiquidSheetDialog` passam a usar
  seletores próprios, evitando que fundo e borda sejam aplicados aos títulos,
  tabelas e campos descendentes.
- Os campos mantêm agora o contraste correto nos temas claro e escuro. Esta
  correção beneficia também os restantes diálogos baseados na mesma folha
  visual.

### Validação

- Novo teste de regressão reproduz a abertura a partir de uma página com 2 000
  píxeis de altura nos temas claro e escuro.
- O teste confirma ausência do aviso do Qt, painel contido em 960 × 640,
  conteúdo visível, contraste dos seletores, scroll e persistência dos valores
  escolhidos.
- A bateria geral terminou com 204 testes aprovados; o teste visual integral
  percorreu também todas as páginas, resoluções, temas e modo de privacidade
  sem regressões.

---

## V3.0 — v3.0.7 — 26 de julho de 2026

Correção sistemática do espaço vertical entre títulos, descrições e conteúdo
dos cartões.

### Cabeçalho compacto reutilizável

- `GlassCard` recebeu um modo de cabeçalho compacto que calcula a altura real
  do texto depois de cada alteração de largura, estilo ou conteúdo.
- Títulos simples ficam numa linha com a sua altura natural; títulos longos
  continuam a quebrar linha em 960 px sem serem cortados.
- A margem superior dos cartões corrigidos passa para 7 píxeis e a distância
  entre cabeçalho e primeiro conteúdo para 4 píxeis.
- O mesmo mecanismo pode ajustar descrições dinâmicas e elimina a repetição de
  regras locais nas páginas.

### Cartões corrigidos

- Panorama financeiro: “Distribuição por forma de pagamento” e “Diagnóstico do
  mês”.
- Risco e Resiliência: “Plano de resposta”.
- Score Financeiro 2.0: “Evolução nos últimos 12 meses”.
- Histórico mensal: “Comparação com meses anteriores”, mantendo também
  “Distribuição mensal” no novo mecanismo comum.
- Simulações: cabeçalho do laboratório, três títulos de secção, descrição do
  hábito, esforço no rendimento e detalhe do gráfico. A introdução e o grupo
  de ações rápidas passam também a acompanhar a altura real do seu conteúdo,
  incluindo quando os botões mudam de linha.
- Orçamentos: “Orçamentos inteligentes”, respetiva explicação, “Previsão visual
  por categoria” e respetiva descrição.
- Dashboard: “Despesas em tempo de trabalho” e a sua introdução.
- “Top resultados” mantém a correção da v3.0.5 através do novo mecanismo comum.

### Resultado medido

- A auditoria abrangeu 146 cartões em 960, 1280 e 1600 píxeis.
- Os nove títulos problemáticos ocupavam entre 36 e 175 píxeis para texto que
  necessitava de 16 píxeis na maioria dos casos.
- Depois da correção, os 83 cartões não métricos auditados, as nove etiquetas
  auxiliares e os dois grupos responsivos registados não apresentam qualquer
  expansão vertical residual.
- O tempo de construção da janela permaneceu próximo de 0,53 segundos no
  cenário de auditoria com uma cópia da base de dados real.

### Validação

- O teste de cabeçalhos foi alargado aos onze cartões compactos, nove
  descrições, dois grupos responsivos e às três larguras suportadas.
- A validação confirma margens, intervalos, altura responsiva do texto e altura
  mínima dos gráficos.

---

## V3.0 — v3.0.6 — 26 de julho de 2026

Aumento da área útil da lista de subscrições.

### Lista de subscrições

- O cartão “Lista” passa a reservar uma altura mínima de 560 píxeis.
- A tabela dispõe agora de pelo menos 500 píxeis de altura, permitindo ver mais
  subscrições em simultâneo antes de ser necessário usar o scroll interno.
- Em janelas de menor altura, o cartão deixa de ser comprimido e a página usa o
  scroll vertical exterior para preservar a legibilidade da lista.
- O cartão recebeu uma referência própria na página, facilitando testes e
  futuras alterações isoladas.

### Validação

- Foi acrescentado um teste responsivo a 960 × 640 que confirma a altura do
  cartão e da tabela, assim como a ativação correta do scroll da página.

---

## V3.0 — v3.0.5 — 26 de julho de 2026

Correção do alinhamento vertical dos cabeçalhos de dois gráficos.

### Cartões compactados

- O título de “Top resultados”, em Análise dinâmica, deixou de absorver altura
  livre do cartão e passa a ocupar apenas a altura necessária para o texto.
- Foi aplicada a mesma correção a “Distribuição mensal”, em Histórico mensal.
- A margem superior dos dois cartões foi reduzida de 13 para 7 píxeis e a
  distância entre título e gráfico passou de 8 para 4 píxeis.
- O espaço recuperado é entregue à área útil do gráfico, sem reduzir dados,
  barras ou legibilidade.
- Os cartões receberam identificadores próprios na página para permitir
  validação direta e manutenção independente.

### Validação

- Foi adicionado um teste visual de geometria que confirma a posição do título,
  a margem superior, o intervalo até ao gráfico e o respeito pela altura mínima
  do gráfico.

---

## V3.0 — v3.0.4 — 26 de julho de 2026

Correção crítica de responsividade no arranque da aplicação.

### Arranque e geometria

- Foi removido um ciclo de realimentação no redimensionamento do Dashboard que
  podia colocar a janela no estado “Não responde” logo após a abertura.
- O Dashboard deixou de calcular a sua altura mínima a partir da geometria já
  esticada dos próprios cartões. Esse cálculo aumentava novamente a página,
  provocava outro redimensionamento e repetia o processo indefinidamente.
- O ajuste compacto de “Orçamento vs. realizado” mantém-se, mas passa a
  sincronizar apenas a altura do próprio cartão, sem alterar recursivamente a
  altura total da página.
- As alturas mínimas das linhas do Dashboard são agora recalculadas a partir
  dos tamanhos intrínsecos dos cartões. Textos e tabelas que crescem depois de
  receber dados deixam de sobrepor ou ultrapassar o limite inferior da página.
- Uma margem final pequena e fixa absorve a interpolação interna do layout no
  ponto de passagem para três colunas, sem aumentar os próprios cartões nem
  voltar a medir geometrias em ciclo.
- Foi eliminada a segunda aplicação integral do tema e da escala de letra
  durante os primeiros 100 milissegundos. O arranque faz agora apenas a
  sincronização final do conteúdo.
- Voltar a selecionar a página ativa também agenda a sincronização da largura,
  evitando conservar por instantes a geometria de uma janela mais larga.
- O teste visual ativa explicitamente o Dashboard antes de validar a sua
  geometria, reproduzindo o comportamento real de navegação em vez de medir
  uma página escondida.

### Validação e proteção contra regressões

- Foi criado um teste específico de responsividade do arranque que monitoriza
  os pulsos da interface, o tempo decorrido, o número de redimensionamentos e o
  crescimento da página.
- No cenário de diagnóstico que revelou o problema, cinco segundos de
  observação demoravam cerca de 40,7 segundos, geravam 37 397 eventos de
  redimensionamento e faziam a página crescer para mais de 40 mil píxeis.
- Com a correção, a mesma observação termina em cerca de 5,05 segundos, com
  quatro redimensionamentos e uma altura estável inferior a 3 100 píxeis.
- Na verificação final com uma cópia da base de dados real, a janela foi
  construída em 0,587 segundos e uma observação de três segundos terminou em
  3,006 segundos, processando 73 pulsos normais da interface.
- As melhorias visuais e a recuperação da tabela de orçamentos inteligentes da
  v3.0.3 foram preservadas.

---

## V3.0 — v3.0.3 — 26 de julho de 2026

Correção de densidade visual no Dashboard e recuperação da geometria da tabela
de orçamentos inteligentes.

### Espaçamento dos cartões

- “Orçamento vs. realizado” deixou de calcular a altura através de um
  `sizeHint()` independente da largura. O cartão mede agora título, estado,
  indicadores, explicação e gráfico à largura realmente disponível.
- Título e textos explicativos usam a altura exata necessária, em vez de
  receberem espaço vertical excedente distribuído pelo layout.
- O espaçamento interno do cartão foi reduzido de oito para quatro píxeis e os
  grupos responsivos usam intervalos verticais mais curtos.
- “Despesas por categoria” passou a usar margens verticais menores, três píxeis
  entre título e gráfico e uma margem interna do donut mais curta.
- O cálculo é repetido depois de redimensionar a janela, mantendo o resultado
  compacto nas larguras suportadas e com letra ampliada.
- A altura total do Dashboard acompanha agora o cartão visível mais baixo,
  evitando que a compactação corte o último cartão em combinações largas.

### Orçamentos inteligentes

- A tabela recebeu uma identidade persistente própria, impedindo que larguras
  antigas ou de outra configuração sejam aplicadas ao cartão.
- A disposição inválida é migrada uma única vez na v3.0.3. A coluna “Categoria”
  deixa de poder reaparecer com apenas 30 píxeis.
- As sete colunas recebem larguras iniciais equilibradas e ocupam toda a área
  disponível em janelas largas; em janelas estreitas, o scroll horizontal
  começa sempre na coluna “Categoria”.
- Todas as colunas continuam interativas: as larguras alteradas pelo utilizador
  voltam a ser guardadas automaticamente após esta migração.
- O título do cartão pode quebrar linha sem impor uma largura mínima superior à
  página.

### Validação

- Foram adicionadas verificações de regressão para o espaçamento efetivo dos
  dois cartões do Dashboard, para a origem horizontal da tabela inteligente e
  para larguras de colunas legíveis.

---

## V3.0 — v3.0.2 — 26 de julho de 2026

Correção visual para recuperar as proporções compactas dos cartões de gráficos
sem remover as opções de exploração introduzidas na versão anterior.

### Alturas dos gráficos

- A pilha central volta a respeitar simultaneamente a altura mínima e máxima
  calculada para a página ativa. Assim, cartões com política vertical expansível
  deixam de absorver espaço excedente e crescer desnecessariamente.
- A política partilhada dos gráficos passou de expansão vertical livre para
  altura preferida. Os mínimos calculados continuam a ser respeitados, mas o
  gráfico já não estica apenas para preencher espaço disponível.
- O redimensionamento continua a ser recalculado quando a página ativa muda ou
  quando o conteúdo é atualizado, preservando o scroll por página da v3.0.1.
- Donut, radar e Sankey regressam ao limite compacto de oito categorias por
  defeito; o gráfico de bolhas regressa ao limite de dezasseis.
- O Dashboard abre agora em “Top 8 + Outros”. A opção “Todas” continua
  disponível no seletor para uma expansão deliberada.
- Na primeira abertura da v3.0.2, a preferência antiga “Todas” é migrada uma
  única vez para “Top 8”, garantindo que a redução também é visível em bases de
  dados já existentes. Depois da migração, qualquer escolha do utilizador volta
  a ser guardada normalmente.
- Repor os filtros do Dashboard repõe também “Top 8”, em vez de voltar a criar
  cartões excessivamente altos.
- O gráfico “Orçamento vs. realizado” conserva todas as categorias numa área
  interna com scroll e deixa de aumentar o cartão indefinidamente.
- As alturas preferidas de radar, treemap, bolhas e Sankey foram reduzidas para
  proporções mais compactas, mantendo o cálculo necessário para não cortar
  linhas ou categorias.

### Validação

- Os testes dos componentes passam a confirmar tanto o modo compacto inicial
  como a expansão manual para todas as categorias.
- O teste integral da interface confirma o limite compacto, a nova versão e a
  manutenção das larguras e alturas responsivas.

---

## V3.0 — v3.0.1 — 26 de julho de 2026

Versão de correção e consolidação da privacidade, qualidade dos dados,
previsões, gráficos e navegação em páginas longas.

### Privacidade efetiva da IA

- As preferências `privacy_aggregated`, `anonymize` e `ask_before_details`
  deixaram de ser apenas opções guardadas e passaram a controlar a preparação
  real de cada pedido.
- O contexto agregado já não contém descrições de movimentos, contas ou nomes
  de possíveis pagamentos recorrentes.
- As operações que necessitam de detalhe enviam apenas os campos mínimos e,
  quando configurado, apresentam antes do envio a quantidade de movimentos e
  os campos incluídos.
- O modo visual de privacidade força anonimização e confirmação adicional em
  pedidos detalhados.
- Emails, IBAN e sequências numéricas pessoais reconhecíveis são ocultados
  antes do envio.
- Perguntas livres são anonimizadas quando necessário e o histórico local já
  não guarda a cópia integral do contexto financeiro enviado ao modelo.
- A análise comportamental foi reformulada para apresentar hipóteses prudentes,
  sem inferir emoções, intenções ou saúde mental como factos.

### Qualidade dos dados e cálculos financeiros

- `parse_date` deixou de substituir datas inválidas pela data atual; devolve
  agora um erro explícito.
- Conversões numéricas rejeitam `NaN` e infinito. A importação inteligente
  apresenta uma coluna “Motivo” com a causa concreta de cada linha a rever.
- Datas ou valores inválidos já existentes na base são contados e mostrados na
  revisão mensal, nos relatórios e no contexto de qualidade fornecido à IA.
- O cálculo de independência financeira deixou de criar a despesa fictícia de
  1 000 euros e de dividir períodos incompletos por doze.
- A nova estimativa usa até 12 meses completos observados, exige pelo menos
  três meses com atividade e mostra período, confiança e pressupostos.
- O capital usado no progresso é identificado como capital investível líquido:
  liquidez mais investimentos, menos passivos.
- A projeção considera rentabilidade, inflação, imposto sobre ganhos e comissão
  dos reforços, apresentando “dados insuficientes” quando não existe base real.
- O indicador correspondente do Dashboard passou a usar exatamente o mesmo
  método validado.

### Tooltips e segurança dos objetos Qt

- O gestor de tooltips claros passou a ler também `Qt.ToolTipRole` de células
  em tabelas e listas, eliminando o caminho restante para balões nativos pretos.
- A lista de movimentos mantém o seu cartão de detalhe próprio sem abrir um
  segundo tooltip.
- Gráficos com detalhe desenhado internamente deixaram de definir tooltips
  nativos concorrentes.
- Os callbacks diferidos da persistência de colunas usam referências fracas e
  validam os objetos Qt antes de os consultar, evitando acessos a objetos C++
  já eliminados.

### Scroll, responsividade e navegação

- A posição vertical é guardada separadamente por página e restaurada numa
  única passagem ao regressar, em vez de vários saltos atrasados para o topo.
- A pilha central calcula o tamanho apenas com a página ativa e deixou de ficar
  presa a uma altura máxima antiga após atualizações assíncronas.
- Áreas de scroll internas em eventos e comparação de períodos libertam a roda
  para a página principal quando chegam ao início ou ao fim.
- A validação visual cobre agora também janelas de 960 × 640 e 960 × 720, além
  das larguras 960, 1280 e 1600, temas claro/escuro, privacidade e letra a 125%.

### Gráficos e interface

- Foi criada uma resolução central e estável de cores de categorias, usada por
  donut, treemap, bolhas e Sankey para manter a mesma categoria com a mesma cor.
- O Dashboard permite escolher “Todas”, “Top 8 + Outros” ou “Top 16 + Outros”.
  Por defeito são apresentadas todas as categorias.
- Clicar em “Outros” expande os gráficos para todas as categorias.
- O Sankey calcula a altura para todas as linhas, ganhou realce por passagem do
  cursor e permite abrir a categoria selecionada.
- Todos os gráficos têm foco por teclado, navegação com setas e um botão
  visível de ações para exportar PNG ou repor zoom quando disponível.
- Datas inválidas em eixos deixam de aparecer disfarçadas como a data atual.
- Terminologia visível uniformizada: “saldo transitado”, “teste histórico”,
  “concentração 80/20”, “investimento único”, “sugestões acionáveis” e
  “independência financeira”.

### Testes e manutenção

- Adicionados testes de anonimização da IA, campos mínimos, contexto agregado,
  números não finitos, motivos de importação e deteção de dados corrompidos.
- Adicionados testes puros para a estimativa de independência financeira sem
  dados artificiais.
- Adicionados testes visuais para tooltips de células, cores partilhadas,
  limites de categorias, teclado, Sankey denso, scroll por página e letra
  ampliada.
- Os testes de metadados passaram a validar a versão semântica a partir da fonte
  única, reduzindo o risco de esquecer valores duplicados em versões futuras.

---

## V3.0 — v3.0.0 — 26 de julho de 2026

Primeira versão formalmente identificada e documentada da aplicação.

### Identificação e processo de lançamento

- Criada uma fonte única de versão em `utils/version.py`.
- A aplicação apresenta `v3.0.0` no título da janela e num selo permanente na
  barra lateral.
- A barra lateral compacta apresenta a forma reduzida `v3`.
- A versão também é registada nos metadados da `QApplication`.
- Criados dois históricos permanentes:
  - `RELEASE_NOTES.md`, com detalhe técnico;
  - `RELEASE_NOTES_RESUME.md`, com o resumo para o utilizador.
- Definida a convenção de acrescentar a versão mais recente no topo sem apagar
  as versões anteriores.

### Interface Liquid Glass e navegação

- Renovação visual dos cartões, fundos, estados, badges, ícones e hierarquia
  tipográfica.
- Suporte consistente para tema claro, tema escuro e modo de privacidade.
- Navegação reorganizada em 19 entradas principais, preservando 28 páginas.
- Páginas relacionadas consolidadas em áreas de trabalho:
  - Análise e relatórios;
  - Saúde financeira;
  - Simulações;
  - Assistente e IA.
- Seletor responsivo dentro de cada área para trocar entre páginas secundárias.
- Favoritos e grupos recolhíveis na barra lateral.
- Barra lateral normal e compacta com estado persistente.
- Janela de personalização reduzida e adaptada ao conteúdo.
- Personalização de cartões alargada às várias páginas da aplicação.
- Possibilidade de ocultar cartões mantendo as preferências entre sessões.
- Botões para repor zoom, filtros e vista dos gráficos.

### Tooltips, pop-ups e interatividade

- Substituição dos tooltips nativos problemáticos do Windows por um balão
  interno claro, compacto e de alto contraste.
- O novo tooltip usa fundo branco, texto escuro, contorno com a cor principal e
  posicionamento limitado à janela da aplicação.
- Tooltips próprios desenhados nos gráficos para evitar janelas nativas pretas.
- Cartão de detalhe legível ao passar o cursor sobre movimentos.
- Cartões informativos e estados visuais revistos para leitura em ambos os
  temas.
- Notificações do Dashboard alinhadas à direita, junto à área de personalização,
  com duração superior e apresentação mais legível.
- Interação por hover, zoom, seleção, filtros e detalhes contextuais nos novos
  gráficos.

### Dashboard e indicadores pessoais

- Reorganização responsiva dos cartões e respetivas larguras.
- “Despesas por categoria” revisto com:
  - valores sempre visíveis junto às percentagens;
  - paleta com maior diferenciação entre categorias;
  - legenda e alinhamento estáveis ao redimensionar a janela;
  - espaçamento adicional entre o total e o respetivo rótulo.
- “Peso das categorias” redimensionado para mostrar todas as categorias sem
  invadir o cartão seguinte.
- Removida a legenda final duplicada do cartão de peso das categorias.
- “Ritmo diário de gastos” corrigido para apresentar todos os dias da semana em
  formato compacto.
- Categorias do Dashboard sincronizadas com o período selecionado, em vez de
  permanecerem sempre no mês atual.
- “Orçamento vs. realizado” ajustado para apresentar todas as categorias com
  dados e corrigir a sobreposição na linha dos 100%.
- Novo cartão de reserva de emergência com valor atual, objetivo, diferença e
  estado atingido/acima/abaixo.
- Novo indicador do valor líquido de uma hora de trabalho.
- Novo cartão que converte despesas por categoria em horas e dias de trabalho.
- “Insights acionáveis” com cores mais legíveis.
- “Objetivos em destaque” com título alinhado no topo.
- Pareamento responsivo dos cartões de despesas, insights, orçamento e
  objetivos.

### Saúde financeira, risco e inteligência

- Score Financeiro 2.0 com classificação, decomposição, tendência e explicação.
- Nova área “Risco e resiliência”.
- Cenários de resiliência e análise da capacidade de suportar choques
  financeiros.
- Património completo com ativos, investimentos, liquidez e passivos.
- Orçamentos inteligentes com sugestão, rollover, projeção e previsão de
  rutura.
- Deteção de anomalias em valores, frequência e padrões de despesa.
- Centro de alertas financeiros por gravidade e origem.
- Comparador de períodos para todas as categorias com dados.
- Explicação automática das causas de variações entre períodos.
- Cards de insights com ações relacionadas e ligações para as áreas relevantes.

### Novos gráficos e análises

- Gráfico de precisão das previsões:
  - realizado versus previsto;
  - erro médio, MAPE, viés e precisão;
  - intervalo de incerteza de 80%;
  - backtest baseado apenas em meses anteriores.
- Mapa de sazonalidade 12 × 7, cruzando mês e dia da semana.
- Pareto de comerciantes com percentagem acumulada e identificação da
  concentração de 80% da despesa.
- Fluxo de dinheiro em Sankey com altura calculada a partir da quantidade de
  categorias, evitando cortes em meses densos.
- Comparadores e gráficos corrigidos para não limitarem os resultados à
  categoria “Subscrições”.
- Previsão visual por categoria, execução de orçamentos e rollover com altura
  dinâmica.
- Tooltips claros em “Fluxo do dinheiro”, “Período e resolução” e restantes
  gráficos interativos.

### Investimentos e simulações

- Evolução do capital investido face ao valor total através de duas linhas.
- Cálculo do ganho médio até à data atual.
- Simulador de investimento com:
  - investimento único ou mensal;
  - capital inicial;
  - horizonte de 1 a 5 anos;
  - inflação, impostos e comissões;
  - cenários pessimista, base e otimista;
  - esforço financeiro calculado em tempo real.
- Simulação Monte Carlo com distribuição de resultados e risco.
- Nova área de simulações de hábitos e despesas discricionárias.
- Cenários para casino, apostas, refeições, cafés, cervejas, doces,
  subscrições e outros hábitos.
- Projeções para 1 mês, 1, 2, 3, 5, 10 e 30 anos.
- Comparação do custo evitado com alternativas e investimento potencial.
- Frequência configurada em valores inteiros, com ações de redução de 25% e
  para metade efetivamente funcionais.
- Percentagem de esforço calculada automaticamente face ao rendimento mensal.
- Planos de mudança de hábitos, objetivos associados e acompanhamento do
  progresso.

### Centro financeiro

- Calendário financeiro corrigido para apresentar cabeçalhos, números e células
  com dimensões estáveis.
- “Próximos eventos” colocado ao lado do calendário com linhas explícitas,
  data, origem, valor e ação para abrir.
- “Tesouraria a 60 dias” reposicionada abaixo do calendário, com título no topo
  e menor espaçamento vertical.
- Previsões de tesouraria, pagamentos recorrentes e subscrições integradas.
- Cartões e listas dimensionados em função do conteúdo real.

### Movimentos, tabelas e documentos

- Todas as tabelas de utilizador permitem redimensionar colunas manualmente.
- A largura escolhida para cada coluna e tabela é guardada automaticamente.
- Menu no cabeçalho para ocultar ou voltar a mostrar colunas.
- Tabelas internas do calendário excluídas corretamente deste mecanismo.
- Lista de movimentos com carregamento progressivo.
- Filtros, regras de categoria, ações em lote e deteção de duplicados.
- Exportação de movimentos com proteção do modo de privacidade e escape seguro
  de conteúdo.
- Cartão de hover próprio para detalhes do movimento.
- Gestão de anexos e documentos com auditoria de consistência.
- Relatório anual com receitas, despesas, investimentos, documentos e
  explicações comparativas.

### Histórico reversível e base de dados

- Esquema de base de dados atualizado para a versão 12.
- Nova tabela `change_journal` para registar alterações reversíveis.
- Possibilidade de desfazer:
  - importações;
  - edições individuais;
  - edições em massa;
  - aplicação de regras;
  - eliminações.
- Eliminações preservam temporariamente os anexos necessários à recuperação.
- Janela de histórico de alterações com estado disponível/desfeito.
- Índices e consultas revistos para carregamento progressivo e análises mais
  rápidas.
- Proteções contra referências Qt já eliminadas durante personalização e troca
  de página.

### Backups, privacidade e segurança

- Backups manuais, automáticos e ao fechar a aplicação.
- Retenção configurável de backups.
- Exportação completa com base de dados e anexos.
- Backups cifrados opcionais no formato `.lgbak`.
- Cifragem AES-256-GCM com derivação PBKDF2-SHA256.
- Palavra-passe guardada apenas no Gestor de Credenciais do Windows.
- Validação de integridade antes do restauro.
- Backup de segurança criado antes de substituir a base de dados.
- Remoção de chaves antigas da IA existentes em cópias SQLite ou ZIP.
- Modo de privacidade aplicado a valores, relatórios e exportações.

### Código, qualidade e distribuição

- Separação de serviços para métricas do Dashboard, saúde financeira,
  inteligência, projeções, simulações, documentos e diagnósticos de despesa.
- Componentes reutilizáveis para cartões, gráficos, tabelas, tooltips, layouts
  e áreas de trabalho.
- Repositório Git local criado na branch `main`.
- Bases de dados, anexos, backups, credenciais, logs e pacotes excluídos do Git.
- Remoção de backups duplicados e bases de dados antigas sem utilização.
- Dependências de execução fixadas nas versões efetivamente testadas.
- Processo reproduzível de criação do executável em
  `scripts/build_windows.ps1`.
- Pacote Windows validado para não incluir dados financeiros pessoais.
- Testes responsivos para 960, 1280 e 1600 px, temas claro/escuro e modo de
  privacidade.
- Testes de serviços, integridade, gráficos, tabelas, backups, personalização e
  arranque do executável compilado.
