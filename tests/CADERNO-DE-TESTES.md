# Caderno de Testes: cert-monitor

Execute este caderno **depois de qualquer alteração** (playbook, manifestos, alertas, dependências ou imagem).
A parte automatizada roda local e no CI. A parte de cluster valida o que só existe no OpenShift (RBAC, NetworkPolicy, coleta, Alertmanager).

O que cada teste cobre, como funciona e o que investigar quando falha está em [DETALHAMENTO-DOS-TESTES.md](DETALHAMENTO-DOS-TESTES.md).

## 1. Parte automatizada

```bash
tests/run.sh
```

Requisitos locais: `python3` com `ansible-core`, `cryptography` e PyYAML (`pip install -r requirements.txt yamllint ansible-lint`), as collections (`ansible-galaxy collection install -r requirements.yml`), `kubectl`. Opcionais: `kubeconform` (sem ele a etapa L04 é pulada) e `promtool`, ou então `podman`/`docker`, que usam a imagem `prom/prometheus` (sem nenhum deles, as etapas A01–A08 e D01–D02 são puladas), e `gitleaks` ou `podman`/`docker` para a etapa S01.

Resultado esperado: `Todas as etapas executadas passaram.` e, no CI, nenhuma etapa pulada.

### Segredos, lint e manifestos
| ID | Verifica | Esperado |
|---|---|---|
| S01 | Varredura de segredos com gitleaks: **todo o histórico do git** e os arquivos atuais, inclusive os não commitados | `no leaks found` nas duas varreduras |
| L01 | `yamllint .` | Sem erros |
| L02 | `ansible-lint check-certs.yaml` | Perfil `production` sem falhas |
| L03 | `kubectl kustomize .` | Manifestos gerados sem erro |
| L04 | `kubeconform -strict` (com schemas de CRDs) | Todos os recursos válidos |

### Playbook (`tests/test_playbook.py`)
O playbook roda de verdade, sem cluster: os recursos são injetados e um servidor HTTP local faz o papel do Pushgateway.

| ID | Verifica | Esperado |
|---|---|---|
| P01 | Envio ao Pushgateway | Um único `PUT` em `/metrics/job/cert_monitor` |
| P02 | Formato do payload | Formato de exposição válido, HELP/TYPE presentes, **sem séries duplicadas** (o Pushgateway rejeitaria o envio inteiro) |
| P03 | Origens | Secret (base64), ConfigMap `data` e `binaryData` lidos, com `tipo` correto |
| P04 | Bundles | Uma série por certificado, `indice` 0..n na ordem do arquivo |
| P05 | Extensões | `.crt`, `.pem`, `.cer` e `.cert` lidas; `.txt` e `.key` ignoradas |
| P06 | Segredos | Chave privada nunca aparece no payload nem no log |
| P07 | Exclusões padrão (`configmap-exclude.yaml` real) | `openshift-*` (exceto `openshift-config`/`openshift-ingress`), `openshift`, `kube.*`, `default`, `kube-root-ca.crt`, `openshift-service-ca.crt`, `trusted-ca-bundle` e `config-trusted-cabundle` ignorados; nomes parecidos monitorados |
| P08 | PEM inválido | `cert_monitor_parse_errors 1`; o playbook não falha |
| P09 | Contagem | `cert_monitor_certificates` = entradas (séries + PEMs inválidos) |
| P10 | Mesmo certificado em vários recursos | Uma série por recurso, mesmo valor |
| P11 | Data de expiração | Valor = epoch UTC do `notAfter`; label `not_after` no formato `AAAAMMDDhhmmssZ` |
| P12 | Escape do CN | Aspas, barra invertida e quebra de linha não quebram o payload |
| P13 | PEM com CRLF | Certificado lido normalmente |
| P14 | Última execução | `cert_monitor_last_run_timestamp_seconds` próximo do horário atual |
| P15 | Relatório RFC 5280 | CN de 78 bytes, série 0 e série -1 listados com namespace/nome/chave/posição, e continuam exportados |
| P16 | Avisos crus | Com o `PYTHONWARNINGS` do CronJob, nenhum `UserWarning`/`CryptographyDeprecationWarning` no log |
| P17 | Exclusão vazia / ConfigMap ausente | Nada é ignorado, sem erro |
| P18 | Regex de exclusão inválida | Playbook falha (Job com erro, visível) e **nada é enviado** (o estado anterior não é apagado) |
| P19 | **Renovação** | Na varredura seguinte, a série antiga (`not_after` antigo) sai e só a nova é enviada, via `PUT` |
| P20 | Recurso removido | Some das métricas na varredura seguinte |
| P21 | Pushgateway fora do ar | Playbook falha (Job com erro), não é silencioso |
| P22 | DoS por regex | Busca de PEM em 1 MiB malicioso em menos de 2 s |
| P23 | ConfigMap malicioso | A varredura conclui e os demais certificados são exportados |
| P24 | Primeira execução (Pushgateway vazio) | Nenhuma `cert_renewed_timestamp_seconds` |
| P25 | **Certificado renovado** | `cert_renewed_timestamp_seconds` com horário ≈ agora e os mesmos labels do certificado novo; certificado inalterado e recurso novo não são marcados |
| P26 | Renovação para validade menor | Também é detectada |
| P27 | Bundle reordenado | Não é renovação |
| P28 | Marcação nas varreduras seguintes | Mantida com o horário original por até 7 dias e descartada depois disso |
| P29 | Estado anterior indisponível (HTTP 500) | Varredura e envio normais, sem marcar renovações |

