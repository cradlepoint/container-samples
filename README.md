## Build and Publish a Docker Image for use on a Cradlepoint router

* ##### Requirements

  * ###### For Cradlepoint routers (IBR1700, AER2200, E300, E3000) with NCOS V7.2.20 or greater.

  * ###### The IBR1700, AER2200, and E300 use ARM7 architecture, and the E3000 uses ARM64 architecture. They must be built for the correct architecture in order to run correctly.
  
  * ###### This example assumes that you have included the ssh / openssh-server packages in your Dockerfile. This is not required if you don't want it installed.

###### 1. Download the Dockerfile and the docker-compose.yaml files to a folder. Change directory to this folder.

###### 2. Test and build the docker container

```
$ docker build -t <username/repository> .
```

###### 3. Verify the container has been built

```
$ docker image ls
```

###### 4. Start the container in the background

```
$ docker run -itdP --name ubuntu_example <username/repository>
```

###### 5. Verify SSH is running

```
$ docker port ubuntu_example 22
0.0.0.0:32770 <-- this port is random
```

###### 6. Connect to the random port via SSH

```
$ ssh ubuntu@localhost -p <listed_port>
```

###### 7. For multi-architecture builds, we will leverage the buildx experimental feature of Docker. To enable buildx, enable the Command Line Experimental Features of Docker. To accomplish this, run the following command from the terminal that will be used to build the container. If you are running Mac OS-X and have Docker Desktop installed, you can enable the experimental features from preferences menu.  

```
$ export DOCKER_CLI_EXPERIMENTAL=enabled
```

###### 8. To support building different architectures, register multi-architecture QEMU kernel handlers with host kernel by execute the following command to run the binfmt docker container.  You man need to run this command each time you reboot your host machine.

```
$ docker run -it --rm --privileged docker/binfmt:a7996909642ee92942dcd6cff44b9b95f08dad64
```

###### 9. Next, setup a builder for building multi-architecture Docker images.

```
$ docker buildx create --name mybuilder
$ docker buildx use mybuilder
```

###### 10. To ensure the builder is setup correctly, execute the following command and verify the output is similar to what is shown below. It is important to verify the desired target architecture is listed in Platforms. 

```
$ docker buildx inspect --bootstrap

Name: mybuilder
Driver: docker-container
Nodes:
Name: mybuilder0
Endpoint: unix:///var/run/docker.sock
Status: running
Platforms: linux/amd64, linux/arm64, linux/ppc64le, linux/s390x, linux/386, linux/arm/v7, linux/arm/v6
```

###### 11. Ensure that you are logged into DockerHub:

```
$ docker login
```

###### 12. Once the builder is setup, we can build the Docker image(s) and push to the repository that was created previously. In this example, the Docker image is built for the ARM7 architecture. Also note that you need to specify your repository information.

```
$ docker buildx build --platform linux/arm/v7 --no-cache -t <username/repository>:<tag> . --push
```

###### 13. At this point, the custom ubuntu docker image has been created and pushed to a repository where the router container runtime can pull the image down to the router and run it. This can take a few minutes to pull down. By default, the ports exposed by the containers will also be accessible on the WAN. To block these ports on the WAN, a zone firewall policy must be configured.