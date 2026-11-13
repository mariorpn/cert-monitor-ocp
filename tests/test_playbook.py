#!/usr/bin/env python3
"""Testes automatizados do playbook check-certs.yaml (ver tests/CADERNO-DE-TESTES.md).

O playbook roda de verdade (ansible-playbook), mas sem cluster: as tasks k8s_info são removidas e os
Secrets/ConfigMaps são injetados via extra-vars. Um servidor HTTP local faz o papel do Pushgateway.
Requer ansible-core, as collections de requirements.yml, cryptography e PyYAML (requirements.txt).
"""
import base64
import datetime
import http.server
import json
import os
import re
import subprocess
import tempfile
import textwrap
import threading
import time
import unittest
import warnings
from pathlib import Path

import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

RAIZ = Path(__file__).resolve().parent.parent
PLAYBOOK = RAIZ / 'check-certs.yaml'
EXCLUDE_CM = RAIZ / 'configmap-exclude.yaml'
CRONJOB = RAIZ / 'manifestos-rbac-cronjob.yaml'
AGORA = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
PEM_INVALIDO = '-----BEGIN CERTIFICATE-----\nlixo\n-----END CERTIFICATE-----\n'
# Chave fictícia (P06). Montada em partes para não ser confundida com uma chave real por scanners de segredos.
CHAVE_PRIVADA = '-----BEGIN ' + 'PRIVATE KEY-----\nSEGREDO-NAO-PODE-VAZAR\n-----END ' + 'PRIVATE KEY-----\n'


# ---------------------------------------------------------------------------------------------
# Geração de dados
# ---------------------------------------------------------------------------------------------
class Cert:
    """Certificado autoassinado de teste: pem, epoch de expiração e label not_after esperados."""

    def __init__(self, cn, dias=30, serial=1, crlf=False):
        chave = ec.generate_private_key(ec.SECP256R1())
        with warnings.catch_warnings():  # CN > 64 bytes é proposital em alguns testes
            warnings.simplefilter('ignore')
            nome = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn, _validate=False)])
        cert = (x509.CertificateBuilder().subject_name(nome).issuer_name(nome)
                .public_key(chave.public_key()).serial_number(1)
                .not_valid_before(AGORA - datetime.timedelta(days=1))
                .not_valid_after(AGORA + datetime.timedelta(days=dias))
                .sign(chave, hashes.SHA256()))
        der = cert.public_bytes(serialization.Encoding.DER)
        if serial != 1:
            # O cryptography não emite série <= 0; edita o DER (versão v3 seguida de serial 1).
            # A assinatura fica inválida, mas o playbook só lê os campos.
            versao_e_serial = b'\xa0\x03\x02\x01\x02\x02\x01\x01'
            assert der.count(versao_e_serial) == 1
            der = der.replace(versao_e_serial, versao_e_serial[:-1] + (serial & 0xFF).to_bytes(1, 'big'))
        quebra = '\r\n' if crlf else '\n'
        corpo = quebra.join(textwrap.wrap(base64.b64encode(der).decode(), 64))
        self.pem = f'-----BEGIN CERTIFICATE-----{quebra}{corpo}{quebra}-----END CERTIFICATE-----{quebra}'
        self.cn = cn
        self.epoch = int(cert.not_valid_after_utc.timestamp())
        self.not_after = cert.not_valid_after_utc.strftime('%Y%m%d%H%M%SZ')


def b64(texto):
    return base64.b64encode(texto.encode()).decode()


def secret(ns, nome, dados):
    return {'metadata': {'namespace': ns, 'name': nome}, 'data': {k: b64(v) for k, v in dados.items()}}


def configmap(ns, nome, dados=None, binarios=None):
    cm = {'metadata': {'namespace': ns, 'name': nome}}
    if dados:
        cm['data'] = dados
    if binarios:
        cm['binaryData'] = {k: b64(v) for k, v in binarios.items()}
    return cm


