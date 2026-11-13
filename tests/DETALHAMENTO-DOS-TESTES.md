# Detalhamento dos Testes: cert-monitor

Este documento explica **cada teste** do [caderno de testes](CADERNO-DE-TESTES.md): por que ele existe, que risco cobre, como funciona, quando passa e o que investigar quando falha. O caderno é o roteiro de execução; este documento é a referência para entender e manter os testes.

## Sumário
1. [Visão geral da suíte](#1-visão-geral-da-suíte)
2. [Como a suíte funciona por dentro](#2-como-a-suíte-funciona-por-dentro)
3. [Lint e manifestos (L01–L04)](#3-lint-e-manifestos-l01l04)
4. [Testes do playbook (P01–P23)](#4-testes-do-playbook-p01p23)
5. [Testes das regras de alerta (A01–A07)](#5-testes-das-regras-de-alerta-a01a07)
6. [Testes no cluster (C01–C11)](#6-testes-no-cluster-c01c11)
7. [Evidência de que os testes detectam regressões](#7-evidência-de-que-os-testes-detectam-regressões)
8. [Como executar só uma parte e como adicionar testes](#8-como-executar-só-uma-parte-e-como-adicionar-testes)

---

## 1. Visão geral da suíte

| Camada | IDs | Ferramenta | Onde roda | O que garante |
|---|---|---|---|---|
| Lint e manifestos | L01–L04 | yamllint, ansible-lint, kustomize, kubeconform | Local e CI | Os arquivos são válidos e os manifestos são aceitos pela API |
| Playbook | P01–P23 | `tests/test_playbook.py` (unittest + ansible-playbook) | Local e CI | A varredura extrai, filtra, analisa e envia as métricas corretamente |
| Regras de alerta | A01–A07 | `tests/alertas.test.yaml` (promtool) | Local e CI | Os alertas disparam, não disparam e **resolvem** nas condições certas |
| Cluster | C01–C11 | `oc` + console | Manual, após deploy | Integração real: RBAC, NetworkPolicy, coleta, Alertmanager |

O ponto de entrada automatizado é `tests/run.sh`. Ele executa as camadas em ordem, continua mesmo se uma etapa falhar (para mostrar todos os problemas de uma vez) e termina com um resumo: etapas puladas por falta de ferramenta e etapas que falharam. O código de saída é diferente de zero se qualquer etapa falhar, e é isso que reprova o CI.

---

## 2. Como a suíte funciona por dentro

### 2.1 O playbook roda de verdade, sem cluster
A função `executar()` (em `test_playbook.py`):
1. Lê o `check-certs.yaml` **do repositório** e remove as tasks `kubernetes.core.k8s_info`, as únicas que falam com o cluster.
2. Injeta via `-e @vars.json` as variáveis que essas tasks produziriam: `exclude_cm`, `secrets_brutos` e `configmaps_brutos`. Assim, todo o resto do playbook (extração, filtros, análise, relatório RFC, payload, envio) roda sem alteração.
3. Sobe um **Pushgateway falso** (`class Pushgateway`), um servidor HTTP em `127.0.0.1` numa porta livre que registra método, caminho e corpo de cada requisição.
4. Roda `ansible-playbook` com `PUSHGATEWAY_URL` apontando para o servidor falso e com o `PYTHONWARNINGS` **lido do CronJob** (`manifestos-rbac-cronjob.yaml`). Se alguém alterar o filtro no manifesto, o teste usa o valor alterado.
5. Devolve `Resultado`: código de saída, saída completa (stdout + stderr), requisições recebidas e duração.

Por padrão, as exclusões vêm do `configmap-exclude.yaml` **real** do repositório (`exclude='padrao'`). Os testes de exclusão validam, portanto, a configuração que vai para produção, e não uma cópia.

### 2.2 Certificados de teste
A classe `Cert` gera certificados autoassinados (EC P-256) com a biblioteca `cryptography`:
- `dias`: validade a partir de agora; o teste conhece o `notAfter` exato (`epoch` e `not_after`).
- `cn`: aceita valores fora da RFC (por exemplo, 78 caracteres) usando `_validate=False`.
- `serial`: o `cryptography` se recusa a emitir série ≤ 0. Para simular esses certificados, o teste gera um com série 1 e **edita o DER**, trocando o byte do número de série por `0x00` (zero) ou `0xFF` (-1). A assinatura fica inválida, o que não importa, porque o playbook só lê os campos e não valida a assinatura.
- `crlf`: gera o PEM com quebras de linha `\r\n` (arquivos vindos do Windows).

### 2.3 Leitura e validação do payload
A função `interpretar()` lê o texto enviado ao Pushgateway e:
- valida cada linha contra o formato de exposição do Prometheus (`nome{rótulos} valor`);
- interpreta os rótulos respeitando os escapes (`\"`, `\\`, `\n`);
- **falha em série duplicada**: o Pushgateway rejeita o envio inteiro quando duas linhas têm o mesmo nome e os mesmos rótulos;
- **falha em métrica sem `# HELP` e `# TYPE`**.

`certificados(series, **filtro)` devolve as séries de `cert_not_after_timestamp_seconds` que casam com os rótulos pedidos. `escalar(series, nome)` devolve o valor de uma métrica única, como `cert_monitor_parse_errors`.

### 2.4 Regras de alerta
O `promtool` não lê o objeto `PrometheusRule` do Kubernetes, só o formato de regras do Prometheus. O `run.sh` extrai o campo `spec` do `custom_alerts.yaml` para um arquivo temporário `regras.yaml`, roda `promtool check rules` (sintaxe) e depois `promtool test rules` (comportamento). Assim, as regras testadas são sempre as do repositório.

No promtool, o relógio começa em `time() = 0`. Por isso, nos testes, o valor da série `cert_not_after_timestamp_seconds` é diretamente "segundos até expirar" (por exemplo, 20 dias = `1728000`). O `promtool` v3 compara também as **anotações**, então os testes verificam o texto final que chega ao Alertmanager.

---

## 3. Lint e manifestos (L01–L04)

### L01: yamllint
- **Por que existe:** YAML com indentação errada, chaves duplicadas ou valores ambíguos (`yes`/`no` virando booleano) quebra manifestos e o playbook de forma silenciosa.
- **Como funciona:** `yamllint .` com as regras do `.yamllint`, em todos os YAML do repositório, inclusive os testes e o workflow.
- **Passa quando:** não há erros.
- **Se falhar:** a saída mostra arquivo, linha e regra. Erros comuns: falta de quebra de linha no fim do arquivo e comentário com indentação errada dentro de bloco `|`. Um caso desses já aconteceu no `configmap-exclude.yaml`: o comentário encerrava o bloco antes da hora.

### L02: ansible-lint
- **Por que existe:** detecta más práticas e erros no playbook (módulos sem FQCN, tasks sem nome, uso arriscado de `ignore_errors`).
- **Como funciona:** `ansible-lint check-certs.yaml`.
- **Passa quando:** atinge o perfil `production` sem falhas. O aviso sobre `braces.max-spaces-inside` no `.yamllint` é esperado e não reprova.
- **Se falhar:** a regra indicada (por exemplo `name[casing]`, `jinja[spacing]`) tem link para a documentação. Corrija o playbook em vez de desabilitar a regra.

### L03: kustomize build
- **Por que existe:** o deploy é feito com `oc apply -k .`. Se o `kustomization.yaml` referencia um arquivo inexistente ou um manifesto está malformado, o deploy quebra.
- **Como funciona:** `kubectl kustomize .`, descartando a saída.
- **Passa quando:** o comando termina sem erro.
- **Se falhar:** verifique a lista `resources` do `kustomization.yaml` e o YAML do arquivo citado.

### L04: kubeconform
- **Por que existe:** valida cada recurso contra o schema da API do Kubernetes, o que pega campos inexistentes, tipos errados e campos na posição errada (por exemplo, `automountServiceAccountToken` fora de `spec.template.spec`).
- **Como funciona:** `kubectl kustomize . | kubeconform -strict`, com os schemas padrão e o catálogo de CRDs (datreeio) para `ServiceMonitor` e `PrometheusRule`. `ImageStream` e `BuildConfig` são pulados, porque não há schema público para eles.
- **Passa quando:** todos os recursos são válidos. Com `-strict`, campos desconhecidos também reprovam.
- **Se falhar:** a mensagem indica o recurso e o caminho do campo. Localmente, a etapa é pulada se o `kubeconform` não estiver instalado; no CI ele é instalado com o sha256 conferido.

---

## 4. Testes do playbook (P01–P23)

### Cenário principal (P01–P16)
Os testes P01 a P16 compartilham **uma única execução** do playbook (`CenarioPrincipal.setUpClass`), com um conjunto de recursos que exercita todos os caminhos ao mesmo tempo. Isso deixa a suíte rápida (cerca de 10 s no total). Se essa execução falhar, todos os testes do cenário aparecem como `ERROR: setUpClass`, com o fim do log do Ansible na mensagem.

Recursos do cenário principal:

| Recurso | Conteúdo | Usado em |
|---|---|---|
| `app/tls-normal` (Secret) | `tls.crt` (30 dias) e `tls.key` com chave privada fictícia | P03, P06, P10, P11 |
| `app/bundle` (Secret) | `ca.crt` com 3 certificados encadeados | P04 |
| `app/extensoes` (Secret) | O mesmo certificado em `a.pem`, `b.cer`, `c.cert`, `d.txt` e `e.key` | P05 |
| `app/invalido` (Secret) | PEM com corpo inválido | P08, P09 |
| `app/escape` (Secret) | CN `a"b\c` + quebra de linha + `d` | P12 |
| `app/crlf` (Secret) | PEM com `\r\n` | P13 |
| `app/rfc` (Secret) | CN de 78 bytes, série 0 e série -1 | P15, P16 |
| `openshift-etcd`, `openshift`, `kube-system`, `default` | Certificados que devem ser ignorados | P07 |
| `openshift-ingress/router-certs`, `openshift-config/user-ca-bundle` | Certificados que devem ser monitorados | P07 |
| `app/cm-data`, `app/cm-bin` (ConfigMaps) | Certificado em `data` e em `binaryData` | P03, P10 |
| `kube-root-ca.crt`, `openshift-service-ca.crt`, `trusted-ca-bundle`, `config-trusted-cabundle` | Nomes excluídos por padrão | P07 |
| `app/meu-trusted-ca-bundle` | Nome parecido com um excluído, que deve ser monitorado | P07 |

#### P01: Envio por PUT no grupo do job
- **Risco coberto:** se o envio usasse `POST`, as métricas de certificados removidos ou renovados **nunca sairiam** do Pushgateway, porque o `POST` só sobrescreve as métricas com o mesmo nome. Os alertas de certificados já renovados ficariam ativos para sempre.
- **Como funciona:** verifica que houve exatamente um envio, com método `PUT` e caminho `/metrics/job/cert_monitor`.
- **Passa quando:** `('PUT', '/metrics/job/cert_monitor')`.
- **Se falhar:** confira `method` e `url` da task "Enviar métricas para o Pushgateway". Mais de um envio indica loop ou task duplicada.

#### P02: Formato do payload
- **Risco coberto:** uma única linha malformada ou duplicada faz o Pushgateway rejeitar o envio inteiro com HTTP 400. O resultado seria nenhuma métrica atualizada e o estado antigo congelado.
- **Como funciona:** `interpretar()` valida todas as linhas, os escapes, a ausência de duplicatas e a presença de HELP/TYPE (seção 2.3).
- **Passa quando:** o payload é lido sem erro e contém séries de certificados.
- **Se falhar:** a mensagem mostra a linha problemática. Causas típicas: rótulo com aspas sem escape, mudança no template do payload, dois certificados gerando o mesmo conjunto de rótulos.

#### P03: Origens (Secret, ConfigMap `data` e `binaryData`)
- **Risco coberto:** Secrets vêm em base64, ConfigMaps `data` vêm em texto puro e `binaryData` vem em base64. Decodificar errado (ou não decodificar) faz o certificado sumir sem nenhum erro.
- **Como funciona:** procura séries de `app/tls-normal` (`tipo=SECRET`), `app/cm-data` (`tipo=CONFIGMAP`, `key=ca.crt`) e `app/cm-bin` (`tipo=CONFIGMAP`, `key=x.pem`).
- **Passa quando:** as três existem com o `tipo` correto.
- **Se falhar:** confira a montagem de `fontes` na task "Extrair e organizar certificados" (flag `b64`).

#### P04: Bundle gera uma série por posição
- **Risco coberto:** bundles e cadeias costumam ter um certificado intermediário ou uma CA vencendo antes do certificado final. Se só o primeiro fosse lido, esse vencimento passaria despercebido.
- **Como funciona:** `app/bundle` tem 3 certificados (CNs `bundle-a`, `bundle-b`, `bundle-c`). O teste exige `indice` 0, 1 e 2, na ordem do arquivo.
- **Passa quando:** `{'0': 'bundle-a', '1': 'bundle-b', '2': 'bundle-c'}`.
- **Se falhar:** confira o `regex_findall(pem_regex)` e o `loop.index0` da extração. Uma regex alterada pode estar juntando dois certificados num só.

#### P05: Extensões aceitas
- **Risco coberto:** o playbook só lê chaves com as extensões de `extensoes_certificado`. Uma lista errada perde certificados (se faltar extensão) ou lê arquivos indevidos.
- **Como funciona:** o mesmo certificado está em `a.pem`, `b.cer`, `c.cert`, `d.txt` e `e.key`.
- **Passa quando:** só `a.pem`, `b.cer` e `c.cert` geram séries.
- **Se falhar:** compare `extensoes_certificado` com o esperado. Se a lista mudou de propósito, atualize o teste **e** o README.

#### P06: Chave privada nunca exposta
- **Risco coberto:** vazamento de segredo. O playbook lê Secrets inteiros, incluindo `tls.key`; nada além dos blocos de certificado pode aparecer nas métricas ou no log do Job.
- **Como funciona:** `app/tls-normal` tem `tls.key` com a marca `SEGREDO-NAO-PODE-VAZAR`. O teste procura essa marca no payload e em toda a saída do Ansible.
- **Passa quando:** a marca não aparece em nenhum dos dois.
- **Se falhar:** **trate como incidente de segurança antes de qualquer deploy.** Causas prováveis: `no_log: true` removido de alguma task, task de debug nova imprimindo dados brutos, ou extensão `.key` incluída na lista.

#### P07: Exclusões padrão
- **Risco coberto:** exclusão ampla demais esconde certificados importantes. Foi o que acontecia com `openshift.*`, que ignorava os certificados custom da API e do router. Exclusão estreita demais gera ruído e alertas que ninguém consegue resolver.
- **Como funciona:** usa o `configmap-exclude.yaml` real. Verifica que são **ignorados** `openshift-etcd`, `openshift`, `kube-system`, `default`, `kube-root-ca.crt`, `openshift-service-ca.crt`, `trusted-ca-bundle` e `config-trusted-cabundle`. Verifica que são **monitorados** `openshift-ingress/router-certs`, `openshift-config/user-ca-bundle` e `app/meu-trusted-ca-bundle`. Este último prova que o casamento é exato e não por trecho do nome.
- **Passa quando:** todas as afirmações valem.
- **Se falhar:** compare as listas `namespaces` e `nomes` do ConfigMap. Se a mudança foi intencional, atualize o teste e o README.

#### P08: PEM inválido conta erro sem derrubar a varredura
- **Risco coberto:** um único certificado corrompido em qualquer namespace não pode interromper o monitoramento do cluster inteiro.
- **Como funciona:** `app/invalido` tem um PEM com corpo `lixo`.
- **Passa quando:** `cert_monitor_parse_errors` vale 1, o playbook termina com sucesso e não há série para `app/invalido`.
- **Se falhar:** confira o `failed_when: false` da task "Analisar a validade de cada certificado" e a indexação em `info_por_pem`.

#### P09: Contagem de entradas
- **Risco coberto:** `cert_monitor_certificates` é usado para acompanhar a cobertura da varredura; uma queda brusca indica certificados sumindo. A semântica documentada precisa ser respeitada.
- **Como funciona:** a métrica conta **entradas** (cada certificado em cada recurso e posição), incluindo PEMs inválidos, que entram na lista mas não viram série.
- **Passa quando:** `cert_monitor_certificates` = número de séries exportadas + 1 (o PEM inválido).
- **Se falhar:** verifique se a métrica ainda usa `lista_certificados | length`. Se a semântica mudou de propósito, atualize o README.

#### P10: Mesmo certificado em vários recursos
- **Risco coberto:** o playbook analisa cada PEM único uma vez (`pems_unicos`) e depois redistribui o resultado. Um erro nessa redistribuição faria uma das cópias sumir ou receber dados de outro certificado.
- **Como funciona:** o mesmo certificado está em `app/tls-normal` (Secret) e `app/cm-data` (ConfigMap).
- **Passa quando:** existe exatamente uma série para cada recurso, e as duas têm o mesmo valor.
- **Se falhar:** confira as tasks "Listar certificados únicos" e "Indexar informações por certificado" (o `zip` entre `pems_unicos` e os resultados).

#### P11: Data de expiração e label `not_after`
- **Risco coberto:** erro de fuso horário ou de formato desloca a data de expiração em horas, ou a anula. Os alertas disparariam cedo ou tarde demais.
- **Como funciona:** compara o valor da série com o epoch UTC exato do `notAfter` do certificado gerado, e o rótulo `not_after` com o formato `AAAAMMDDhhmmssZ`.
- **Passa quando:** os dois batem exatamente.
- **Se falhar:** confira o cálculo de `expiracao` no template do payload (`to_datetime('%Y%m%d%H%M%SZ')` menos `epoch_zero`). Uma diferença de múltiplos de 3600 s indica problema de fuso.

#### P12: Escape do CN
- **Risco coberto:** o CN vem do certificado, ou seja, de dados que qualquer usuário do cluster controla. Aspas, barra invertida ou quebra de linha sem escape quebrariam o payload (ver P02) e permitiriam **injetar linhas de métricas falsas**.
- **Como funciona:** `app/escape` tem CN `a"b\c` + quebra de linha + `d`.
- **Passa quando:** o rótulo `subject_cn`, depois de lido com os escapes, vale `a"b\c d` (a quebra de linha vira espaço).
- **Se falhar:** confira a cadeia de `replace` na variável `cn` do template do payload. A ordem importa: a barra invertida precisa ser escapada primeiro.

#### P13: PEM com CRLF
- **Risco coberto:** certificados copiados do Windows têm `\r\n`. A regex de extração precisa aceitá-los; isso ficou sensível depois que ela passou a aceitar só base64 e espaços.
- **Como funciona:** `app/crlf` tem o PEM com `\r\n`.
- **Passa quando:** a série existe com o epoch correto.
- **Se falhar:** confira se `pem_regex` ainda inclui `\s` (que cobre `\r`) na classe de caracteres.

#### P14: Última execução recente
- **Risco coberto:** o alerta `CertMonitorStale` depende de `cert_monitor_last_run_timestamp_seconds`. Se o valor estiver errado (fuso ou formato), o alerta dispara à toa ou nunca dispara.
- **Como funciona:** compara o valor com o relógio atual.
- **Passa quando:** a diferença é menor que 300 s.
- **Se falhar:** confira `now(utc=true)` e `epoch_zero` no template.

#### P15: Relatório RFC 5280
- **Risco coberto:** a task "Listar certificados fora dos limites da RFC 5280" substitui os avisos crus do `cryptography`. Se ela parar de funcionar, os problemas ficam invisíveis, porque os avisos crus também estão silenciados (ver P16).
- **Como funciona:** `app/rfc` tem um certificado com CN de 78 bytes (`tls.crt`), um com série 0 (`ca.crt`) e um com série -1 (`s.crt`). O teste procura as três mensagens no log, com namespace, nome, chave e posição. Confirma também que um certificado normal não aparece no relatório e que os três continuam sendo exportados.
- **Passa quando:** as três mensagens estão presentes, `app/tls-normal` está ausente e há 3 séries para `app/rfc`.
- **Se falhar:** confira `limites_rfc5280` e o bloco Jinja da task. Se o texto da mensagem mudou de propósito, atualize o teste.

#### P16: Sem avisos crus do cryptography
- **Risco coberto:** os avisos crus poluem o log do Job, não dizem qual certificado é o problema e já confundiram a análise uma vez (um aviso cortado escondia o outro).
- **Como funciona:** com o `PYTHONWARNINGS` do CronJob, procura `UserWarning`, `CryptographyDeprecationWarning` e `errantly sent data directly to stderr` na saída.
- **Passa quando:** nenhum dos três aparece.
- **Se falhar:** algum filtro do `PYTHONWARNINGS` foi removido ou alterado, ou uma nova versão do `cryptography` mudou o texto do aviso. Rode o playbook e copie o início exato da nova mensagem para o filtro. Se for um aviso de outra natureza, considere tratá-lo também no relatório RFC (P15).

### Exclusão (P17–P18)

#### P17: Lista de exclusão vazia e ConfigMap ausente
- **Risco coberto:** o ConfigMap de exclusão é editado à mão. Uma chave vazia fazia `from_yaml` devolver `None` e quebrava o playbook. A ausência do ConfigMap também não pode quebrar a varredura.
- **Como funciona:** duas execuções (subtestes): uma com `namespaces: ''` e `nomes: '[]'`, outra sem o ConfigMap. Em ambas há um certificado em `openshift-etcd`.
- **Passa quando:** o playbook termina com sucesso e o certificado de `openshift-etcd` é monitorado (nada é ignorado).
- **Se falhar:** confira o tratamento `(… | from_yaml) or []` e o fallback `^$` na task "Construir regex de namespaces e recursos ignorados".

#### P18: Regex de exclusão inválida falha de forma visível
- **Risco coberto:** um erro de digitação no ConfigMap (por exemplo, um parêntese sem par) não pode passar em silêncio, e muito menos apagar as métricas existentes.
- **Como funciona:** usa `namespaces: '- "(abc"'`.
- **Passa quando:** o playbook falha (o Job fica com erro, visível em `oc get jobs`) e **nenhum envio** é feito ao Pushgateway, de modo que o último estado bom é preservado.
- **Se falhar:** se o playbook passou a aceitar a regex inválida, alguém adicionou tratamento de erro que mascara o problema. Se houve envio, a ordem das tasks mudou e o envio está acontecendo antes da validação.

### Persistência (P19–P21)

#### P19: Certificado renovado substitui a série antiga
- **Risco coberto:** **alerta persistente depois da renovação.** Como `not_after` é um rótulo, um certificado renovado gera uma série **nova**. Se a antiga não sair, o alerta do certificado velho continua ativo indefinidamente.
- **Como funciona:** duas execuções seguidas no mesmo Secret `app/web`: a primeira com um certificado de 10 dias, a segunda com um de 400 dias.
- **Passa quando:** a primeira execução tem só o `not_after` antigo; a segunda tem **só** o novo, o `not_after` antigo não aparece em nenhum lugar do payload e o envio é `PUT` (que substitui o grupo inteiro no Pushgateway).
- **Se falhar:** verifique se o método voltou a ser `POST`, ou se algum estado está sendo reaproveitado entre execuções.

#### P20: Recurso removido some das métricas
- **Risco coberto:** Secret apagado ou namespace removido deixando alerta "fantasma".
- **Como funciona:** executa a varredura sem o Secret `app/web`.
- **Passa quando:** não há séries com `name="web"`.
- **Se falhar:** mesma investigação do P19.

#### P21: Falha no envio falha o Job
- **Risco coberto:** se o Pushgateway estiver fora do ar e o playbook terminar com "sucesso", ninguém percebe que as métricas pararam de ser atualizadas.
- **Como funciona:** aponta `PUSHGATEWAY_URL` para uma porta fechada (`127.0.0.1:9`).
- **Passa quando:** o playbook termina com erro (código de saída diferente de zero).
- **Se falhar:** confira se a task de envio ganhou `ignore_errors`/`failed_when: false` ou se `status_code` passou a aceitar erros.

### Segurança (P22–P23)

#### P22: Regex de PEM linear
- **Risco coberto:** **negação de serviço.** A regex antiga (`[\s\S]+?`) tinha custo quadrático com valores contendo muitos `BEGIN CERTIFICATE` sem `END`. Qualquer usuário que pudesse criar ConfigMaps num namespace monitorado conseguiria travar a varredura do cluster inteiro: 1 MiB levava cerca de 80 s, e alguns desses ConfigMaps estourariam o prazo de 30 min do Job.
- **Como funciona:** lê `pem_regex` direto do playbook e mede o tempo de `re.findall` sobre cerca de 1 MiB (o tamanho máximo de um ConfigMap) de conteúdo malicioso. O Jinja usa o mesmo `re` do Python, então a medição corresponde ao que o playbook faz.
- **Passa quando:** leva menos de 2 s (na prática, cerca de 0,01 s).
- **Se falhar:** a regex foi alterada para algo que permite backtracking amplo. Mantenha a classe restrita `[A-Za-z0-9+/=\s]` entre os marcadores.

#### P23: ConfigMap malicioso não impede a varredura
- **Risco coberto:** complementa o P22 de ponta a ponta. Além de a regex ser rápida, o certificado válido vizinho precisa continuar sendo monitorado.
- **Como funciona:** executa o playbook com um ConfigMap malicioso de cerca de 1 MiB e um ConfigMap válido.
- **Passa quando:** o playbook termina com sucesso, o certificado válido é exportado e a execução leva menos de 120 s.
- **Se falhar:** veja o P22. Se só o tempo estourou, verifique a máquina (CPU) antes de concluir que houve regressão.

---

## 5. Testes das regras de alerta (A01–A07)

Todos usam `evaluation_interval: 1m` e séries com uma amostra por minuto, como na coleta real (a cada 30 s). Amostras mais espaçadas que 5 min criam buracos na janela do Prometheus e impedem o `for` de completar. Isso aconteceu durante a criação do A04 e foi corrigido no teste, não na regra.

#### A01: Expiração crítica (20 dias e já expirado)
- **Risco coberto:** o alerta mais importante da solução deixar de disparar.
- **Como funciona:** séries `c20` (20 dias) e `vencido` (-1 dia), avaliadas aos 10 min.
- **Passa quando:** `CertificateExpiringCritical` dispara para as duas, com todos os rótulos do certificado, `severity=critical` e o texto renderizado ("expira em 20 dias" / "expira em -1 dias").
- **Se falhar:** compare a expressão e as anotações em `custom_alerts.yaml` com a saída do promtool, que mostra o esperado e o obtido lado a lado.

#### A02: Faixas de aviso (45 dias) e de silêncio (90 dias)
- **Risco coberto:** faixas sobrepostas (o mesmo certificado gerando aviso e crítico) ou alertas para certificados ainda distantes do vencimento.
- **Como funciona:** séries `c45` e `c90`, no mesmo teste do A01.
- **Passa quando:** `CertificateExpiringSoon` dispara **só** para `c45`, `c90` não gera nenhum alerta e `c20` não aparece no aviso, só no crítico.
- **Se falhar:** confira os limites `<= 60` e `> 30` na expressão.

#### A03: Renovação resolve o alerta
- **Risco coberto:** **alerta persistente depois da renovação**, do ponto de vista do Prometheus. É o par do P19: o P19 garante que o playbook remove a série antiga; o A03 garante que, quando ela some, o alerta resolve.
- **Como funciona:** a série antiga (20 dias, `not_after="antigo"`) existe até os 30 min e recebe um marcador `stale` aos 31 min, exatamente o que acontece quando ela sai do Pushgateway. A série nova (400 dias, `not_after="novo"`) começa aos 30 min.
- **Passa quando:** aos 20 min o crítico está ativo para `antigo`; aos 33 min não há nenhum alerta, nem crítico nem aviso.
- **Se falhar:** algo na regra está mantendo o alerta, como uma função que olha para o passado (`max_over_time`, `last_over_time`) ou um `keep_firing_for`.

#### A04: Monitor parado
- **Risco coberto:** o CronJob para de rodar (imagem quebrada, cota, Job falhando) e ninguém percebe. As métricas antigas continuariam lá, com dados cada vez mais desatualizados.
- **Como funciona:** `cert_monitor_last_run_timestamp_seconds` fica parado em 0, ou seja, a última varredura aconteceu no instante 0.
- **Passa quando:** às 25h não há alerta; às 27h `CertMonitorStale` está ativo, com a duração humanizada no texto ("1d 3h 0m 0s").
- **Se falhar:** confira o limite `26 * 3600` e o `for: 10m`. O limite de 26h tolera uma execução perdida com o agendamento de 12h. Se o agendamento mudar, ajuste o limite e o teste.

#### A05: Monitor presente não gera "ausente"
- **Risco coberto:** falso positivo de `CertMonitorAbsent` com o monitor funcionando.
- **Como funciona:** no mesmo cenário do A04 (a métrica existe), avalia às 27h.
- **Passa quando:** `CertMonitorAbsent` não está ativo.

#### A06: Métricas ausentes
- **Risco coberto:** o Pushgateway caiu, o ServiceMonitor quebrou ou o monitor nunca rodou; nenhuma métrica chega ao Prometheus.
- **Como funciona:** só existe uma série sem relação (`up{job="outro"}`).
- **Passa quando:** aos 30 min não há alerta (o `for: 1h` está pendente); aos 70 min `CertMonitorAbsent` está ativo, com o texto esperado.
- **Se falhar:** confira o `absent(...)` e o `for: 1h`.

#### A07: Erros de análise
- **Risco coberto:** certificados que não podem ser lidos ficam fora do monitoramento; o alerta informativo avisa que existem pontos cegos.
- **Como funciona:** `cert_monitor_parse_errors` vale 0 por 5 min e depois 2.
- **Passa quando:** aos 4 min não há alerta; aos 10 min `CertMonitorParseErrors` está ativo, com "2 certificado(s)" no texto.

---

## 6. Testes no cluster (C01–C11)

Validam o que os testes automatizados não alcançam: a integração com o OpenShift real. Faça-os após cada deploy relevante (mudança de imagem, de manifestos ou de alertas).

| ID | O que valida e por quê | Se falhar |
|---|---|---|
| C01 | **Deploy e build.** O `oc apply -k .` aplica tudo e o build da imagem termina com `Complete`. Garante que a imagem roda o playbook atual. | `oc logs build/<nome> -n cert-monitor`: erro de rede no `pip`/`ansible-galaxy`, tag da imagem base inexistente. |
| C02 | **Pushgateway no ar.** Pod `Running` e `READY 1/1` (probes `/-/ready` e `/-/healthy`). Também confirma que o `securityContext` restrito e a falta de token não impedem o pod de subir. | `oc describe pod`: erro de SCC/PSA, imagem não encontrada. |
| C03 | **Job manual conclui.** Execução real com a ServiceAccount, RBAC e acesso à API. | `oc logs job/<nome>`: `403` indica RBAC (ClusterRole/Binding); `ImagePullBackOff` indica build ausente. |
| C04 | **Log limpo.** `failed=0` e nenhum `errantly sent data directly to stderr`. Equivale ao P16 no ambiente real, com os certificados reais do cluster. | Um aviso novo do `cryptography` apareceu. Veja o P16 e, se for o caso, inclua o caso no relatório RFC. |
| C05 | **Métricas chegando ao Prometheus.** Valida ServiceMonitor, User Workload Monitoring e NetworkPolicy na direção da coleta. | `CertMonitorAbsent` deve estar ativo. Verifique o ServiceMonitor e o target em Observe → Targets. |
| C06 | **Sem séries concorrentes por posição.** Se o PUT parar de substituir o grupo, as séries de certificados renovados se acumulam e aparecem aqui. Equivale ao P19 em produção. | Confira o método de envio e se há outro job enviando para o mesmo grupo. |
| C07 | **Regras carregadas.** As 5 regras aparecem sem erro de avaliação. | `oc get prometheusrule -n cert-monitor -o yaml` e os logs do Thanos Ruler/Prometheus do UWM. |
| C08 | **Renovação ponta a ponta.** O cenário que originou estes testes: o alerta dispara com um certificado de 10 dias e **resolve** depois da troca por um de 400 dias e de uma nova varredura. Integra P19 + A03 + Alertmanager. | Se o alerta não resolve: veja C06 (séries acumuladas), confira se o Job da renovação terminou e procure outras cópias com `cert_not_after_timestamp_seconds{subject_cn="teste-renovacao"}`. |
| C09 | **NetworkPolicy bloqueia pods externos.** Um pod do namespace `default` não deve alcançar o Pushgateway; se alcançasse, poderia enviar métricas falsas ou vazias. | Rede do cluster sem suporte a NetworkPolicy, ou policy alterada. |
| C10 | **Pushgateway sem token de ServiceAccount.** O diretório do token não deve existir no container. | `automountServiceAccountToken` removido ou no lugar errado do manifesto. |
| C11 | **Secrets ignorados por tipo.** Tokens de ServiceAccount, `dockercfg`, `dockerconfigjson` e releases do Helm não devem gerar séries. É o único filtro aplicado no servidor (`field_selectors`) e, por isso, não pode ser testado sem cluster. | Confira `secret_types_ignorados` e o `field_selectors` da task "Buscar os Secrets do cluster". |

---

## 7. Evidência de que os testes detectam regressões

Ao criar a suíte, três defeitos reais foram reintroduzidos numa cópia do projeto, um de cada vez, para confirmar que os testes pegam cada um:

| Defeito reintroduzido | Teste que falhou |
|---|---|
| Regex de PEM antiga (`[\s\S]+?`, vulnerável a DoS) | P22 |
| Remoção do filtro `PYTHONWARNINGS` do número de série | P16 |
| Remoção do escape de aspas no CN | Cenário principal inteiro (`setUpClass`), porque o payload ficou inválido |

Ao alterar um teste, repita esse exercício: introduza de propósito o defeito que o teste deveria pegar e confirme que ele falha.

---

## 8. Como executar só uma parte e como adicionar testes

### Execução parcial
```bash
# Todos os testes do playbook
python3 tests/test_playbook.py -v

# Um teste específico (pelo nome completo ou por trecho)
python3 tests/test_playbook.py -v Persistencia.test_P19_certificado_renovado_substitui_a_serie_antiga
python3 tests/test_playbook.py -v -k P22

# Só as regras de alerta (o run.sh faz a extração do spec): rode o run.sh completo
tests/run.sh
```

### Adicionando um teste do playbook
1. Escolha o próximo ID livre (P24, …).
2. Se o caso cabe no cenário principal, acrescente o recurso em `CenarioPrincipal.setUpClass` e crie um método `test_P24_…`. Se ele precisa de execuções próprias (sequência de varreduras, configuração diferente), crie o método numa das classes `Exclusao`, `Persistencia` ou `Seguranca`, ou numa classe nova, chamando `executar(...)`.
3. Use `Cert(...)` para gerar certificados, `secret(...)`/`configmap(...)` para os recursos e `interpretar()` + `certificados()`/`escalar()` para as verificações.
4. Faça o teste falhar de propósito (seção 7) antes de considerá-lo pronto.
5. Registre o novo ID na tabela do `CADERNO-DE-TESTES.md` e explique-o neste documento.

### Adicionando um teste de alerta
1. Acrescente um bloco em `tests/alertas.test.yaml`, com o próximo ID livre (A08, …) no `name`.
2. Use `interval: 1m` e lembre que `time()` começa em 0.
3. Informe `exp_labels` **e** `exp_annotations` completos; o promtool v3 compara os dois.
4. Atualize o caderno e este documento.
