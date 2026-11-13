# OpenShift Certificate Monitor (Ansible + Prometheus)

## O que é e Qual a Função?
Esta solução é um automatismo nativo em Ansible executado via CronJob dentro do OpenShift. Sua função é varrer periodicamente os `Secrets` e `ConfigMaps` do cluster, extrair e validar os certificados x509 (inclusive cada certificado de bundles e cadeias), e exportar a data de expiração de cada um como métrica do Prometheus (`cert_not_after_timestamp_seconds`).

A solução integra-se à stack de monitoramento do OpenShift, gerando alertas no Alertmanager para certificados prestes a expirar e para falhas da própria varredura.

## Estrutura de Arquivos
* `check-certs.yaml`: Playbook Ansible que realiza a varredura e envia as métricas ao Pushgateway.
* `Containerfile`: Instruções de build da imagem (Red Hat UBI 9 com Python 3.12) com permissões ajustadas para OpenShift.
* `requirements.txt` / `requirements.yml`: Versões fixas dos pacotes Python e das collections Ansible usadas na imagem.
* `kustomization.yaml`: Ponto de entrada para implantação via Kustomize.
* `manifestos-rbac-cronjob.yaml`: Namespace `cert-monitor`, ServiceAccount, ClusterRole (apenas get/list em secrets/configmaps), ClusterRoleBinding e o CronJob (às 00:00 e às 12:00, `0 */12 * * *`, no fuso do kube-controller-manager, normalmente UTC).
* `configmap-exclude.yaml`: Regex dos namespaces e dos nomes de recursos ignorados na varredura.
* `pushgateway.yaml`: Deployment, Service, ServiceMonitor e NetworkPolicy do Pushgateway.
* `custom_alerts.yaml`: PrometheusRule com os alertas de expiração e de saúde do monitor.
* `build-config.yaml`: ImageStream e BuildConfig para compilação nativa no cluster.
* `tests/`: caderno de testes (`CADERNO-DE-TESTES.md`), explicação detalhada de cada teste (`DETALHAMENTO-DOS-TESTES.md`), script único `run.sh`, testes do playbook (`test_playbook.py`), das regras de alerta (`alertas.test.yaml`) e das consultas do dashboard (`dashboard_promql.py`).
* `grafana/`: dashboard Grafana para importar (`cert-monitor-dashboard.json`) e instruções de datasource e instalação (`README.md`).
* `.github/workflows/lint.yml` / `.yamllint`: CI que roda `tests/run.sh` e a configuração do yamllint.

## Métricas Exportadas
| Métrica | Descrição |
|---|---|
| `cert_not_after_timestamp_seconds` | Data de expiração (epoch UTC). Labels: `cert_namespace`, `tipo` (`SECRET`/`CONFIGMAP`), `name`, `key`, `indice` (posição no bundle), `subject_cn`, `not_after` (formato `AAAAMMDDhhmmssZ`, UTC). |
| `cert_monitor_certificates` | Quantidade de entradas encontradas na última varredura. O mesmo certificado presente em vários recursos ou posições conta várias vezes. |
| `cert_monitor_parse_errors` | Certificados únicos (PEM) que não puderam ser analisados. |
| `cert_renewed_timestamp_seconds` | Momento em que a renovação do certificado foi detectada (epoch UTC), mantido por 7 dias (`renovacao_retencao_dias`). Mesmos labels de `cert_not_after_timestamp_seconds`, referentes ao certificado novo. |
| `cert_monitor_last_run_timestamp_seconds` | Momento da última varredura concluída (epoch UTC). |

O Pushgateway acrescenta o label `job="cert_monitor"`, e o User Workload Monitoring acrescenta `namespace="cert-monitor"` (por isso o namespace do certificado fica em `cert_namespace`).

Os dias restantes são calculados no próprio Prometheus (`(cert_not_after_timestamp_seconds - time()) / 86400`), portanto continuam diminuindo mesmo que o CronJob pare de rodar.