# ---------------------------------------------------------------------------------------------
# Execução do playbook
# ---------------------------------------------------------------------------------------------
class Pushgateway:
    """Servidor HTTP local que registra (método, caminho, corpo) de cada requisição.
    estado: resposta de GET /api/v1/metrics (dict, como a API do Pushgateway) ou um código HTTP de erro."""

    def __init__(self, estado=None):
        requisicoes = self.requisicoes = []
        estado = {'status': 'success', 'data': []} if estado is None else estado

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_PUT(self):
                corpo = self.rfile.read(int(self.headers['Content-Length'])).decode()
                requisicoes.append((self.command, self.path, corpo))
                self.send_response(200)
                self.end_headers()

            do_POST = do_PUT

            def do_GET(self):
                requisicoes.append((self.command, self.path, ''))
                if isinstance(estado, int):
                    self.send_response(estado)
                    self.end_headers()
                    return
                corpo = json.dumps(estado).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(corpo)))
                self.end_headers()
                self.wfile.write(corpo)

            def log_message(self, *args):
                pass

        self.servidor = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.servidor.server_port}'

    def __enter__(self):
        threading.Thread(target=self.servidor.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *args):
        self.servidor.shutdown()
        self.servidor.server_close()


def python_warnings_do_cronjob():
    for doc in yaml.safe_load_all(CRONJOB.read_text()):
        if doc and doc.get('kind') == 'CronJob':
            env = doc['spec']['jobTemplate']['spec']['template']['spec']['containers'][0]['env']
            return next(e['value'] for e in env if e['name'] == 'PYTHONWARNINGS')
    raise AssertionError('PYTHONWARNINGS não encontrado no CronJob')


class Resultado:
    def __init__(self, rc, saida, requisicoes, duracao):
        self.rc, self.saida, self.requisicoes, self.duracao = rc, saida, requisicoes, duracao

    @property
    def envios(self):
        """Requisições que alteram o Pushgateway (a leitura do estado anterior é um GET e fica de fora)."""
        return [r for r in self.requisicoes if r[0] != 'GET']

    @property
    def payload(self):
        assert len(self.envios) == 1, f'esperado 1 envio ao Pushgateway, houve {len(self.envios)}'
        return self.envios[0][2]


def executar(secrets=(), configmaps=(), exclude='padrao', pushgateway_url=None, estado=None):
    """Roda o playbook com os recursos informados. exclude: 'padrao' (configmap-exclude.yaml do repo),
    None (ConfigMap de exclusão ausente) ou um dict com o campo data. estado: o que o Pushgateway falso
    devolve como varredura anterior (ver estado_api)."""
    play = yaml.safe_load(PLAYBOOK.read_text())
    play[0]['tasks'] = [t for t in play[0]['tasks'] if 'kubernetes.core.k8s_info' not in t]
    if exclude == 'padrao':
        exclude_cm = {'resources': [yaml.safe_load(EXCLUDE_CM.read_text())]}
    elif exclude is None:
        exclude_cm = {'resources': []}
    else:
        exclude_cm = {'resources': [{'data': exclude}]}
    variaveis = {'exclude_cm': exclude_cm,
                 'secrets_brutos': {'resources': list(secrets)},
                 'configmaps_brutos': {'resources': list(configmaps)}}
    with tempfile.TemporaryDirectory() as tmp, Pushgateway(estado) as pg:
        Path(tmp, 'play.yaml').write_text(yaml.safe_dump(play, allow_unicode=True, sort_keys=False))
        Path(tmp, 'vars.json').write_text(json.dumps(variaveis))
        env = dict(os.environ, PUSHGATEWAY_URL=pushgateway_url or pg.url,
                   PYTHONWARNINGS=python_warnings_do_cronjob(), ANSIBLE_NOCOLOR='1',
                   ANSIBLE_LOCALHOST_WARNING='False', ANSIBLE_INVENTORY_UNPARSED_WARNING='False',
                   ANSIBLE_DEPRECATION_WARNINGS='False')
        inicio = time.monotonic()
        proc = subprocess.run(['ansible-playbook', 'play.yaml', '-e', '@vars.json'], cwd=tmp, env=env,
                              capture_output=True, text=True, timeout=600)
        return Resultado(proc.returncode, proc.stdout + proc.stderr, list(pg.requisicoes),
                         time.monotonic() - inicio)


# ---------------------------------------------------------------------------------------------
# Leitura do payload (formato de exposição do Prometheus)
# ---------------------------------------------------------------------------------------------
LINHA = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})? (-?[0-9.eE+]+)$')
ROTULO = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\\n]|\\.)*)"(?:,|$)')