### Regras de alerta (`tests/alertas.test.yaml`, via `promtool test rules`)
| ID | Verifica | Esperado |
|---|---|---|
| A01 | 20 dias e já expirado | `CertificateExpiringCritical`, com o texto renderizado |
| A02 | 45 dias / 90 dias | `CertificateExpiringSoon` só para 45; nada para 90 |
| A03 | **Renovação** | O alerta crítico da série antiga resolve quando ela some e a nova (400 dias) aparece |
| A04 | Última varredura há mais de 26h | `CertMonitorStale` (nada às 25h) |
| A05 | Monitor presente | Sem `CertMonitorAbsent` |
| A06 | Métricas ausentes | `CertMonitorAbsent` só depois de 1h |
| A07 | Erros de análise | `CertMonitorParseErrors` depois de 1m |
| A08 | **Aviso de renovação** | `CertificateRenewed` ativo 1h depois da renovação e resolvido depois de 24h |

### Dashboard (`grafana/cert-monitor-dashboard.json`, via `tests/dashboard_promql.py` + `promtool`)
| ID | Verifica | Esperado |
|---|---|---|
| D01 | Sintaxe de todas as consultas do dashboard | `promtool check rules` sem erros |
| D02 | Resultado das consultas principais sobre séries sintéticas | Situação (renovado > expirado > ≤ 30 > 31–60 > OK, inclusive renovado para validade curta), contadores do resumo, faixas e renovados corretos; o verde some depois de 24h |

## 2. Parte no cluster (manual)

Faça logo após o deploy. Use `NS=cert-monitor`.

