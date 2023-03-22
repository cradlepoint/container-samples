# speedtest-tracker
This container runs a speedtest every hour and graphs the results. The back-end is written in Laravel and the front-end uses React. It uses Ookla's Speedtest cli to get the data and uses Chart.js to plot the results.

![image](https://user-images.githubusercontent.com/127797701/226963907-80ee2aae-f1d5-499b-9b84-ba0f6d8c8559.png)

## Setup:
- Edit your Cradlepoint router configuration and navigate to System > Containers > Projects and click Add.  
- Give your project a name ("Speedtest-Tracker") and click on the Compose tab.
- Paste the following YAML into the compose tab of your project and click save:

```yaml
version: '2.4'
services:
  speedtest:
    image: henrywhitaker3/speedtest-tracker:dev-arm
    ports:
     - 8000:80
    volumes:
     - data:/config
    environment:
     - OOKLA_EULA_GDPR=true
    restart: unless-stopped
volumes:
  data:
    driver: local
```

![image](https://user-images.githubusercontent.com/127797701/226963581-e4f081b3-865b-486e-8064-1d13828b6106.png)

## Usage:  
- Use NCM Remote Connect LAN Manager to create a profile for "Speedtest-Tracker" at 127.0.0.1 port 8000 protocol HTTP.
- Connect to the "Speedtest-Tracker" profile you created.

## Docker Hub:  
https://hub.docker.com/r/henrywhitaker3/speedtest-tracker
