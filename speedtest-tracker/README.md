# speedtest-tracker
This container runs a speedtest every hour and graphs the results. The back-end is written in Laravel and the front-end uses React. It uses Ookla's Speedtest cli to get the data and uses Chart.js to plot the results.

Project Source: https://github.com/henrywhitaker3/Speedtest-Tracker

![image](https://user-images.githubusercontent.com/127797701/226963907-80ee2aae-f1d5-499b-9b84-ba0f6d8c8559.png)

## Setup:
- Edit your Cradlepoint router configuration and navigate to System > Containers > Projects and click Add.  
- Give your project a name ("Speedtest-Tracker") and click on the Compose tab.
- Paste the contents of the docker-compose file into the compose tab of your project and click save.

![image](https://user-images.githubusercontent.com/127797701/226963581-e4f081b3-865b-486e-8064-1d13828b6106.png)

## Usage:  
- Use NCM Remote Connect LAN Manager to create a profile for "Speedtest-Tracker" at 127.0.0.1 port 8000 protocol HTTP.
- Connect to the "Speedtest-Tracker" profile you created.


