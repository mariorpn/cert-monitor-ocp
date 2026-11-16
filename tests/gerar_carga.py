#!/usr/bin/env python3
"""Gera manifestos de carga para testar o cert-monitor: N namespaces, cada um com
1 Secret TLS e 4 ConfigMaps com certificado, com validades sorteadas por faixa.

Uso:
    python3 tests/gerar_carga.py [--namespaces 1400] [--saida carga-gerada] [--semente 42]
    ls carga-gerada/*.yaml | xargs -P8 -n1 oc create -f        # aplica em paralelo
    oc delete ns -l cert-monitor-teste=true --wait=false        # remove tudo

Para testar renovações depois da primeira varredura, veja renovar_carga.py.
"""
import argparse
import collections
import datetime as dt
import random
from pathlib import Path

import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

# (nome, dias mínimos exclusivo, dias máximos inclusivo)
FAIXAS_CURTAS = [
    ("vencido", -365, 0),
    ("ate-5d", 0, 5),
    ("ate-15d", 5, 15),
    ("ate-30d", 15, 30),
    ("ate-45d", 30, 45),
    ("ate-60d", 45, 60),
    ("ate-90d", 60, 90),
]
FAIXA_LONGA = ("mais-90d", 90, 825)
NS_POR_ARQUIVO = 100


def sortear_faixa(rng):
    # 2/3 com mais de 90 dias; o terço restante dividido igualmente entre as faixas curtas
    return FAIXA_LONGA if rng.random() < 2 / 3 else rng.choice(FAIXAS_CURTAS)


def gerar_cert(chave, cn, agora, faixa, rng):
    _, minimo, maximo = faixa
    # Sorteio em segundos dentro de (minimo, maximo]; a margem de 1h evita cair na fronteira
    segundos = rng.uniform(minimo * 86400 + 3600, maximo * 86400 - 3600)
    not_after = agora + dt.timedelta(seconds=segundos)
    not_before = min(agora, not_after) - dt.timedelta(days=rng.randint(30, 365))
    assert minimo < (not_after - agora).total_seconds() / 86400 <= maximo
    nome = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(nome)
        .issuer_name(nome)
        .public_key(chave.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .sign(chave, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--namespaces", type=int, default=1400)
    p.add_argument("--saida", default="carga-gerada")
    p.add_argument("--prefixo", default="certteste")
    p.add_argument("--semente", type=int, help="fixa o sorteio para repetir a mesma carga")
    args = p.parse_args()

    rng = random.Random(args.semente)
    agora = dt.datetime.now(dt.timezone.utc)
    # shortcut: uma única chave EC para todos os certificados (gerar 7000 chaves é lento e irrelevante para o monitor)
    chave = ec.generate_private_key(ec.SECP256R1())
    chave_pem = chave.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
    ).decode()

    saida = Path(args.saida)
    saida.mkdir(parents=True, exist_ok=True)
    contagem = collections.Counter()
    docs = []

    for i in range(1, args.namespaces + 1):
        ns = f"{args.prefixo}-{i:04d}"
        rotulo = {"cert-monitor-teste": "true"}
        docs.append({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns, "labels": rotulo}})

        faixa = sortear_faixa(rng)
        contagem[faixa[0]] += 1
        docs.append({
            "apiVersion": "v1", "kind": "Secret", "type": "kubernetes.io/tls",
            "metadata": {"name": "app-tls", "namespace": ns, "labels": {**rotulo, "faixa": faixa[0]}},
            "stringData": {"tls.crt": gerar_cert(chave, f"app.{ns}.lab", agora, faixa, rng), "tls.key": chave_pem},
        })

        for j in range(1, 5):
            faixa = sortear_faixa(rng)
            contagem[faixa[0]] += 1
            docs.append({
                "apiVersion": "v1", "kind": "ConfigMap",
                "metadata": {"name": f"ca-cert-{j}", "namespace": ns, "labels": {**rotulo, "faixa": faixa[0]}},
                "data": {"ca.crt": gerar_cert(chave, f"ca-{j}.{ns}.lab", agora, faixa, rng)},
            })

        if i % NS_POR_ARQUIVO == 0 or i == args.namespaces:
            arquivo = saida / f"carga-{(i - 1) // NS_POR_ARQUIVO + 1:03d}.yaml"
            arquivo.write_text(yaml.safe_dump_all(docs, sort_keys=False))
            docs = []

    total = sum(contagem.values())
    print(f"{args.namespaces} namespaces, {total} certificados em {saida}/")
    for nome, *_ in FAIXAS_CURTAS + [FAIXA_LONGA]:
        print(f"  {nome:10} {contagem[nome]:6}  ({contagem[nome] / total:.1%})")


if __name__ == "__main__":
    main()
