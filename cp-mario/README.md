# cp-mario
This project runs the jgaudu/cp-mario container which runs the game "Infinite Mario Bros" in a web interface on port 8080.  
No background music (sorry). Use arrow keys for left and right, 'A' and 'S' for run and jump.  
  
> ARM64 only  

![image](https://user-images.githubusercontent.com/7169690/227016810-4c7c2f26-7bb5-4604-915c-c1220d17ad28.png)

## Setup:  
- Edit your Cradlepoint router configuration and navigate to System > Containers > Projects and click Add.  
- Give your project a name ("Speedtest-Tracker") and click on the Compose tab.  
- Paste the following YAML into the compose tab of your project and click save:  
  
```yaml
version: '2.4'
services:
  cp-mario:
    image: jgaudu/cp-mario
    restart: unless-stopped
    ports:
     - 8080:8080
```
  
![image](https://user-images.githubusercontent.com/7169690/227014750-f1e48eaa-6e8e-408b-9e0f-f94ddecc928e.png)
  
## Usage:  

#### NCM:  
- Use NCM Remote Connect LAN Manager to create a profile for "CP-Mario" at 127.0.0.1 port 8080 protocol HTTP.   
- Connect to the "CP-Mario" profile you created.  
  
#### Local:
- In the routers Zone Firewall, forward the PrimaryLAN Zone to the Router with DefaultAllowAll policy.
- Browse to the IP address of your router, port 8080 (e.g. http://192.168.0.1:8080)

## Docker Hub:  
https://hub.docker.com/r/jgaudu/cp-mario
