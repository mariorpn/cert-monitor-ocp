# Dashboard Grafana: cert-monitor

`cert-monitor-dashboard.json` é um dashboard para importar em qualquer Grafana 10 ou superior. Ele mostra a expiração dos certificados monitorados e destaca **em verde, por 24 horas, os certificados renovados**.

## O que o dashboard mostra

| Seção | Painéis |
|---|---|
| Resumo | Expirados · Vencem em ≤ 30 dias · Vencem em 31–60 dias · ✔ Renovados nas últimas 24h · Erros de análise · Última varredura |
| Certificados | Tabela com todos os certificados, dos que vencem primeiro aos mais distantes: Situação, dias restantes, data de expiração, namespace, tipo, nome, chave, posição no bundle e CN. Cada coluna tem filtro. |
| Renovações e vencimentos | Renovados nos últimos 7 dias (verde nas primeiras 24h) · Certificados por faixa de vencimento |
| Saúde do monitor | Certificados monitorados ao longo do tempo |

Filtros no topo: **Namespace** e **Tipo** (Secret/ConfigMap).

### Cores
Cada cor tem um único significado e vem sempre acompanhada de texto ou ícone:

| Cor | Significado |
|---|---|
| Verde | **✔ Renovado**: renovação detectada nas últimas 24h. O verde não é usado para mais nada, para que a renovação se destaque. |
| Vermelho | ✖ Expirado |
| Laranja | ▲ Vence em 30 dias ou menos |
| Amarelo | ▲ Vence em 31 a 60 dias |
| Sem cor | OK |

Um certificado renovado para uma validade curta aparece como "✔ Renovado" na coluna **Situação**, mas a coluna **Dias restantes** continua com a cor da faixa de vencimento do certificado novo.

### Como a renovação é detectada
A cada varredura, o playbook compara as datas de expiração com as da varredura anterior, guardadas no Pushgateway. Uma data nova numa chave que já era monitorada (mesmo namespace, tipo, nome e chave) gera a métrica `cert_renewed_timestamp_seconds`. Consequências:
- a renovação aparece **na varredura seguinte** (até 12 horas depois, ou na hora com um Job manual);
- um recurso novo não aparece como renovado, e trocar a ordem dos certificados de um bundle também não;
- incluir um certificado novo num bundle existente aparece como renovação daquela chave;
- se o pod do Pushgateway for recriado (armazenamento `emptyDir`), perdem-se as marcações de renovação, não os dados de expiração.

O mesmo evento dispara o alerta informativo `CertificateRenewed` por 24 horas.

## Instalação

### 1. Credencial para consultar o Prometheus do OpenShift
O dashboard consulta o **Thanos Querier** do OpenShift, que reúne as métricas da plataforma e do User Workload Monitoring. Crie uma ServiceAccount com permissão de leitura de métricas:

```bash
oc create serviceaccount grafana-cert-monitor -n cert-monitor
oc adm policy add-cluster-role-to-user cluster-monitoring-view -z grafana-cert-monitor -n cert-monitor

# Token com validade de 1 ano (o cluster pode impor um limite menor)
oc create token grafana-cert-monitor -n cert-monitor --duration=8760h
```

> `cluster-monitoring-view` dá leitura de **todas** as métricas do cluster. Guarde o token como segredo e renove-o antes de expirar.

### 2. Datasource no Grafana
Em *Connections → Data sources → Add data source → Prometheus*:

| Campo | Valor |
|---|---|
| Prometheus server URL | Grafana dentro do cluster: `https://thanos-querier.openshift-monitoring.svc:9091`. Grafana fora do cluster: `https://$(oc -n openshift-monitoring get route thanos-querier -o jsonpath='{.spec.host}')` |
| Authentication → HTTP headers | Header `Authorization`, valor `Bearer <token do passo 1>` |
| TLS | Informe a CA do cluster ou, apenas para teste, marque *Skip TLS certificate validation* |
| HTTP method | `GET` |

Clique em *Save & test*: a mensagem deve ser de sucesso.

### 3. Importar o dashboard
*Dashboards → New → Import → Upload dashboard JSON file*, selecione `cert-monitor-dashboard.json` e, em **Prometheus (Thanos Querier)**, escolha o datasource do passo 2.

## Validação
- Os painéis carregam sem erro e o filtro **Namespace** lista os namespaces monitorados (passo C12 do [caderno de testes](../tests/CADERNO-DE-TESTES.md)).
- As consultas do dashboard são testadas automaticamente pelo `tests/run.sh`: sintaxe (D01) e resultado sobre dados sintéticos, inclusive o verde por 24h (D02).

## Alterando o dashboard
Edite no Grafana, exporte com *Share → Export → Export for sharing externally* (isso mantém a variável de datasource) e substitua o `cert-monitor-dashboard.json`. Rode `tests/run.sh`: o D02 identifica os painéis pelo título e pelo `refId` das consultas. Se você renomear um painel ou consulta testada, atualize `tests/dashboard_promql.py`.
