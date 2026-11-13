#!/usr/bin/env bash
# Caderno de testes automatizado (ver tests/CADERNO-DE-TESTES.md). Roda localmente e no CI.
# Etapas sem a ferramenta disponível são puladas com aviso (no CI todas estão disponíveis).
set -uo pipefail
cd "$(dirname "$0")/.."

PROMETHEUS_IMAGEM="docker.io/prom/prometheus:v3.16.0"
falhas=()
puladas=()

etapa() {
  local nome=$1
  shift
  echo "=== ${nome}"
  if "$@"; then
    echo "--- OK: ${nome}"
  else
    echo "--- FALHOU: ${nome}"
    falhas+=("${nome}")
  fi
}

pular() {
  echo "=== $1"
  echo "--- PULADO: $1 ($2)"
  puladas+=("$1")
}

kubeconform_validar() {
  kubectl kustomize . | kubeconform -strict -summary \
    -schema-location default \
    -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
    -skip ImageStream,BuildConfig
}

# promtool local ou, na falta dele, via container (podman/docker)
promtool_cmd=()
if command -v promtool > /dev/null; then
  promtool_cmd=(promtool)
else
  for runtime in podman docker; do
    if command -v "${runtime}" > /dev/null; then
      promtool_cmd=("${runtime}" run --rm -v "__DIR__:/t:Z" -w /t --entrypoint promtool "${PROMETHEUS_IMAGEM}")
      break
    fi
  done
fi

alertas_validar() {
  local dir rc
  dir=$(mktemp -d)
  # Extrai o spec do PrometheusRule para o formato de regras do Prometheus
  python3 -I -c 'import sys, yaml; print(yaml.safe_dump(yaml.safe_load(open(sys.argv[1]))["spec"], allow_unicode=True))' \
    custom_alerts.yaml > "${dir}/regras.yaml" && cp tests/alertas.test.yaml "${dir}/"
  # O promtool do container roda como nobody e precisa ler o diretório
  chmod -R a+rX "${dir}"
  local cmd=("${promtool_cmd[@]/__DIR__/${dir}}")
  if [[ ${cmd[0]} == promtool ]]; then
    (cd "${dir}" && promtool check rules regras.yaml && promtool test rules alertas.test.yaml)
  else
    "${cmd[@]}" check rules regras.yaml && "${cmd[@]}" test rules alertas.test.yaml
  fi
  rc=$?
  rm -rf "${dir}"
  return "${rc}"
}

etapa "L01 yamllint" yamllint .
etapa "L02 ansible-lint" ansible-lint check-certs.yaml
etapa "L03 kustomize build" sh -c 'kubectl kustomize . > /dev/null'
if command -v kubeconform > /dev/null; then
  etapa "L04 kubeconform" kubeconform_validar
else
  pular "L04 kubeconform" "kubeconform não instalado"
fi
etapa "P01-P23 testes do playbook" python3 tests/test_playbook.py -v
if [[ ${#promtool_cmd[@]} -gt 0 ]]; then
  etapa "A01-A07 regras de alerta" alertas_validar
else
  pular "A01-A07 regras de alerta" "promtool, podman ou docker não encontrados"
fi

echo
echo "===== Resumo"
echo "Puladas: ${#puladas[@]} ${puladas[*]:+(${puladas[*]})}"
if [[ ${#falhas[@]} -gt 0 ]]; then
  printf 'FALHAS: %s\n' "${falhas[@]}"
  exit 1
fi
echo "Todas as etapas executadas passaram."
