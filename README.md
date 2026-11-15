# Finanças Pessoais — Liquid Glass

Aplicação Windows em Python/PySide6 para gerir finanças pessoais com os dados
guardados localmente. Não existe sincronização automática com bancos,
corretoras ou aplicações de investimento.

Versão do código: **v3.50.0** — [notas técnicas](RELEASE_NOTES.md) ·
[resumo da versão](RELEASE_NOTES_RESUME.md) ·
[proteção dos dados entre versões](DATA_SAFETY.md)

## Versões e atualizações pelo GitHub

Repositório oficial: https://github.com/renafcampos27/Finan-as.
Os pacotes Windows validados são publicados em
[Releases](https://github.com/renafcampos27/Finan-as/releases).
O ficheiro «Source code» do GitHub contém código e não é o instalador.
Descarrega o ZIP `LiquidGlass_App_vX.Y.Z.zip`, extrai a pasta inteira e executa
`INSTALAR_FINANCAS_PESSOAIS.bat`.

Na instalação Windows habitual, a aplicação procura versões estáveis ao iniciar.
Em **Definições → Atualizações da aplicação** podes desativar essa consulta,
procurar manualmente ou aceitar uma atualização. A consulta usa apenas o serviço
de versões do GitHub e não envia dados financeiros. A instalação exige confirmação,
verificação do pacote e encerramento seguro da aplicação. As instalações portáteis
e a execução pelo código abrem a página da versão para instalação manual.

O código de publicação gera os pacotes num ambiente Windows isolado e publica-os
apenas se os testes e verificações definidos no processo passarem. A existência
do código no repositório não significa que já exista um pacote aprovado em Releases.
Os executáveis podem ser distribuídos sem assinatura Authenticode enquanto não
existir um certificado configurado; nesse caso o Windows pode mostrar um aviso.

Para desenvolver: usa CPython 3.14 normal, de 64 bits, num ambiente virtual;
instala `requirements-dev.txt` e executa `python main.py`. Os testes usam perfis
sintéticos isolados. Para publicar uma versão, atualiza `utils/version.py` e as
notas da versão, cria a etiqueta `vX.Y.Z` no mesmo commit e envia-a para o GitHub.
A configuração em `.github/workflows` testa o código e gera a distribuição Windows.
Não adiciones bases de dados, anexos, backups, credenciais ou exportações de conversas.

## Estado da implementação auditada

Em 30 de agosto de 2026 foi concluída a **Fase 1 — cálculos financeiros e
fiscalidade** na árvore de código atual:

- saldo canónico por conta com `reference_balance` e `reference_date`;
- Dashboard, séries, cashflow, runway e ponte patrimonial reconciliados;
- snapshots mensais e diários alinhados com o saldo canónico;
- holdings ligados apenas são descontados quando a conta associada existe;
- ligações quebradas permanecem visíveis no património e na qualidade dos dados;
- FIFO fiscal rejeita quantidade, preço e taxas inválidos e nunca consome lotes
  adquiridos depois da data da venda;
- 139 testes financeiros/de integração, análise Ruff, compilação e autoteste da
  aplicação foram aprovados.

Também foi concluída a **Fase 3 — interface e acessibilidade**, preservando as
correções já presentes e completando a limpeza das animações:

- navegação rápida e encerramento sem efeitos de opacidade abandonados;
- callbacks desligados antes de destruir animações, eliminando o crash Qt
  reproduzido na sequência de testes de navegação/acessibilidade;
- seletores de movimentos com rótulos associados e nomes acessíveis distintos
  que não mudam com a seleção ou com texto personalizado;
- 131 testes conjuntos aprovados, além de Ruff, compilação e autoteste isolado
  pelo código com 30 páginas. Não inclui teste manual com leitor de ecrã.

A **Fase 2** foi revalidada durante a Fase 4: backups sem commits laterais,
caminhos da base injetada, migrações com esquema/versão atómicos e transferências
que respeitam a transação do chamador. Foram aprovados 80 testes dirigidos.

A **Fase 4 está em validação**: smokes integrados e isolados, recuperação de
importações após rollback, formatação global e ambiente de build separado.
O inventário completo dos problemas, ficheiros alterados, testes e pendências
está no [registo vivo da auditoria](RELEASE_NOTES.md).
O ZIP anterior só deverá ser substituído depois da QA e dos autotestes do novo
executável/instalador. O manifesto do pacote novo identificará o commit e a tag
exatos que lhe deram origem.












## Privacidade e IA opcional

O funcionamento principal é local e não precisa de serviços externos.
A área **IA online** é opcional e usa Gemini apenas depois de ser configurada e
acionada pelo utilizador. A chave é guardada no Gestor de Credenciais do
Windows, não no código nem nos backups exportados.

## Abrir a aplicação pelo código

Requer **Windows de 64 bits** e **CPython 3.14.7** (versão normal, não
free-threaded). O PySide6 atual ainda exige Python inferior a 3.15:

```bat
py -3.14 -m pip install --upgrade pip
py -3.14 -m pip install -r requirements.txt
run_windows_liquid_glass.bat
```

Para diagnóstico:

```bat
run_windows_liquid_glass_debug.bat
```

## Usar o pacote Windows

O ficheiro `LiquidGlass_App.zip` contém a versão pronta a usar. A forma
recomendada num computador novo é:

1. Extrair **todo o ZIP** para uma pasta normal.
2. Abrir `LiquidGlass\INSTALAR_FINANCAS_PESSOAIS.bat` com duplo clique.
3. Usar o atalho criado no Ambiente de Trabalho ou no menu Iniciar.
4. Restaurar um backup da instalação anterior, se aplicável.

O instalador funciona apenas para o utilizador atual e **não deve ser aberto
como administrador**. Copia a aplicação de forma verificada para
`%LOCALAPPDATA%\Programs\FinancasPessoaisLiquidGlass`, cria os atalhos e abre-a.
Também é possível usar a versão portátil sem instalar, através de
`ABRIR_FINANCAS_PESSOAIS.bat` ou `LiquidGlass.exe` na pasta extraída.

Este é um pacote portátil em modo *one-folder*: já leva o interpretador Python,
PySide6/Qt, cryptography, bibliotecas C/C++ e os restantes módulos necessários.
Não instala dependências no Windows e o computador de destino não precisa de
Python nem de pip. A pasta `_internal` tem de permanecer ao lado do executável;
copiar apenas `LiquidGlass.exe` não funciona.

PyInstaller, Ruff, pytest e pip-audit são ferramentas usadas para construir e
validar a aplicação. As respetivas versões ficam registadas no manifesto, mas
não são copiadas nem instaladas no computador de destino porque não são
necessárias para executar o programa.

Antes de substituir uma instalação anterior, o instalador confirma o manifesto,
o SHA-256, o tamanho e a lista completa de ficheiros, incluindo Python, Qt,
PySide6, SQLite, cryptography, Visual C++ e os plugins gráficos. A atualização é
preparada numa pasta temporária e só depois substitui a aplicação anterior.

O pacote distribuível não inclui bases de dados, anexos, palavras-passe ou
outros dados pessoais.

As notas técnicas e o resumo das versões também são incluídos no pacote.

## Funcionalidades principais

- Dashboard, panorama e Centro financeiro.
- Movimentos, filtros, importação, documentos e divisões.
- Orçamentos, objetivos, subscrições e planeamento.
- Carteira, património, risco, resiliência e Score Financeiro 2.0.
- Simulações de hábitos, investimento, FIRE e juros compostos.
- Comparadores, previsões, sazonalidade e concentração 80/20 de comerciantes.
- Fluxo acumulado de entradas/saídas, margem mensal e taxa de poupança.
- Previsões de tesouraria e orçamento recalibradas pelos erros de meses
  concluídos, com precisão e faixas aprendidas visíveis.
- Painel de detalhe ao clicar nos gráficos, com os movimentos e divisões por
  categoria que explicam cada valor.
- Seleção fixa em todos os gráficos interativos para comparar dois pontos com
  diferenças absolutas e percentuais.
- Ponte do património que reconcilia receitas, despesas, carteira e passivos,
  distinguindo claramente histórico completo de valores não classificáveis.
- Evolução mensal das subscrições e pagamentos agendados conhecidos para os
  próximos 12 meses, sem inferir compromissos artificiais.
- Contexto temporal, atualidade e confiança visíveis nos principais cartões.
- Categorias ligadas entre gráficos por destaque visual interativo.
- Larguras e visibilidade das colunas guardadas com identificadores estáveis.
- Prioridades financeiras locais com impacto, evidência, confiança e ação direta.
- Histórico reversível para desfazer importações, criação, duplicação, divisão
  e geração automática de movimentos, incluindo o estado associado.
- Backups completos automáticos, manuais e cifrados, com teste de recuperação.
- Segunda localização opcional para cópias num disco externo, NAS ou pasta
  sincronizada.
- Proteção verificada e reposição automática antes de cada atualização.
- Áreas de trabalho consolidadas para Análises, Saúde, Simulações e Assistente.

## Dados locais

A partir da versão `v3.2.0`, o programa e os dados ficam separados. Por
predefinição, toda a informação persistente é guardada no perfil do Windows:

```text
%LOCALAPPDATA%\FinancasPessoais\
├── dados\
│   ├── financas_pessoais.sqlite3
│   ├── anexos\
│   ├── backups\
│   │   └── historico_alteracoes\
│   └── configuracao\backup_preferences.json
├── logs\
└── protecoes_de_versao\
```

Na primeira abertura da `v3.2.0`, uma instalação anterior junto ao executável
é copiada para esta localização através do mecanismo de backup online do
SQLite. A cópia é validada, comparada e publicada de forma atómica. Os
ficheiros antigos não são apagados.

Na primeira abertura de cada versão, a aplicação também cria uma proteção
verificada em `protecoes_de_versao` antes de escrever na base. Movimentos,
documentos, histórico e preferências anteriores são confirmados após a
atualização; se algo for alterado ou a atualização for interrompida, o estado
anterior é reposto automaticamente.

Desde a `v3.25.0`, são conservadas até três proteções automáticas de atualização
e, em conjunto, até 5 GB. A proteção da atualização atual e qualquer ficheiro
referenciado por um processo de recuperação permanecem protegidos; backups
manuais e cópias de restauro não entram nesta limpeza.

Só pode existir uma instância da aplicação em execução, evitando duas escritas
ou um restauro concorrente sobre os mesmos dados.

Antes de transferir a aplicação para outro computador, cria um backup completo
em **Definições → Backups**. Não copies a base de dados enquanto a aplicação
estiver aberta.

## Desenvolvimento e validação

```bat
py -m pip install -r requirements-dev.txt
python -m ruff check .
python -m pip_audit -r requirements.txt --progress-spinner off
python -m pytest -q
python scripts\validate_visual_rendering.py
python scripts\validate_visual_stress.py
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
```

Antes de criar o ZIP, a construção confirma as versões fixadas, executa
`pip check`, verifica os runtimes incluídos e abre o executável num perfil
temporário para carregar todas as páginas e testar uma base descartável. O
manifesto dentro do pacote regista as versões efetivamente incorporadas. As
pastas intermédias `build` e `dist` são removidas no fim; para as conservar,
usar `scripts\build_windows.ps1 -KeepWorkFolders`.

Os comandos de validação visual usam o motor real do Windows e criam capturas
em `artifacts\visual_validation` e `artifacts\visual_stress`. A validação
`offscreen` continua a testar a geometria rapidamente, mas não é usada para
aprovar fontes ou emojis.
A matriz de stress acrescenta 30 categorias com nomes longos, valores extremos,
estados vazios e escalas de 100%, 125% e 150%, sempre num perfil temporário.

O repositório Git é local. Bases de dados, anexos, backups, logs e pacotes
gerados estão excluídos pelo `.gitignore`.