As métricas são enviadas com `PUT`, que substitui o grupo inteiro: certificados removidos do cluster (ou excluídos via ConfigMap) somem das métricas na execução seguinte.

## Alertas
* `CertificateExpiringSoon` (warning): expira entre 30 e 60 dias (`for: 1m`).
* `CertificateExpiringCritical` (critical): expira em 30 dias ou menos, inclusive já expirados (`for: 1m`).
* `CertificateRenewed` (info): certificado renovado, ativo nas 24 horas seguintes à detecção e resolvido automaticamente.
* `CertMonitorStale` (warning): nenhuma varredura concluída nas últimas 26 horas (`for: 10m`). Com o agendamento de 12 em 12 horas, tolera uma execução perdida.
* `CertMonitorAbsent` (warning): métricas do monitor ausentes (`for: 1h`).
* `CertMonitorParseErrors` (info): certificados que não puderam ser analisados (`for: 1m`).

### Detecção de renovação
A cada varredura, o playbook lê no Pushgateway o resultado da varredura anterior (`GET /api/v1/metrics`) e compara as datas de expiração. Um certificado com uma data de expiração que não existia antes, numa chave já monitorada (mesmo namespace, tipo, nome e chave), é considerado **renovado**: gera `cert_renewed_timestamp_seconds` e o alerta `CertificateRenewed`, e fica verde no dashboard por 24 horas.

* A detecção acontece na varredura seguinte à renovação (até 12 horas, ou na hora com um Job manual).
* Recurso novo não conta como renovação, e reordenar um bundle também não. Incluir um certificado novo num bundle existente conta como renovação daquela chave.
* Se o estado anterior não estiver disponível (primeira execução, Pushgateway recriado ou indisponível na leitura), nenhuma renovação é marcada nessa varredura; as métricas de expiração não são afetadas.

## Dashboard Grafana
O diretório [`grafana/`](grafana/README.md) traz um dashboard pronto para importar, com resumo, tabela de certificados com a situação de cada um, renovações dos últimos 7 dias, faixas de vencimento e saúde do monitor. Os certificados renovados ficam **verdes por 24 horas**. O guia explica como criar a credencial e o datasource apontando para o Thanos Querier do OpenShift.

## Pré-requisitos
* OpenShift 4.x com o User Workload Monitoring habilitado (ver passo 1 abaixo).
* Registry interno de imagens habilitado: o CronJob usa a imagem `image-registry.openshift-image-registry.svc:5000/cert-monitor/ansible-cert-monitor:latest`.
* Permissão de cluster-admin para criar o Namespace, a ClusterRole e a ClusterRoleBinding.

## Como Configurar e Instalar no Cluster

