#!/usr/bin/env python3
"""Gera os testes do dashboard Grafana para o promtool (ver tests/CADERNO-DE-TESTES.md, D01 e D02).

Lê grafana/cert-monitor-dashboard.json, troca as variáveis do Grafana por '.*' e escreve em <dir>:
- regras-dashboard.yaml: toda consulta do dashboard como regra de gravação (D01: sintaxe, `promtool check rules`);
- dashboard.test.yaml: avaliação das consultas principais sobre séries sintéticas (D02, `promtool test rules`).

Uso: python3 tests/dashboard_promql.py grafana/cert-monitor-dashboard.json <dir>
"""
import json
import sys
from pathlib import Path

import yaml

DIA = 86400


def consultas(dashboard):
    """{(título do painel, refId): expr} com as variáveis do Grafana substituídas."""
    resultado = {}
    for painel in dashboard['panels']:
        for alvo in painel.get('targets', []):
            expr = alvo['expr'].replace('$namespace', '.*').replace('$tipo', '.*')
            resultado[(painel['title'], alvo['refId'])] = expr
    return resultado


def rotulos(nome):
    return (f'cert_namespace="app",tipo="SECRET",name="{nome}",key="tls.crt",indice="0",'
            f'subject_cn="{nome}",not_after="x"')


def main(caminho_dashboard, destino):
    dashboard = json.loads(Path(caminho_dashboard).read_text())
    exprs = consultas(dashboard)
    destino = Path(destino)

    regras = {'groups': [{'name': 'dashboard', 'rules': [
        {'record': f'dashboard:consulta_{i}', 'expr': e} for i, e in enumerate(exprs.values())]}]}
    (destino / 'regras-dashboard.yaml').write_text(yaml.safe_dump(regras, allow_unicode=True, sort_keys=False))

    # time() começa em 0; avaliação em 1h. Valores = segundos até expirar.
    # renovado_curto: renovado para uma validade que ainda cai na faixa de 30 dias; o verde deve prevalecer
    certs = {'renovado': 400 * DIA, 'renovado_curto': 20 * DIA, 'expirado': -DIA, 'c20': 20 * DIA,
             'c45': 45 * DIA, 'c200': 200 * DIA}
    series = [{'series': f'cert_not_after_timestamp_seconds{{{rotulos(n)}}}', 'values': f'{v}x90'}
              for n, v in certs.items()]
    series += [{'series': f'cert_renewed_timestamp_seconds{{{rotulos(n)}}}', 'values': '0x90'}
               for n in ('renovado', 'renovado_curto')]
    series.append({'series': 'cert_monitor_last_run_timestamp_seconds', 'values': '0x90'})

    def amostra(valor, nome=None):
        return {'labels': f'{{{rotulos(nome)}}}' if nome else '{}', 'value': valor}

    esperado = [
        # Coluna Situação: 4 renovado (tem prioridade), 3 expirado, 2 <= 30 dias, 1 31-60 dias, 0 OK
        (('Certificados', 'A'), [amostra(4, 'renovado'), amostra(4, 'renovado_curto'), amostra(3, 'expirado'), amostra(2, 'c20'),
                                 amostra(1, 'c45'), amostra(0, 'c200')]),
        (('Expirados', 'A'), [amostra(1)]),
        (('Vencem em ≤ 30 dias', 'A'), [amostra(2)]),
        (('Vencem em 31–60 dias', 'A'), [amostra(1)]),
        (('✔ Renovados nas últimas 24h', 'A'), [amostra(2)]),
        (('Última varredura', 'A'), [amostra(3600)]),
        (('Renovados nos últimos 7 dias', 'D'), [amostra(1, 'renovado'), amostra(1, 'renovado_curto')]),  # horas desde a renovação
        (('Renovados nos últimos 7 dias', 'F'), [amostra(400 * DIA * 1000, 'renovado'),
                                                          amostra(20 * DIA * 1000, 'renovado_curto')]),
        (('Certificados por faixa de vencimento', 'G'), [amostra(1)]),  # expirados
        (('Certificados por faixa de vencimento', 'H'), [amostra(2)]),  # <= 30
        (('Certificados por faixa de vencimento', 'I'), [amostra(1)]),  # 31-60
        (('Certificados por faixa de vencimento', 'J'), [amostra(0)]),  # 61-90 (vector(0) quando vazio)
        (('Certificados por faixa de vencimento', 'K'), [amostra(2)]),  # > 90
    ]
    faltando = [chave for chave, _ in esperado if chave not in exprs]
    if faltando:
        sys.exit(f'Consultas não encontradas no dashboard (título, refId): {faltando}')

    testes = {
        'rule_files': ['regras-dashboard.yaml'],
        'evaluation_interval': '1m',
        'tests': [
            {'name': 'D02 consultas do dashboard em 1h', 'interval': '1m', 'input_series': series,
             'promql_expr_test': [{'expr': exprs[chave], 'eval_time': '1h', 'exp_samples': amostras}
                                  for chave, amostras in esperado]},
            {'name': 'D02 renovação sai do destaque depois de 24h', 'interval': '1m', 'input_series': [
                {'series': f'cert_not_after_timestamp_seconds{{{rotulos("renovado")}}}', 'values': f'{400 * DIA}x1600'},
                {'series': f'cert_renewed_timestamp_seconds{{{rotulos("renovado")}}}', 'values': '0x1600'}],
             'promql_expr_test': [
                 {'expr': exprs[('Certificados', 'A')], 'eval_time': '25h', 'exp_samples': [amostra(0, 'renovado')]},
                 {'expr': exprs[('✔ Renovados nas últimas 24h', 'A')], 'eval_time': '25h',
                  'exp_samples': [amostra(0)]}]},
        ],
    }
    (destino / 'dashboard.test.yaml').write_text(yaml.safe_dump(testes, allow_unicode=True, sort_keys=False))


if __name__ == '__main__':
    main(*sys.argv[1:3])
