# web-video-player

Purpose:
NGINX webserver with index.html serving embedded video using javascript library.

Usage:
1. Put your video in the folder and name it video.mp4 (or edit index.html and change the video source filename)
2. Put your thumbnail image in the folder and name it thumbnail.png - this is displayed before the video is played
3. Build the image and push to your docker repo:  

```
docker login  
docker buildx build --platform linux/arm64,linux/arm/v7 -t docker-username/repo-name --push .  
```
