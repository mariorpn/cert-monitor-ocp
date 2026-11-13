#!/usr/bin/env bash
# Caderno de testes automatizado (ver tests/CADERNO-DE-TESTES.md). Roda localmente e no CI.
# Etapas sem a ferramenta disponível são puladas com aviso (no CI todas estão disponíveis).
set -uo pipefail
cd "$(dirname "$0")/.."

PROMETHEUS_IMAGEM="docker.io/prom/prometheus:v3.16.0"
GITLEAKS_IMAGEM="ghcr.io/gitleaks/gitleaks:v8.30.1"
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

container_runtime=""
for runtime in podman docker; do
  if command -v "${runtime}" > /dev/null; then
    container_runtime=${runtime}
    break
  fi
done

# promtool local ou, na falta dele, via container
promtool_cmd=()
if command -v promtool > /dev/null; then
  promtool_cmd=(promtool)
elif [[ -n ${container_runtime} ]]; then
  promtool_cmd=("${container_runtime}" run --rm -v "__DIR__:/t:Z" -w /t --entrypoint promtool "${PROMETHEUS_IMAGEM}")
fi

# gitleaks local ou, na falta dele, via container (o repositório é montado em /repo)
gitleaks_cmd=()
gitleaks_repo=.
if command -v gitleaks > /dev/null; then
  gitleaks_cmd=(gitleaks)
elif [[ -n ${container_runtime} ]]; then
  # safe.directory: o dono do repositório montado difere do usuário do container
  gitleaks_cmd=("${container_runtime}" run --rm -v "${PWD}:/repo:Z" -e GIT_CONFIG_COUNT=1
    -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0='*' "${GITLEAKS_IMAGEM}")
  gitleaks_repo=/repo
fi

# Roda comandos do promtool dentro de um diretório (local ou montado no container)
promtool_em() {
  local dir=$1
  shift
  # O promtool do container roda como nobody e precisa ler o diretório
  chmod -R a+rX "${dir}"
  local cmd=("${promtool_cmd[@]/__DIR__/${dir}}")
  if [[ ${cmd[0]} == promtool ]]; then
    (cd "${dir}" && promtool "$@")
  else
    "${cmd[@]}" "$@"
  fi
}

alertas_validar() {
  local dir rc
  dir=$(mktemp -d)
  # Extrai o spec do PrometheusRule para o formato de regras do Prometheus
  python3 -I -c 'import sys, yaml; print(yaml.safe_dump(yaml.safe_load(open(sys.argv[1]))["spec"], allow_unicode=True))' \
    custom_alerts.yaml > "${dir}/regras.yaml" && cp tests/alertas.test.yaml "${dir}/" &&
    promtool_em "${dir}" check rules regras.yaml && promtool_em "${dir}" test rules alertas.test.yaml
  rc=$?
  rm -rf "${dir}"
  return "${rc}"
}

dashboard_validar() {
  local dir rc
  dir=$(mktemp -d)
  # D01: sintaxe de todas as consultas; D02: resultado das consultas principais sobre séries sintéticas
  python3 tests/dashboard_promql.py grafana/cert-monitor-dashboard.json "${dir}" &&
    promtool_em "${dir}" check rules regras-dashboard.yaml && promtool_em "${dir}" test rules dashboard.test.yaml
  rc=$?
  rm -rf "${dir}"
  return "${rc}"
}

# S01: histórico do git (todos os commits) e arquivos atuais, inclusive os ainda não commitados
segredos_validar() {
  "${gitleaks_cmd[@]}" git --redact --no-banner "${gitleaks_repo}" &&
    "${gitleaks_cmd[@]}" dir --redact --no-banner "${gitleaks_repo}"
}

if [[ ${#gitleaks_cmd[@]} -gt 0 ]]; then
  etapa "S01 segredos (gitleaks)" segredos_validar
else
  pular "S01 segredos (gitleaks)" "gitleaks, podman ou docker não encontrados"
fi
etapa "L01 yamllint" yamllint .
etapa "L02 ansible-lint" ansible-lint check-certs.yaml
etapa "L03 kustomize build" sh -c 'kubectl kustomize . > /dev/null'
if command -v kubeconform > /dev/null; then
  etapa "L04 kubeconform" kubeconform_validar
else
  pular "L04 kubeconform" "kubeconform não instalado"
fi
etapa "P01-P29 testes do playbook" python3 tests/test_playbook.py -v
if [[ ${#promtool_cmd[@]} -gt 0 ]]; then
  etapa "A01-A08 regras de alerta" alertas_validar
  etapa "D01-D02 consultas do dashboard" dashboard_validar
else
  pular "A01-A08 regras de alerta" "promtool, podman ou docker não encontrados"
  pular "D01-D02 consultas do dashboard" "promtool, podman ou docker não encontrados"
fi

echo
echo "===== Resumo"
echo "Puladas: ${#puladas[@]} ${puladas[*]:+(${puladas[*]})}"
if [[ ${#falhas[@]} -gt 0 ]]; then
  printf 'FALHAS: %s\n' "${falhas[@]}"
  exit 1
fi
echo "Todas as etapas executadas passaram."
