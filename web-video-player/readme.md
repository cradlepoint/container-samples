# web-video-player

### Purpose:
NGINX webserver with index.html serving embedded video using javascript library.

![image](https://user-images.githubusercontent.com/7169690/221065573-005d6b62-7788-49ff-91b4-9ef174bae7fc.png)

### Build:
1. Put your video in the folder and name it video.mp4 (or edit index.html and change the video source filename)
2. Put your thumbnail image in the folder and name it thumbnail.png - this is displayed before the video is played
3. Build the image and push to your docker repo:  

```
docker login  
docker buildx build --platform linux/arm64,linux/arm/v7 -t docker-username/repo-name --push .  
```

### Setup Container:
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

### Watch video:
#### From Netcloud Manager:
- Use Remote Connect LAN Manager to connect to 127.0.0.1 port 8000 HTTP.  

#### Locally:  
- In the zone firewall, forward the PrimaryLAN zone to the RouterZone with DefaultAllowAll policy.  
- Open a browser and navigate to your router IP address port 8000.  
Example:  
http://192.168.0.1:8000  

### Docker homepage:  
https://hub.docker.com/r/cpcontainer/web-video-player
