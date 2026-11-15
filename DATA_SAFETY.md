# Proteção dos dados entre versões

Os movimentos, documentos anexados, preferências e restantes informações
locais são tratados como dados persistentes que uma atualização não pode
substituir silenciosamente. A partir da versão `v3.1.0`, cada mudança de versão
passa por uma barreira obrigatória antes de a base de dados poder ser aberta
para escrita.

## Onde ficam os dados

A partir da `v3.2.0`, os dados deixam de ficar dependentes da pasta do programa:

```text
%LOCALAPPDATA%\FinancasPessoais\dados
```

Esta pasta contém a base, anexos, backups, histórico reversível e preferências.
Os logs e as proteções de versão ficam em pastas irmãs no mesmo perfil.

O instalador por utilizador guarda apenas os ficheiros do programa em:

```text
%LOCALAPPDATA%\Programs\FinancasPessoaisLiquidGlass
```

O instalador recusa usar a pasta dos dados como destino. Uma atualização é
copiada e validada numa pasta temporária; a versão anterior só é trocada depois
de a lista, o tamanho e o SHA-256 de todos os ficheiros confirmarem o manifesto.
Assim, remover ou substituir a pasta do programa não remove movimentos, anexos
ou backups.

Na primeira abertura, os dados de uma instalação legada são copiados através
da API de backup online do SQLite. A base e os ficheiros são verificados antes
de a pasta nova ser publicada de forma atómica. A origem antiga não é apagada.
Se for encontrado um destino parcial ou uma cópia inválida, o arranque é
bloqueado sem escrever por cima de nenhum dos dois lados.

## O que acontece ao abrir uma nova versão

1. A base de dados atual é aberta apenas para leitura.
2. São verificados a integridade SQLite, as relações entre tabelas e a
   compatibilidade da estrutura.
3. É criada uma proteção fora da pasta do programa, em:

   ```text
   %LOCALAPPDATA%\FinancasPessoais\protecoes_de_versao
   ```

4. A proteção inclui:

   - base de dados completa;
   - documentos da pasta `anexos`;
   - histórico reversível da pasta `backups\historico_alteracoes`;
   - preferências locais de backup;
   - manifesto com versões, quantidades, tamanhos e hashes SHA-256.

5. Quando a cifragem de backups está ativa, a proteção usa `.lgbak` com AES-GCM;
   caso contrário mantém o ZIP verificável. O conteúdo é novamente lido e
   validado antes de a aplicação abrir a base de dados em escrita.
6. Depois da atualização, a aplicação confirma que todas as linhas e todos os
   ficheiros que já existiam continuam intactos.

Uma atualização pode acrescentar estruturas e informação nova, mas não pode
remover nem modificar silenciosamente os dados anteriores. Se essa validação
falhar, o arranque é cancelado e a proteção anterior é reposta
automaticamente.

## Falha ou interrupção durante uma atualização

Enquanto uma nova versão está a preparar os dados, existe um marcador externo
de “atualização em curso”. Se o computador, o processo ou a aplicação forem
interrompidos, o arranque seguinte deteta esse marcador e repõe a proteção
verificada antes de voltar a escrever.

O marcador só é removido depois de a nova estrutura, os dados e os documentos
passarem todas as verificações.

A partir da `v3.36.0`, os documentos geridos deixam de depender de caminhos
absolutos do computador. A base guarda apenas a localização relativa dentro de
`anexos`. Uma mudança de localização ou um restauro só é aceite depois de cada
anexo referenciado ser encontrado na nova pasta e de o tamanho e a assinatura
SHA-256 corresponderem ao registo. Se faltar um ficheiro ou existir uma
alteração, o restauro normal é revertido pela proteção anterior.

## Falha ou interrupção durante um restauro

A partir da `v3.11.0`, um restauro cria primeiro uma proteção completa adicional
e um marcador externo de “restauro em curso”. A proteção inclui a base de dados,
anexos, histórico reversível e preferências existentes antes da substituição.

Se a aplicação, o processo ou o computador forem interrompidos entre a troca
destes componentes, o arranque seguinte repõe automaticamente a proteção
anterior antes de permitir qualquer escrita. O marcador só é removido depois
de a base recuperada passar uma verificação SQLite completa. Se a recuperação
não puder ser provada, o arranque é bloqueado e os ficheiros de proteção são
mantidos.

## Proteção contra regressões de versão

Uma versão antiga da aplicação não pode abrir em escrita dados que já tenham
sido usados por uma versão mais recente. Também é recusada qualquer base cuja
estrutura seja posterior à suportada pela aplicação instalada.

Nestes casos, a aplicação mostra uma mensagem de proteção e mantém os
ficheiros sem alterações.

## Consultar as proteções

Em **Definições → Localização dos dados**, a aplicação mostra:

- localização da base de dados;
- localização dos documentos;
- pasta dos backups normais;
- pasta externa das proteções de versão;
- estado da barreira de proteção.

