# MTConnect/QIF Feature Traceability Demonstration with LinuxCNC

## Overview

Repo for experimenting with [LinuxCNC](https://linuxcnc.org/), [MTConnect](https://www.mtconnect.org/), and [QIF](https://qifstandards.org/) feature traceability. 

The objective is: 

1. Embed QIF feature traceability in g-code. For this example, we have embedded data in: [NIST_MTC_CRADA_BOX_REV-A OP3 (Back).NC](mtc-box/NC%20Code/NIST_MTC_CRADA_BOX_REV-A%20OP3%20%28Back%29.NC)
2. Run the g-code in LinuxCNC in simulation mode with some random noise
3. Connect an MTConnect Adapter to LinuxCNC to extract telemetry and pass along to Agent as Simple Hierarchical Data Representation (SHDR) data
4. Collect SHDR data and provide via web endpoint
5. Client to collect the data, sort it, and save it as XML

We will then reestablish the traceability of this data back to the original QIF features from which it came. 

### Important links:

* LinuxCNC documentation https://linuxcnc.org/docs/html/config/python-interface.html?utm_source=chatgpt.com#_linuxcnc_stat_attributes
* MTConnect documentaion https://docs.mtconnect.org/

For this demonstration, you will need to install LinuxCNC. Linux and Mac can run this natively. If you are on Windows, you can install it with Debian WSL. This assumes that you have already installed LinuxCNC.

## Initial Setup 

### Setup environment variables

Before running any of the commands below, you need to set up your environment variables. Run this once per terminal session from the repo root:

```bash
source setup.sh
```

This sets up the following environment variables:
- `LINUXCNC_REPO_ROOT` - automatically set to the directory containing setup.sh
- `SHDR_HOST` - defaults to `0.0.0.0` (listen on all interfaces)
- `SHDR_DUMP_FILE` - defaults to `$LINUXCNC_REPO_ROOT/logs/adapter_lcnc.log`
- `SHDR_NOISE_FEED_STD` - defaults to `0.1`

You can customize these values by editing `setup.sh` before sourcing it.

### Setup the MTConnect Agent docker container

1. Get the MTConnect agent

    The MTConnect agent is available as a Docker image. Pull the latest version:

    ```bash
    docker pull mtconnect/agent:latest
    ```

    This will download the official MTConnect agent Docker image from Docker Hub.

2. Setup agent config file

    Set up `agent.container.cfg` with your IP. First, get your IP (if using WSL, make sure it's your WSL IP) with: 

    ```
    hostname -I | awk '{print $1}'
    ```

    Then, put that into `agent.container.cfg`.

## Running MTConnect with LinuxCNC

1. Launch LinuxCNC (run command below from repo root). Open the NC program you want. 

    ```bash
    linuxcnc configs/sim.axis/axis.ini
    ```

2. Run the agent

    Make sure the path after `-v` points to your mtconnect directory. You can use `$LINUXCNC_REPO_ROOT` if you've sourced `setup.sh`.

    ```bash
    source setup.sh
    docker run -d --rm --name lcnc-agent -p 15000:5000 -v $LINUXCNC_REPO_ROOT/mtconnect:/config mtconnect/agent:latest /usr/bin/mtcagent run /config/agent.container.cfg
    ```

    If this is working, you should see a message like `Agent connected from ('123.345.78.90', 58575)` in the python adapter console. 

3. Run the adapter

    ```bash
    source setup.sh
    python3 mtconnect/adapter_lcnc.py --log-fields block line_number program_comment
    ```

    Since there is already an agent running in a container, you should see a message like `Agent connected from ('172.31.64.1', 59051)`. 

4. Verify endpoints

    Check, for example, the endpoints below to see if MTConnect streams are available: 

    http://localhost:15000/probe

    http://localhost:15000/current

    http://localhost:15000/sample?count=100


5. Start mtc logger

    Start running the LinuxCNC simulation and start capturing data with the script below:

    ```bash
    source setup.sh
    python3 mtconnect/mtc_logger.py --agent-url http://localhost:15000 --log-file $LINUXCNC_REPO_ROOT/logs/mtc_client.log --count 1000 --interval 0.5
    ```

6. Filter the log results for processing

    Once you have finished capturing data, you can filter it and dump it to an XML file for further processing: 
    
    ```bash
    python3 mtconnect/log_processor.py logs/mtc_client.log -o logs/xml_only.xml
    ```

## References

The QIF and NC data used to set up this demonstrator were taken from this publicly available dataset: [Design, Manufacturing, and Inspection Data for a Box Assembly - Catalog](https://catalog.data.gov/dataset/design-manufacturing-and-inspection-data-for-a-box-assembly-9b03e). Big thanks to the [Manufacturing Technology Centre (MTC)](https://www.the-mtc.org/) and [NIST](https://www.nist.gov/) for their research and efforts in this area!

- Hedberg Jr., T. D., Sharp, M. E., Maw, T. M. M., Helu, M., Rahman, M., Jadhav, S., Whicker, J. J., & Barnard Feeney, A. (2021). *Defining requirements for integrating information between design, manufacturing, and inspection*. *International Journal of Production Research.* https://doi.org/10.1080/00207543.2021.1920057  
- Hedberg, T. D., Sharp, M. E., Maw, T. M. M., Rahman, M. M., Jadhav, S., Whicker, J. J., Barnard Feeney, A., & Helu, M. (2019). *Design, manufacturing, and inspection data for a three-component assembly.* *Journal of Research of the National Institute of Standards and Technology, 124*, Article 124004. https://doi.org/10.6028/jres.124.004


## About Rubypoint

Have any questions? Get in touch with us here:

[![Website](https://img.shields.io/badge/Website-rubypoint.io-2ea44f?style=for-the-badge)](https://rubypoint.io/) 

[![LinkedIn](https://img.shields.io/badge/LinkedIn-0077B5?style=for-the-badge&logo=linkedin&logoColor=white)](https://www.linkedin.com/company/rubypoint/)
