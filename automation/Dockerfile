FROM python:3.13-slim
WORKDIR /opt/quicklab
RUN python -m pip install --no-cache-dir 'ansible-core>=2.20,<2.21'
COPY site.yml qga_setup.py ./
ENV HOME=/tmp ANSIBLE_LOCAL_TEMP=/tmp/ansible-local ANSIBLE_NOCOLOR=1 PYTHONDONTWRITEBYTECODE=1
RUN ansible-playbook -i localhost, --syntax-check site.yml
ENTRYPOINT ["ansible-playbook", "-i", "localhost,", "/opt/quicklab/site.yml"]
