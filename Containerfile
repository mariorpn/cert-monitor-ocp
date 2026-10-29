# Usa a imagem oficial Red Hat UBI 9 com Python 3.12 (tag fixa para builds reproduzíveis)
FROM registry.access.redhat.com/ubi9/python-312:9.8-1790171739

# Muda temporariamente para root para instalar pacotes e ajustar permissões
USER root

# Instala as dependências Python com versões fixas
COPY requirements.txt requirements.yml /tmp/deps/
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /tmp/deps/requirements.txt

# Instala as collections num caminho padrão do Ansible, independente do HOME em tempo de execução
RUN ansible-galaxy collection install -r /tmp/deps/requirements.yml -p /usr/share/ansible/collections && \
    rm -rf /tmp/deps

# Prepara o diretório de trabalho
WORKDIR /opt/app-root/src

# Copia o playbook
COPY check-certs.yaml .

# Ajusta as permissões para a convenção do OpenShift (permissão total para o grupo 0)
RUN chown -R 1001:0 /opt/app-root/src && \
    chmod -R g+rwX /opt/app-root/src

# Variáveis de ambiente para forçar o Ansible a usar o /tmp do container (sempre gravável)
ENV ANSIBLE_LOCAL_TEMP=/tmp/ansible-local
ENV ANSIBLE_REMOTE_TEMP=/tmp/ansible-remote
ENV HOME=/tmp

# Retorna para o usuário não-root default da imagem
USER 1001

# Comando de execução
CMD ["ansible-playbook", "check-certs.yaml"]
