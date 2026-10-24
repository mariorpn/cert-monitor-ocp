# OpenShift Certificate Monitor (Ansible + Prometheus)

## O que é e Qual a Função?
Esta solução é um automatismo nativo em Ansible executado via CronJob dentro do OpenShift. Sua função é varrer periodicamente todos os `Secrets` e `ConfigMaps` do cluster, extrair e validar certificados x509, calcular os dias restantes para expiração e exportar esses dados como métricas no formato do Prometheus (`cert_expiry_days_remaining`). 

A solução integra-se nativamente com a stack de monitoramento do OpenShift, gerando alertas no Alertmanager para certificados prestes a expirar.

## Estrutura de Arquivos
* `check-certs.yaml`: Playbook Ansible que realiza a varredura e envia o payload.
* `Containerfile`: Instruções de build da imagem (Red Hat UBI 9) com permissões ajustadas para OpenShift.
* `kustomization.yaml`: Ponto de entrada para implantação via Kustomize.
* `namespace.yaml`: Definição do projeto `cert-monitor`.
* `rbac.yaml`: ServiceAccount, ClusterRole (apenas get/list em secrets/configmaps) e ClusterRoleBinding.
* `configmap-exclude.yaml`: Lista em formato regex dos namespaces que serão ignorados na varredura.
* `pushgateway.yaml`: Deployment e Service do Pushgateway, além do ServiceMonitor.
* `cronjob.yaml`: Agendamento da execução do playbook a cada 12 horas.
* `prometheus-rule.yaml`: Alertas customizados de Warning (<= 60 dias) e Critical (<= 30 dias).
* `build-config.yaml`: ImageStream e BuildConfig para compilação nativa no cluster.

## Como Configurar e Instalar no Cluster

**1. Implantação da Infraestrutura via Kustomize:**
Execute o comando abaixo na raiz do diretório para aplicar todos os manifestos:
```bash
oc apply -k .
```

2. Compilação da Imagem:
Faça o build nativo da imagem enviando o Containerfile e o check-certs.yaml locais para o OpenShift:

```bash
oc start-build ansible-cert-monitor-build --from-dir=. --follow -n cert-monitor
```

3. Promoção para "Platform Monitoring" (Opcional, porém recomendado):
Para que os alertas apareçam na categoria Platform do OpenShift (geridos pelo Prometheus principal), execute:

```bash
oc label namespace cert-monitor openshift.io/cluster-monitoring="true"
oc adm policy add-role-to-user view system:serviceaccount:openshift-monitoring:prometheus-k8s -n cert-monitor
```

Como Usar
O sistema opera de forma autônoma a cada 12 horas.

Para testar manualmente, crie um Job a partir do CronJob:

```bash
oc create job --from=cronjob/cert-monitor-cron cert-monitor-manual-run -n cert-monitor
```

Para ignorar novos namespaces: Edite o ConfigMap cert-monitor-exclude adicionando nomes exatos ou regex (ex: openshift.*).

Como Remover
Para desinstalar toda a solução, incluindo a imagem compilada, execute:

```bash
oc delete -k .
```

