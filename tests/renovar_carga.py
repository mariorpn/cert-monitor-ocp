#!/usr/bin/env python3
"""Renova X certificados da carga criada por gerar_carga.py: troca o certificado vencido ou a vencer
por um novo (mais de 90 dias) no mesmo Secret/ConfigMap e na mesma chave, que é o que o playbook
detecta como renovação na varredura seguinte.

Uso (com `oc` logado no cluster):
    python3 tests/renovar_carga.py 50
    oc create job --from=cronjob/cert-monitor-cron renovacao-teste -n cert-monitor
"""
import argparse
import base64
import datetime as dt
import json
import random
import subprocess

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from gerar_carga import FAIXA_LONGA, FAIXAS_CURTAS, gerar_cert

CHAVE_CERT = {"Secret": "tls.crt", "ConfigMap": "ca.crt"}


def oc(*args, entrada=None):
    return subprocess.run(["oc", *args], input=entrada, capture_output=True, text=True, check=True).stdout


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("quantidade", type=int, help="quantos certificados renovar")
    p.add_argument("--faixas", default=",".join(f[0] for f in FAIXAS_CURTAS),
                   help="faixas elegíveis, separadas por vírgula (padrão: todas abaixo de 90 dias)")
    p.add_argument("--semente", type=int)
    args = p.parse_args()

    seletor = f"cert-monitor-teste=true,faixa in ({args.faixas})"
    candidatos = json.loads(oc("get", "secret,configmap", "-A", "-l", seletor, "-o", "json"))["items"]
    if len(candidatos) < args.quantidade:
        raise SystemExit(f"só há {len(candidatos)} certificados nas faixas {args.faixas}")

    rng = random.Random(args.semente)
    agora = dt.datetime.now(dt.timezone.utc)
    chave = ec.generate_private_key(ec.SECP256R1())
    chave_pem = chave.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
    ).decode()

    for obj in rng.sample(candidatos, args.quantidade):
        kind, meta = obj["kind"], obj["metadata"]
        pem = obj["data"][CHAVE_CERT[kind]]
        if kind == "Secret":
            pem = base64.b64decode(pem).decode()
        cn = x509.load_pem_x509_certificate(pem.encode()).subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value

        novo = gerar_cert(chave, cn, agora, FAIXA_LONGA, rng)
        campo = {"stringData": {"tls.crt": novo, "tls.key": chave_pem}} if kind == "Secret" else {"data": {"ca.crt": novo}}
        patch = {**campo, "metadata": {"labels": {"faixa": "renovado", "faixa-anterior": meta["labels"]["faixa"]}}}
        oc("patch", kind.lower(), meta["name"], "-n", meta["namespace"], "--type=merge", "-p", json.dumps(patch))
        print(f"{meta['namespace']}/{kind}/{meta['name']}: {meta['labels']['faixa']} -> renovado")

    print(f"\n{args.quantidade} certificados renovados. Rode um Job do cert-monitor para detectar a renovação.")


if __name__ == "__main__":
    main()