def interpretar(payload):
    """Valida o formato e devolve [(nome, rótulos, valor)]. Falha em série duplicada (o Pushgateway
    rejeitaria o envio inteiro) e em métrica sem HELP/TYPE."""
    series, vistas, declaradas = [], set(), set()
    for linha in payload.splitlines():
        if linha.startswith('# HELP ') or linha.startswith('# TYPE '):
            declaradas.add((linha[2:6], linha.split()[2]))
            continue
        if not linha.strip():
            continue
        m = LINHA.match(linha)
        if not m:
            raise AssertionError(f'linha fora do formato de exposição: {linha!r}')
        nome, corpo, valor = m.groups()
        rotulos, pos = {}, 0
        while corpo and pos < len(corpo):
            r = ROTULO.match(corpo, pos)
            if not r:
                raise AssertionError(f'rótulos inválidos: {linha!r}')
            rotulos[r[1]] = re.sub(r'\\(.)', lambda x: '\n' if x[1] == 'n' else x[1], r[2])
            pos = r.end()
        chave = (nome, tuple(sorted(rotulos.items())))
        if chave in vistas:
            raise AssertionError(f'série duplicada: {linha!r}')
        vistas.add(chave)
        if ('HELP', nome) not in declaradas or ('TYPE', nome) not in declaradas:
            raise AssertionError(f'métrica sem HELP/TYPE: {nome}')
        series.append((nome, rotulos, float(valor)))
    return series


def certificados(series, metrica='cert_not_after_timestamp_seconds', **filtro):
    return [r | {'_valor': v} for n, r, v in series
            if n == metrica and all(r.get(k) == f for k, f in filtro.items())]


def renovados(series, **filtro):
    return certificados(series, metrica='cert_renewed_timestamp_seconds', **filtro)


def escalar(series, nome):
    valores = [v for n, _, v in series if n == nome]
    assert len(valores) == 1, f'{nome}: {valores}'
    return valores[0]


def estado_api(payload):
    """Converte um payload enviado para a resposta de GET /api/v1/metrics do Pushgateway, que é o que a
    próxima varredura lê como estado anterior. Os valores vêm como string em notação científica, como no
    Pushgateway real, e os labels job/instance são acrescentados."""
    grupo = {'labels': {'job': 'cert_monitor'}}
    for nome, rotulos, valor in interpretar(payload):
        familia = grupo.setdefault(nome, {'type': 'GAUGE', 'metrics': []})
        familia['metrics'].append({'labels': rotulos | {'job': 'cert_monitor', 'instance': ''},
                                   'value': f'{valor:.15e}'})
    return {'status': 'success', 'data': [grupo]}