O botão **Abrir proteções de versão** dá acesso direto aos ZIPs ou `.lgbak`
verificados.
Estas proteções não são eliminadas pela retenção automática dos backups
regulares.

## Índices de pesquisa derivados

Os índices FTS usados para acelerar a pesquisa não são dados financeiros: são
estruturas derivadas que podem ser reconstruídas a partir dos movimentos. A
partir da `v3.35.0`, o índice ativo recebe também a verificação interna FTS5,
além das verificações SQLite gerais.

A migração 25 remove exclusivamente um índice FTS antigo e os seus três
triggers obsoletos. Antes dessa reparação é criada a proteção integral da
versão anterior, e as impressões digitais das tabelas reais são comparadas
depois da migração. Assim, uma falha num índice de pesquisa não volta a ser
confundida com perda dos movimentos nem pode continuar a bloquear silenciosamente
as respetivas gravações.

## Encerramento protegido

Ao fechar normalmente a aplicação, a janela só desaparece depois desta
sequência:

1. novas tarefas de pesquisa, IA, cálculo ou backup deixam de arrancar;
2. as tarefas já iniciadas recebem um pedido de cancelamento e são aguardadas;
3. larguras e visibilidade das tabelas, preferências de backup e IA e estado
   das Simulações ainda em espera são gravados;
4. todas as alterações SQLite recebem um `commit` obrigatório;
5. se o backup ao fechar estiver ativo, o pacote completo é criado e validado;
6. a base passa uma verificação rápida de integridade;
7. o registo WAL é consolidado no ficheiro principal e a ligação é fechada
   explicitamente.

A ligação SQLite usa WAL, espera até 30 segundos por bloqueios transitórios e
o nível `synchronous=FULL`, adequado ao baixo volume de escrita desta aplicação
financeira. Se a gravação, a verificação, a sincronização ou o backup solicitado
falhar, o encerramento é cancelado, a janela volta a ficar utilizável e é
mostrada a causa. A aplicação já não transforma uma falha final apenas numa
linha de log para depois fechar.

Os documentos anexados são primeiro copiados para um ficheiro temporário,
sincronizados com o disco e comparados com o original através de tamanho e
SHA-256. Só depois a cópia completa recebe o nome definitivo e o respetivo
registo é guardado na base.

A partir da `v3.13.0`, esse tamanho e SHA-256 ficam também persistidos na base.
O Arquivo de documentos volta a calculá-los na verificação e imediatamente
antes de abrir cada anexo. Uma mudança posterior é bloqueada, mesmo quando o
ficheiro adulterado conserva exatamente o mesmo tamanho. Registos antigos são
assinalados como ainda não verificados e recebem a sua primeira impressão
digital apenas quando o ficheiro existente passa a validação.

Os anexos novos têm um limite de `50 MB`, uma lista explícita de extensões e
uma verificação da assinatura real do formato. PDF e imagens verificados são
mostrados dentro da aplicação; formatos de folha de cálculo e Word exigem uma
confirmação antes de serem entregues a uma aplicação externa.

Os CSV produzidos pela aplicação neutralizam campos textuais que uma folha de
cálculo poderia interpretar como fórmulas. Esta proteção altera apenas a
representação exportada; o texto e os valores monetários guardados na base não
são modificados.

## Leituras, cancelamento e validação estrutural

A partir da `v3.12.0`, os cálculos em segundo plano abrem exclusivamente uma
base que já exista e usam ligações SQLite em modo de leitura. Cada operação
obtém uma fotografia transacional consistente, para que uma tabela e os seus
totais nunca representem instantes diferentes. Ao atualizar ou fechar, as
consultas ainda em execução recebem uma interrupção real do SQLite.

A própria base recusa novas datas, números, percentagens, estados e estruturas
JSON que não cumpram o formato esperado. As ocorrências de pagamentos
agendados passam também a estar ligadas por chaves estrangeiras ao agendamento
e, quando aplicável, ao movimento criado. Uma migração não apaga relações
órfãs: interrompe-se e mantém a proteção anterior para permitir a correção.

Um registo antigo inválido não é convertido silenciosamente em zero nem
apagado. Continua visível para revisão, é contado no aviso de qualidade e fica
fora dos totais, previsões e scores até ser corrigido. Os valores monetários
válidos continuam armazenados como euros decimais.

A partir da `v3.24.0`, movimentos, subscrições e pagamentos configurados com
valor efetivamente zero são recusados também na base de dados. Eventuais zeros
legados não são apagados: ficam sinalizados e excluídos dos cálculos. Alterações
de investimentos, dívidas e subscrições são confirmadas juntamente com a sua
fotografia histórica, eliminando o estado intermédio em que apenas uma das duas
gravações tinha sido concluída.

Na `v3.25.0`, criar, duplicar, transferir, dividir ou gerar movimentos
recorrentes grava também uma entrada de Desfazer na mesma transação SQLite. Ao
desfazer uma cobrança automática, são removidos o movimento e a ocorrência e
são repostas as datas da subscrição, o progresso do pagamento agendado ou o
valor do objetivo afetado. Uma falha ao escrever o histórico recua toda a
operação.

