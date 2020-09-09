FROM ubuntu:20.04
ENV PYTHONUNBUFFERED 1
ENV TIME_ZONE=America/Boise
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update
RUN adduser --disabled-password ubuntu
RUN echo "ubuntu:ubuntu" | chpasswd
RUN apt install ssh openssh-server python3 python3-pip -y && pip3 install --upgrade pip setuptools
EXPOSE 22
EXPOSE 8022
CMD /usr/sbin/sshd -D
ENTRYPOINT service ssh restart && tail -f /dev/null