# ---------------------------------------------------------------------------------------------
# Casos de teste (IDs no CADERNO-DE-TESTES.md)
# ---------------------------------------------------------------------------------------------
class CenarioPrincipal(unittest.TestCase):
    """Uma execução com recursos variados; cada teste verifica um aspecto do resultado."""

    @classmethod
    def setUpClass(cls):
        c = cls.c = {
            'normal': Cert('normal', dias=30),
            'a': Cert('bundle-a', dias=100), 'b': Cert('bundle-b', dias=200), 'c': Cert('bundle-c', dias=300),
            'ext': Cert('extensao'), 'fora': Cert('fora-da-varredura'), 'incl': Cert('incluido'),
            'escape': Cert('a"b\\c\nd'), 'crlf': Cert('crlf', crlf=True),
            'cn_longo': Cert('x' * 78), 'serial0': Cert('serial-zero', serial=0),
            'serial_neg': Cert('serial-negativo', serial=-1),
        }
        cls.secrets = [
            secret('app', 'tls-normal', {'tls.crt': c['normal'].pem, 'tls.key': CHAVE_PRIVADA}),
            secret('app', 'bundle', {'ca.crt': c['a'].pem + c['b'].pem + c['c'].pem}),
            secret('app', 'extensoes', {'a.pem': c['ext'].pem, 'b.cer': c['ext'].pem, 'c.cert': c['ext'].pem,
                                        'd.txt': c['ext'].pem, 'e.key': c['ext'].pem}),
            secret('app', 'invalido', {'ca.crt': PEM_INVALIDO}),
            secret('app', 'escape', {'tls.crt': c['escape'].pem}),
            secret('app', 'crlf', {'tls.crt': c['crlf'].pem}),
            secret('app', 'rfc', {'tls.crt': c['cn_longo'].pem, 'ca.crt': c['serial0'].pem,
                                  's.crt': c['serial_neg'].pem}),
            secret('openshift-etcd', 'etcd', {'tls.crt': c['fora'].pem}),
            secret('openshift', 'x', {'tls.crt': c['fora'].pem}),
            secret('kube-system', 'x', {'tls.crt': c['fora'].pem}),
            secret('default', 'x', {'tls.crt': c['fora'].pem}),
            secret('openshift-ingress', 'router-certs', {'tls.crt': c['incl'].pem}),
        ]
        cls.configmaps = [
            configmap('app', 'cm-data', dados={'ca.crt': c['normal'].pem}),
            configmap('app', 'cm-bin', binarios={'x.pem': c['b'].pem}),
            configmap('openshift-config', 'user-ca-bundle', dados={'ca-bundle.crt': c['incl'].pem}),
            configmap('app', 'kube-root-ca.crt', dados={'ca.crt': c['fora'].pem}),
            configmap('app', 'openshift-service-ca.crt', dados={'service-ca.crt': c['fora'].pem}),
            configmap('app', 'trusted-ca-bundle', dados={'ca-bundle.crt': c['fora'].pem}),
            configmap('app', 'config-trusted-cabundle', dados={'ca-bundle.crt': c['fora'].pem}),
            configmap('app', 'meu-trusted-ca-bundle', dados={'ca-bundle.crt': c['incl'].pem}),
        ]
        cls.r = executar(cls.secrets, cls.configmaps)
        if cls.r.rc != 0:
            raise AssertionError(f'playbook falhou (rc={cls.r.rc}):\n{cls.r.saida[-4000:]}')
        cls.series = interpretar(cls.r.payload)

    def test_P01_envio_put_no_grupo_do_job(self):
        metodo, caminho, _ = self.r.envios[0]
        self.assertEqual((metodo, caminho), ('PUT', '/metrics/job/cert_monitor'))

    def test_P02_formato_do_payload(self):
        self.assertTrue(certificados(self.series))  # interpretar() já validou formato e duplicatas

    def test_P03_origens_secret_configmap_binarydata(self):
        self.assertTrue(certificados(self.series, name='tls-normal', tipo='SECRET', key='tls.crt'))
        self.assertTrue(certificados(self.series, name='cm-data', tipo='CONFIGMAP', key='ca.crt'))
        self.assertTrue(certificados(self.series, name='cm-bin', tipo='CONFIGMAP', key='x.pem'))

    def test_P04_bundle_gera_uma_serie_por_posicao(self):
        bundle = {s['indice']: s['subject_cn'] for s in certificados(self.series, name='bundle')}
        self.assertEqual(bundle, {'0': 'bundle-a', '1': 'bundle-b', '2': 'bundle-c'})

    def test_P05_extensoes_aceitas(self):
        chaves = {s['key'] for s in certificados(self.series, name='extensoes')}
        self.assertEqual(chaves, {'a.pem', 'b.cer', 'c.cert'})

    def test_P06_chave_privada_nunca_exposta(self):
        self.assertNotIn('SEGREDO-NAO-PODE-VAZAR', self.r.payload)
        self.assertNotIn('SEGREDO-NAO-PODE-VAZAR', self.r.saida)

    def test_P07_exclusoes_padrao(self):
        monitorados = {(s['cert_namespace'], s['name']) for s in certificados(self.series)}
        for excluido in [('openshift-etcd', 'etcd'), ('openshift', 'x'), ('kube-system', 'x'), ('default', 'x'),
                         ('app', 'kube-root-ca.crt'), ('app', 'openshift-service-ca.crt'),
                         ('app', 'trusted-ca-bundle'), ('app', 'config-trusted-cabundle')]:
            self.assertNotIn(excluido, monitorados)
        for incluido in [('openshift-ingress', 'router-certs'), ('openshift-config', 'user-ca-bundle'),
                         ('app', 'meu-trusted-ca-bundle')]:
            self.assertIn(incluido, monitorados)

    def test_P08_pem_invalido_conta_erro_sem_derrubar(self):
        self.assertEqual(escalar(self.series, 'cert_monitor_parse_errors'), 1)
        self.assertFalse(certificados(self.series, name='invalido'))

    def test_P09_contagem_de_entradas(self):
        # Entradas = séries exportadas + o PEM inválido (que entra na lista, mas não vira série)
        self.assertEqual(escalar(self.series, 'cert_monitor_certificates'), len(certificados(self.series)) + 1)

    def test_P10_mesmo_certificado_em_varios_recursos(self):
        a = certificados(self.series, name='tls-normal')
        b = certificados(self.series, name='cm-data')
        self.assertEqual((len(a), len(b)), (1, 1))
        self.assertEqual(a[0]['_valor'], b[0]['_valor'])

    def test_P11_timestamp_e_label_not_after(self):
        s = certificados(self.series, name='tls-normal')[0]
        self.assertEqual(int(s['_valor']), self.c['normal'].epoch)
        self.assertEqual(s['not_after'], self.c['normal'].not_after)

    def test_P12_escape_do_cn(self):
        s = certificados(self.series, name='escape')[0]
        self.assertEqual(s['subject_cn'], 'a"b\\c d')

    def test_P13_pem_com_crlf(self):
        self.assertEqual(int(certificados(self.series, name='crlf')[0]['_valor']), self.c['crlf'].epoch)

    def test_P14_ultima_execucao_recente(self):
        self.assertLess(abs(escalar(self.series, 'cert_monitor_last_run_timestamp_seconds') - time.time()), 300)

    def test_P15_relatorio_rfc5280(self):
        saida = self.r.saida
        self.assertIn('app/rfc (SECRET, chave tls.crt, posição 0): subject commonName com 78 bytes', saida)
        self.assertIn('app/rfc (SECRET, chave ca.crt, posição 0): número de série não positivo (0)', saida)
        self.assertIn('app/rfc (SECRET, chave s.crt, posição 0): número de série não positivo (-1)', saida)
        self.assertNotIn('app/tls-normal (SECRET', saida)
        # Certificados fora da RFC continuam exportados
        self.assertEqual(len(certificados(self.series, name='rfc')), 3)

    def test_P16_sem_avisos_crus_do_cryptography(self):
        self.assertNotIn('UserWarning', self.r.saida)
        self.assertNotIn('CryptographyDeprecationWarning', self.r.saida)
        self.assertNotIn('errantly sent data directly to stderr', self.r.saida)


