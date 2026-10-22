FROM quay.io/operator-framework/ansible-operator:v1.42.2

USER root
RUN pip3 install --no-cache-dir kafka-python==3.0.11
USER 1001

COPY namespace/requirements.yml ${HOME}/requirements.yml
RUN ansible-galaxy collection install -r ${HOME}/requirements.yml \
 && chmod -R ug+rwx ${HOME}/.ansible

COPY namespace/watches.yaml ${HOME}/watches.yaml
COPY roles/_common/ ${HOME}/roles/_common/
COPY namespace/roles/ ${HOME}/roles/
