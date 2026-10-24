# Usa a imagem oficial Red Hat UBI 9 com Python 3.11
FROM registry.access.redhat.com/ubi9/python-311:latest

# Muda temporariamente para root para instalar pacotes e ajustar permissões
USER root

# Atualiza o pip e instala as dependências
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir ansible kubernetes cryptography

# Instala as collections necessárias
RUN ansible-galaxy collection install kubernetes.core community.crypto

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