| ID | Passo | Esperado |
|---|---|---|
| C01 | `oc apply -k .` e `oc start-build ansible-cert-monitor-build --from-dir=. --follow -n $NS` | Build `Complete` |
| C02 | `oc get pods -n $NS -l app=pushgateway` | Pushgateway `Running`, `READY 1/1` |
| C03 | `JOB=teste-$(date +%s); oc create job --from=cronjob/cert-monitor-cron $JOB -n $NS; oc wait --for=condition=complete job/$JOB -n $NS --timeout=10m` | Job concluído |
| C04 | `oc logs job/$JOB -n $NS \| grep -E 'failed=\|errantly\|Warning:'` | `failed=0`; nenhum `errantly sent data directly to stderr` |
| C05 | Observe → Metrics: `cert_monitor_last_run_timestamp_seconds`, `cert_monitor_certificates`, `cert_monitor_parse_errors` | Última execução recente; contagens coerentes com execuções anteriores |
| C06 | Observe → Metrics: `count by (cert_namespace,tipo,name,key,indice) (cert_not_after_timestamp_seconds) > 1` | Vazio (nenhuma posição com dois certificados ao mesmo tempo) |
| C07 | Observe → Alerting → Alerting rules, filtro `cert-monitor` | As 5 regras carregadas, sem erro |
| C08 | **Renovação ponta a ponta.** Veja o roteiro abaixo | O alerta dispara e resolve depois da renovação |
| C09 | NetworkPolicy: `oc run np-teste -n default --rm -i --restart=Never --image=registry.access.redhat.com/ubi9/ubi-minimal -- curl -s -m 5 http://pushgateway.cert-monitor.svc:9091/-/ready; echo rc=$?` | Falha por timeout (rc ≠ 0): pods de fora não alcançam o Pushgateway |
| C10 | Token do Pushgateway: `oc exec -n $NS deploy/pushgateway -- ls /var/run/secrets/kubernetes.io/serviceaccount` | `No such file or directory` |
| C11 | Secrets ignorados por tipo: `oc logs job/$JOB -n $NS` não deve mencionar secrets `kubernetes.io/service-account-token`, `dockercfg`, `dockerconfigjson` ou `helm.sh/release.v1`; e `cert_not_after_timestamp_seconds{name=~"builder-dockercfg.*\|.*-token-.*"}` deve estar vazio | Nenhuma série desses tipos |
| C12 | Dashboard: importe `grafana/cert-monitor-dashboard.json` seguindo `grafana/README.md` | Todos os painéis carregam sem erro; o filtro Namespace lista os namespaces monitorados; os números do resumo batem com C05 |

### Roteiro C08: renovação ponta a ponta
```bash
NS=cert-monitor; T=cert-monitor-teste
oc create namespace $T
# 1) certificado que vence em 10 dias
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj "/CN=teste-renovacao" \
  -days 10 -keyout /tmp/t.key -out /tmp/t.crt
oc create secret tls teste-renovacao -n $T --cert=/tmp/t.crt --key=/tmp/t.key
JOB=teste-$(date +%s); oc create job --from=cronjob/cert-monitor-cron $JOB -n $NS
oc wait --for=condition=complete job/$JOB -n $NS --timeout=10m
```
Esperado: em até ~2 min, `ALERTS{alertname="CertificateExpiringCritical",name="teste-renovacao"}` fica `firing` (Observe → Alerting).

```bash
# 2) renovação: certificado de 400 dias no mesmo Secret
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj "/CN=teste-renovacao" \
  -days 400 -keyout /tmp/t.key -out /tmp/t.crt
oc create secret tls teste-renovacao -n $T --cert=/tmp/t.crt --key=/tmp/t.key --dry-run=client -o yaml | oc apply -f -
JOB=teste-$(date +%s); oc create job --from=cronjob/cert-monitor-cron $JOB -n $NS
oc wait --for=condition=complete job/$JOB -n $NS --timeout=10m
```
Esperado: `cert_not_after_timestamp_seconds{name="teste-renovacao"}` mostra **uma única** série, com o `not_after` novo, e o alerta crítico resolve em poucos minutos. `cert_renewed_timestamp_seconds{name="teste-renovacao"}` existe, o alerta `CertificateRenewed` fica `firing` e, no dashboard, a linha aparece como **✔ Renovado** (verde) na tabela Certificados e em Renovados nos últimos 7 dias.

```bash
# 3) limpeza (as séries somem na próxima varredura)
oc delete namespace $T; rm -f /tmp/t.key /tmp/t.crt
JOB=teste-$(date +%s); oc create job --from=cronjob/cert-monitor-cron $JOB -n $NS
```

## 3. Registro de execuções
| Data | Commit / alteração | Automatizado | Cluster (C01–C12) | Responsável | Observações |
|---|---|---|---|---|---|
| | | | | | |