Alterar a categoria principal de um movimento dividido remove as divisões
anteriores na mesma operação, impedindo que os gráficos continuem a usar uma
classificação invisível. O histórico conserva essas divisões para permitir a
reposição integral.

## Backups regulares completos

Os backups manuais, automáticos e ao fechar incluem:

- base de dados;
- documentos anexados;
- histórico para desfazer;
- preferências de backup;
- manifesto com hashes SHA-256.

Antes de ser aceite, cada backup é novamente aberto e a base passa uma
verificação SQLite completa. O restauro de um pacote novo substitui os quatro
âmbitos como uma única operação e cria primeiro um backup completo do estado
atual. Os formatos antigos continuam disponíveis para recuperação.

O estado “verificado” é recalculado a partir do SHA-256 atual e do conteúdo do
pacote. Nome e tamanho iguais já não são suficientes. A extração é recusada
quando o pacote ultrapassa os limites de quantidade, tamanho, profundidade ou
compressão, contém caminhos inseguros ou não existe espaço livre suficiente.

A retenção automática atua separadamente sobre backups automáticos e backups
ao fechar. Backups manuais, exportados e cópias criadas antes de um restauro não
são eliminados automaticamente.

As proteções criadas automaticamente antes de uma atualização têm uma retenção
própria desde a `v3.25.0`: ficam as três mais recentes, até ao limite conjunto
de 5 GB. A cópia da atualização acabada de verificar e todos os arquivos
referenciados por marcadores de atualização ou restauro são sempre preservados.
Esta limpeza só corre depois de a nova estrutura e os dados terem sido
validados; ZIPs manuais e pacotes de recuperação não são abrangidos.

Em **Definições → Backups**, o botão **Testar recuperação** repõe uma cópia numa
pasta temporária, valida-a e remove-a, sem alterar os dados ativos.

Na `v3.24.0`, conteúdo decifrado existe apenas numa subpasta temporária privada
da aplicação. Essa pasta é removida normalmente e é também limpa no arranque
seguinte caso uma falha de energia ou um encerramento forçado tenha impedido a
limpeza anterior. O painel considera “cifrado” apenas o ficheiro mais recente
cujo cabeçalho confirma realmente o formato `.lgbak`; ativar a opção não altera
retroativamente um ZIP já existente.

Pode ser configurada uma segunda localização fora da pasta de dados. A
aplicação compara essa cópia por SHA-256; se o disco ou rede estiver
indisponível, o backup principal continua válido e a falha fica visível.

## Uma instância de cada vez

Um bloqueio do perfil é adquirido antes da migração ou da abertura da base e
mantido até a aplicação fechar. Assim, duas janelas não podem escrever ou
restaurar simultaneamente os mesmos dados.

## Formato dos valores monetários

A `v3.2.0` não converte os valores para cêntimos inteiros. Os campos monetários
continuam a guardar euros decimais e a interface usa o formato `xx,xx €`.
Migrações e backups copiam estes registos sem transformar os montantes.

## Preferências visuais da v3.14.0

A `v3.14.0` acrescenta apenas preferências de interface na tabela já existente
de definições: densidade por página, filtros principais, painéis recolhidos e
posição de zoom/deslocamento dos gráficos. Estas chaves não modificam linhas
de movimentos, documentos, contas, orçamentos, subscrições, objetivos ou
investimentos.

Quando a janela é fechada, estas preferências são incluídas na mesma fase de
gravação anterior ao `commit`, à verificação SQLite e ao fecho da ligação. Um
evento visual tardio, já depois da ligação fechada, é ignorado em vez de tentar
abrir uma nova escrita.

O modo privacidade altera somente a cópia criada por uma exportação: nos CSV
abrangidos, valores e campos sensíveis são substituídos por marcadores. O ZIP
de backup permanece deliberadamente integral para poder recuperar todos os
dados e o botão identifica essa finalidade. Ativar privacidade nunca reescreve
nem mascara os valores existentes na base.

## Limite importante

Esta barreira protege contra erros de atualização da aplicação e contra
interrupções durante esse processo. Uma segunda localização reduz o risco de
falha física do disco principal, mas não é por si só uma proteção absoluta
contra perda do perfil do Windows, roubo ou ransomware.

O encerramento protegido garante as alterações que já foram confirmadas com
**Guardar**, **Adicionar**, **Aplicar** ou equivalente e as preferências com
gravação automática. Texto ainda escrito numa janela de edição que nunca foi
confirmado não constitui um registo financeiro e não é gravado ao fechar essa
janela. Também não existe uma garantia física absoluta contra encerramento
forçado do processo, avaria súbita do disco, falha de hardware que ignore
pedidos de sincronização, roubo ou ransomware; por isso continuam a existir os
backups completos e a segunda localização opcional.

Continua recomendado criar periodicamente um backup completo ou cifrado em
**Definições → Backups**.
