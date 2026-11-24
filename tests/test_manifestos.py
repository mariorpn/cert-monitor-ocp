#!/usr/bin/env python3
"""Testes dos manifestos gerados pelo kustomize (ver tests/CADERNO-DE-TESTES.md, M01–M02).

Roda `kubectl kustomize .` localmente (sem contato com cluster) e verifica a persistência do Pushgateway.
"""
import subprocess
import unittest
from pathlib import Path

import yaml

RAIZ = Path(__file__).resolve().parent.parent


def recursos():
    saida = subprocess.run(['kubectl', 'kustomize', str(RAIZ)], capture_output=True, text=True, check=True).stdout
    return [r for r in yaml.safe_load_all(saida) if r]


class PersistenciaPushgateway(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.todos = recursos()
        cls.deploy = next(r for r in cls.todos if r['kind'] == 'Deployment' and r['metadata']['name'] == 'pushgateway')
        cls.pod = cls.deploy['spec']['template']['spec']
        cls.container = next(c for c in cls.pod['containers'] if c['name'] == 'pushgateway')

    def test_M01_metricas_gravadas_em_volume_persistente(self):
        args = dict(a.lstrip('-').split('=', 1) for a in self.container['args'] if '=' in a)
        arquivo = args.get('persistence.file')
        self.assertTrue(arquivo, 'Pushgateway sem --persistence.file')
        # O diretório do arquivo de persistência é um volume montado...
        montagem = next((m for m in self.container['volumeMounts'] if arquivo.startswith(m['mountPath'].rstrip('/') + '/')), None)
        self.assertIsNotNone(montagem, f'{arquivo} não está num volume montado')
        # ...que vem de um PersistentVolumeClaim (não emptyDir)...
        volume = next(v for v in self.pod['volumes'] if v['name'] == montagem['name'])
        self.assertIn('persistentVolumeClaim', volume, f'volume {volume["name"]} não é um PVC: {volume}')
        # ...declarado no mesmo kustomize e no mesmo namespace, com tamanho solicitado
        nome = volume['persistentVolumeClaim']['claimName']
        pvc = next((r for r in self.todos if r['kind'] == 'PersistentVolumeClaim' and r['metadata']['name'] == nome), None)
        self.assertIsNotNone(pvc, f'PVC {nome} não está nos manifestos')
        self.assertEqual(pvc['metadata'].get('namespace'), self.deploy['metadata'].get('namespace'))
        self.assertTrue(pvc['spec']['resources']['requests'].get('storage'))

    def test_M02_estrategia_recreate_com_volume_rwo(self):
        pvcs = {r['metadata']['name']: r for r in self.todos if r['kind'] == 'PersistentVolumeClaim'}
        rwo = [v for v in self.pod['volumes'] if 'persistentVolumeClaim' in v
               and 'ReadWriteOnce' in pvcs.get(v['persistentVolumeClaim']['claimName'], {}).get('spec', {}).get('accessModes', [])]
        if rwo:
            # RollingUpdate subiria o pod novo com o antigo ainda montando o volume (Multi-Attach error)
            self.assertEqual(self.deploy['spec'].get('strategy', {}).get('type'), 'Recreate')
            self.assertEqual(self.deploy['spec'].get('replicas', 1), 1)


if __name__ == '__main__':
    unittest.main()
