# web-video-player

### Purpose:
NGINX webserver with index.html serving embedded video using javascript library.

### Build:
1. Put your video in the folder and name it video.mp4 (or edit index.html and change the video source filename)
2. Put your thumbnail image in the folder and name it thumbnail.png - this is displayed before the video is played
3. Build the image and push to your docker repo:  

```
docker login  
docker buildx build --platform linux/arm64,linux/arm/v7 -t docker-username/repo-name --push .  
```

### Usage:
Create a new container project on your Cradlepoint router and enter the following into the project compose tab:  

```yaml
version: '2.4'
services:
  video:
    image: 'docker-username/repo-name'
    restart: unless-stopped
    ports:
     - 8000:80
```

### Docker homepage:  
https://hub.docker.com/r/cpcontainer/web-video-player