class Exclusao(unittest.TestCase):
    def test_P17_lista_vazia_e_configmap_ausente(self):
        cert = Cert('qualquer')
        recursos = [secret('openshift-etcd', 'etcd', {'tls.crt': cert.pem})]
        for exclude in ({'namespaces': '', 'nomes': '[]'}, None):
            with self.subTest(exclude=exclude):
                r = executar(recursos, exclude=exclude)
                self.assertEqual(r.rc, 0, r.saida[-3000:])
                self.assertTrue(certificados(interpretar(r.payload), cert_namespace='openshift-etcd'))

    def test_P18_regex_invalida_falha_visivelmente(self):
        r = executar([secret('app', 'x', {'tls.crt': Cert('x').pem})], exclude={'namespaces': '- "(abc"'})
        self.assertNotEqual(r.rc, 0)
        self.assertEqual(r.envios, [])


class Persistencia(unittest.TestCase):
    """Garante que, a cada varredura, o estado anterior é totalmente substituído."""

    def test_P19_certificado_renovado_substitui_a_serie_antiga(self):
        antigo, novo = Cert('web', dias=10), Cert('web', dias=400)
        r1 = executar([secret('app', 'web', {'tls.crt': antigo.pem})])
        r2 = executar([secret('app', 'web', {'tls.crt': novo.pem})])
        s1, s2 = certificados(interpretar(r1.payload), name='web'), certificados(interpretar(r2.payload), name='web')
        self.assertEqual([s['not_after'] for s in s1], [antigo.not_after])
        self.assertEqual([s['not_after'] for s in s2], [novo.not_after])
        self.assertNotIn(antigo.not_after, r2.payload)
        self.assertEqual(r2.envios[0][0], 'PUT')  # PUT substitui o grupo inteiro no Pushgateway

    def test_P20_recurso_removido_some_das_metricas(self):
        r = executar([secret('app', 'outro', {'tls.crt': Cert('outro').pem})])
        self.assertFalse(certificados(interpretar(r.payload), name='web'))

    def test_P21_falha_no_envio_falha_o_job(self):
        r = executar([secret('app', 'web', {'tls.crt': Cert('web').pem})], pushgateway_url='http://127.0.0.1:9')
        self.assertNotEqual(r.rc, 0)