### 1. Habilitar o User Workload Monitoring
O caminho suportado pela Red Hat para métricas e alertas de namespaces de usuário é o [User Workload Monitoring](https://docs.redhat.com/en/documentation/openshift_container_platform/latest/html/monitoring/configuring-user-workload-monitoring). Caso ainda não esteja habilitado:

```bash
oc -n openshift-monitoring patch configmap cluster-monitoring-config --type merge \
  -p '{"data":{"config.yaml":"enableUserWorkload: true\n"}}'
```

> Se o ConfigMap `cluster-monitoring-config` já tiver outras configurações, edite-o manualmente em vez de usar o patch acima.

### 2. Implantação da Infraestrutura via Kustomize
Execute o comando abaixo na raiz do diretório para aplicar todos os manifestos:

```bash
oc apply -k .
```

### 3. Compilação da Imagem
Faça o build nativo da imagem enviando os arquivos locais para o OpenShift:

```bash
oc start-build ansible-cert-monitor-build --from-dir=. --follow -n cert-monitor
```

Até o primeiro build terminar, execuções do CronJob falharão por falta da imagem. Alterações no playbook (`check-certs.yaml`) ou nas dependências exigem um novo build; alterações nos manifestos e no ConfigMap de exclusão exigem apenas `oc apply -k .`.

## Como Usar
O sistema opera de forma autônoma a cada 12 horas.

Para testar manualmente, crie um Job a partir do CronJob e acompanhe o log:

```bash
JOB=cert-monitor-manual-run-$(date +%s)
oc create job --from=cronjob/cert-monitor-cron "$JOB" -n cert-monitor
oc logs -f "job/$JOB" -n cert-monitor
```

### Exclusões
Para ignorar namespaces ou recursos, edite o ConfigMap `cert-monitor-exclude`, chaves `namespaces` e `nomes`. Cada item é uma regex Python com casamento exato (`^(?:item1|item2)$`), então nomes exatos também funcionam e lookahead é suportado. O ConfigMap é lido a cada execução, sem necessidade de rebuild. A exclusão é aplicada no playbook: os recursos continuam sendo lidos da API.

Por padrão:
* Namespaces: os `openshift-*` são ignorados (`openshift-(?!config$|ingress$).*`), **exceto** `openshift-config` (certificado custom da API) e `openshift-ingress` (certificado do router), que são renovados manualmente. Também são ignorados `openshift`, `kube.*`, `default` e `projeto-legado`; este último é apenas um exemplo, ajuste ao seu ambiente.
* Nomes: `kube-root-ca.crt` e `openshift-service-ca.crt`, que existem em todos os namespaces e são rotacionados pela plataforma, e os bundles `config-trusted-cabundle` / `trusted-ca-bundle`, injetados pelo OpenShift com todas as CAs públicas do sistema. CAs customizadas do cluster continuam monitoradas em `openshift-config/user-ca-bundle`.

Somente as chaves terminadas em `.crt`, `.pem`, `.cer` ou `.cert` são lidas (lista em `extensoes_certificado` no playbook).

Secrets dos tipos `service-account-token`, `dockercfg`, `dockerconfigjson` e `helm.sh/release.v1` não são lidos (lista em `secret_types_ignorados` no playbook).

### Certificados fora da RFC 5280
Alguns certificados violam limites da RFC 5280 e continuam sendo monitorados normalmente. Os casos verificados (no subject e no issuer) são:
* `commonName` fora de 1 a 64 bytes (UTF-8);
* `countryName` / `jurisdictionCountryName` com tamanho diferente de 2;
* número de série negativo ou zero. Algumas CAs raiz públicas foram emitidas com série 0 (ex.: Go Daddy e Starfield G2).

Esses certificados são listados no log do Job pela task "Listar certificados fora dos limites da RFC 5280", com namespace, nome, chave e posição. Os avisos crus que a biblioteca `cryptography` emitiria no stderr são silenciados pela variável `PYTHONWARNINGS` do CronJob. No Prometheus, a busca equivalente para CN longo é `cert_not_after_timestamp_seconds{subject_cn=~".{65,}"}`.

### Alerta continua depois de renovar o certificado?
1. **Aguarde a próxima varredura ou rode um Job manual.** A varredura acontece a cada 12 horas; até lá, as métricas (e o alerta) refletem o certificado antigo. Depois de renovar, rode o Job manual (seção acima): a série antiga sai do Pushgateway e o alerta resolve em poucos minutos.
2. **Procure outras cópias do certificado antigo.** É comum o mesmo certificado estar copiado em truststores, bundles ou Secrets de outras aplicações. Os labels do alerta (`cert_namespace`, `name`, `key`, `indice`) apontam a cópia exata, e a busca abaixo lista todas as cópias de um CN:
   ```promql
   cert_not_after_timestamp_seconds{subject_cn="<CN do certificado>"}
   ```
3. **Confira se o último Job terminou com sucesso.** Se a varredura falhar (por exemplo, por uma regex inválida no ConfigMap de exclusão ou com o Pushgateway fora do ar), nada é enviado e as métricas anteriores permanecem. Nesse caso o `CertMonitorStale` dispara depois de 26 horas.

## Considerações de Segurança
* A ClusterRole concede `get/list` em **todos os Secrets do cluster**. Qualquer um com acesso ao token da ServiceAccount `cert-monitor-sa` consegue ler essas credenciais. Restrinja o acesso ao namespace `cert-monitor`: quem pode criar pods, editar o CronJob ou rodar `oc start-build` nesse namespace consegue executar código com essa ServiceAccount, e portanto ler todos os Secrets.
* O playbook guarda apenas os blocos `BEGIN/END CERTIFICATE`; chaves privadas nunca são extraídas, e as tasks que manipulam conteúdo de Secrets usam `no_log: true`. A task de RFC 5280 não usa `no_log`, mas exibe apenas metadados e atributos que já constam nas métricas.
* Os containers rodam sem privilégios: `runAsNonRoot`, `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]` e `seccompProfile: RuntimeDefault`.
* O Pushgateway não tem autenticação; a NetworkPolicy limita o acesso ao pod do CronJob e aos namespaces de monitoramento. Quem conseguir criar pods com o label `app=cert-monitor` no namespace pode enviar métricas falsas ou vazias e, com isso, esconder alertas. É mais um motivo para restringir o namespace. O Pushgateway roda sem token de ServiceAccount (`automountServiceAccountToken: false`).
* O repositório é verificado contra vazamento de segredos: o `tests/run.sh` (e, portanto, o CI) roda o gitleaks sobre todo o histórico do git e sobre os arquivos atuais (etapa S01), e o `.gitignore` bloqueia arquivos típicos de credenciais (`*.key`, `*.pem`, `*.crt`, `*.p12`, kubeconfig, `.env`, `*.token`). Nunca commite credenciais: se acontecer, revogue-as, porque elas continuam no histórico publicado.
* A extração de PEM usa uma regex linear (só base64 entre `BEGIN` e `END`), para que um ConfigMap malicioso criado em qualquer namespace monitorado não trave a varredura.

## Considerações Operacionais
* O Pushgateway persiste as métricas em `emptyDir` (sobrevive a reinícios do container). Se o pod for recriado, as métricas somem até a próxima execução (até 12 horas) e o `CertMonitorAbsent` dispara após 1 hora. Para evitar isso, troque o `emptyDir` por um PersistentVolumeClaim.
* O playbook carrega todos os Secrets e ConfigMaps do cluster na memória do pod (limite de `1Gi`, prazo de `activeDeadlineSeconds: 1800`). Em clusters grandes, acompanhe o consumo e ajuste `resources.limits.memory` no CronJob. Os certificados são analisados com o filtro `community.crypto.x509_certificate_info`, que roda no próprio processo do Ansible, sem um processo por certificado.

## Testes
Depois de qualquer alteração, siga o [caderno de testes](tests/CADERNO-DE-TESTES.md). A parte automatizada roda com um único comando, o mesmo usado no CI:

```bash
tests/run.sh
```

Ele executa a varredura de segredos (gitleaks), yamllint, ansible-lint, kustomize e kubeconform; os testes do playbook (`tests/test_playbook.py`, que roda o playbook de verdade com recursos simulados e um Pushgateway local); os testes das regras de alerta (`tests/alertas.test.yaml`, via `promtool`), inclusive o cenário de renovação de certificado; e os testes das consultas do dashboard (sintaxe e resultado, via `promtool`). O caderno traz também o roteiro de validação no cluster, e o [detalhamento dos testes](tests/DETALHAMENTO-DOS-TESTES.md) explica cada caso: o risco coberto, como funciona e o que investigar quando falha.

## Como Remover
Para desinstalar toda a solução, incluindo a imagem compilada, execute:

```bash
oc delete -k .
```
