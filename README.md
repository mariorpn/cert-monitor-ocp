# OpenShift Certificate Monitor (Ansible + Prometheus)

## O que é e Qual a Função?
Esta solução é um automatismo nativo em Ansible executado via CronJob dentro do OpenShift. Sua função é varrer periodicamente os `Secrets` e `ConfigMaps` do cluster, extrair e validar os certificados x509 (inclusive cada certificado de bundles e cadeias), e exportar a data de expiração de cada um como métrica do Prometheus (`cert_not_after_timestamp_seconds`).

A solução integra-se à stack de monitoramento do OpenShift, gerando alertas no Alertmanager para certificados prestes a expirar e para falhas da própria varredura.

## Estrutura de Arquivos
* `check-certs.yaml`: Playbook Ansible que realiza a varredura e envia as métricas ao Pushgateway.
* `Containerfile`: Instruções de build da imagem (Red Hat UBI 9) com permissões ajustadas para OpenShift.
* `requirements.txt` / `requirements.yml`: Versões fixas dos pacotes Python e das collections Ansible usadas na imagem.
* `kustomization.yaml`: Ponto de entrada para implantação via Kustomize.
* `manifestos-rbac-cronjob.yaml`: Namespace `cert-monitor`, ServiceAccount, ClusterRole (apenas get/list em secrets/configmaps), ClusterRoleBinding e o CronJob (a cada 12 horas).
* `configmap-exclude.yaml`: Regex dos namespaces e dos nomes de recursos ignorados na varredura.
* `pushgateway.yaml`: Deployment, Service, ServiceMonitor e NetworkPolicy do Pushgateway.
* `custom_alerts.yaml`: PrometheusRule com os alertas de expiração e de saúde do monitor.
* `build-config.yaml`: ImageStream e BuildConfig para compilação nativa no cluster.

## Métricas Exportadas
| Métrica | Descrição |
|---|---|
| `cert_not_after_timestamp_seconds` | Data de expiração (epoch UTC). Labels: `cert_namespace`, `tipo`, `name`, `key`, `indice` (posição no bundle), `subject_cn`, `not_after`. |
| `cert_monitor_certificates` | Quantidade de certificados encontrados na última varredura. |
| `cert_monitor_parse_errors` | Certificados que não puderam ser analisados. |
| `cert_monitor_last_run_timestamp_seconds` | Momento da última varredura concluída. |

Os dias restantes são calculados no próprio Prometheus (`(cert_not_after_timestamp_seconds - time()) / 86400`), portanto continuam diminuindo mesmo que o CronJob pare de rodar.

## Alertas
* `CertificateExpiringSoon` (warning): expira entre 30 e 60 dias.
* `CertificateExpiringCritical` (critical): expira em 30 dias ou menos (inclui já expirados).
* `CertMonitorStale` (warning): nenhuma varredura concluída nas últimas 26 horas.
* `CertMonitorAbsent` (warning): métricas do monitor ausentes.
* `CertMonitorParseErrors` (info): certificados que não puderam ser analisados.

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

Até o primeiro build terminar, execuções do CronJob falharão por falta da imagem.

## Como Usar
O sistema opera de forma autônoma a cada 12 horas.

Para testar manualmente, crie um Job a partir do CronJob:

```bash
oc create job --from=cronjob/cert-monitor-cron cert-monitor-manual-run -n cert-monitor
```

Para ignorar namespaces ou recursos: edite o ConfigMap `cert-monitor-exclude`, chaves `namespaces` e `nomes`, adicionando nomes exatos ou regex (ex: `openshift.*`). Por padrão, `kube-root-ca.crt` e `openshift-service-ca.crt` são ignorados, pois existem em todos os namespaces e são rotacionados pela plataforma.

Secrets dos tipos `service-account-token`, `dockercfg`, `dockerconfigjson` e `helm.sh/release.v1` não são lidos (lista em `secret_types_ignorados` no playbook).

## Considerações de Segurança
* A ClusterRole concede `get/list` em **todos os Secrets do cluster**. Qualquer um com acesso ao token da ServiceAccount `cert-monitor-sa` consegue ler essas credenciais. Restrinja o acesso ao namespace `cert-monitor`.
* O playbook guarda apenas os blocos `BEGIN/END CERTIFICATE`; chaves privadas nunca são extraídas, e as tasks que manipulam conteúdo de Secrets usam `no_log: true`.
* O Pushgateway não tem autenticação; a NetworkPolicy limita o acesso ao pod do CronJob e aos namespaces de monitoramento.
* O Pushgateway persiste as métricas em `emptyDir` (sobrevive a reinícios do container). Para sobreviver à recriação do pod, troque por um PersistentVolumeClaim.

## Como Remover
Para desinstalar toda a solução, incluindo a imagem compilada, execute:

```bash
oc delete -k .
```