class Renovacao(unittest.TestCase):
    """Detecção de renovação (cert_renewed_timestamp_seconds), usando o estado anterior lido do Pushgateway."""

    def varredura(self, recursos, anterior=None):
        r = executar(recursos, estado=estado_api(anterior.payload) if anterior else None)
        self.assertEqual(r.rc, 0, r.saida[-3000:])
        return r, interpretar(r.payload)

    def test_P24_primeira_execucao_nao_marca_nada(self):
        _, series = self.varredura([secret('app', 'web', {'tls.crt': Cert('web').pem})])
        self.assertTrue(certificados(series, name='web'))
        self.assertEqual(renovados(series), [])

    def test_P25_certificado_renovado_e_marcado(self):
        estavel = Cert('estavel', dias=100)
        r1, _ = self.varredura([secret('app', 'web', {'tls.crt': Cert('web', dias=10).pem}),
                                secret('app', 'estavel', {'tls.crt': estavel.pem})])
        novo = Cert('web', dias=400)
        _, series = self.varredura([secret('app', 'web', {'tls.crt': novo.pem}),
                                    secret('app', 'estavel', {'tls.crt': estavel.pem}),
                                    secret('app', 'recurso-novo', {'tls.crt': Cert('novo').pem})], anterior=r1)
        marcados = renovados(series)
        self.assertEqual([(m['name'], m['not_after']) for m in marcados], [('web', novo.not_after)])
        self.assertLess(abs(marcados[0]['_valor'] - time.time()), 300)
        # Mesmo conjunto de labels da série de expiração, para junções diretas no PromQL/Grafana
        cert = certificados(series, name='web')[0]
        self.assertEqual({k: v for k, v in marcados[0].items() if k != '_valor'},
                         {k: v for k, v in cert.items() if k != '_valor'})

    def test_P26_renovacao_para_validade_menor(self):
        r1, _ = self.varredura([secret('app', 'web', {'tls.crt': Cert('web', dias=300).pem})])
        novo = Cert('web', dias=90)
        _, series = self.varredura([secret('app', 'web', {'tls.crt': novo.pem})], anterior=r1)
        self.assertEqual([m['not_after'] for m in renovados(series)], [novo.not_after])

    def test_P27_reordenar_bundle_nao_e_renovacao(self):
        a, b, c = Cert('a', dias=100), Cert('b', dias=200), Cert('c', dias=300)
        r1, _ = self.varredura([secret('app', 'bundle', {'ca.crt': a.pem + b.pem + c.pem})])
        _, series = self.varredura([secret('app', 'bundle', {'ca.crt': c.pem + a.pem + b.pem})], anterior=r1)
        self.assertEqual(renovados(series), [])

    def test_P28_marcacao_mantida_e_expirada(self):
        r1, _ = self.varredura([secret('app', 'web', {'tls.crt': Cert('web', dias=10).pem})])
        recursos = [secret('app', 'web', {'tls.crt': Cert('web', dias=400).pem})]
        r2, _ = self.varredura(recursos, anterior=r1)
        for idade, mantida in ((2 * 3600, True), (8 * 86400, False)):
            with self.subTest(idade_horas=idade // 3600):
                estado = estado_api(r2.payload)
                ts = int(time.time()) - idade
                for m in estado['data'][0]['cert_renewed_timestamp_seconds']['metrics']:
                    m['value'] = f'{ts:.15e}'
                r3 = executar(recursos, estado=estado)
                marcados = renovados(interpretar(r3.payload), name='web')
                self.assertEqual([int(m['_valor']) for m in marcados], [ts] if mantida else [])

    def test_P29_estado_indisponivel_nao_impede_o_envio(self):
        r = executar([secret('app', 'web', {'tls.crt': Cert('web').pem})], estado=500)
        self.assertEqual(r.rc, 0, r.saida[-3000:])
        series = interpretar(r.payload)
        self.assertTrue(certificados(series, name='web'))
        self.assertEqual(renovados(series), [])


class Seguranca(unittest.TestCase):
    def test_P22_regex_de_pem_linear(self):
        pem_regex = yaml.safe_load(PLAYBOOK.read_text())[0]['vars']['pem_regex']
        malicioso = '-----BEGIN CERTIFICATE-----\nAAAA\n' * 32000  # ~1 MiB, limite de um ConfigMap
        inicio = time.monotonic()
        re.findall(pem_regex, malicioso)
        self.assertLess(time.monotonic() - inicio, 2, 'regex com custo superlinear (risco de DoS)')

    def test_P23_configmap_malicioso_nao_impede_a_varredura(self):
        valido = Cert('valido')
        r = executar(configmaps=[
            configmap('app', 'malicioso', dados={'ca.crt': '-----BEGIN CERTIFICATE-----\nAAAA\n' * 32000}),
            configmap('app', 'valido', dados={'ca.crt': valido.pem})])
        self.assertEqual(r.rc, 0, r.saida[-3000:])
        self.assertTrue(certificados(interpretar(r.payload), name='valido'))
        self.assertLess(r.duracao, 120)


if __name__ == '__main__':
    unittest.main()
