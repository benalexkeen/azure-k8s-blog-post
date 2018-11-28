## Processing tasks from a message queue using Azure Kubernetes Service and python

In this post, we're going to have a look at using Azure Kubernetes Service to scale out the processing of tasks from a message queue using Azure Kubernetes Service (AKS).

We will read in some weather data that has temperature values at a 1-minute granularity, e.g.:

| Datetime             | Temperature   |
|----------------------|---------------|
| 2017-12-01 00:00:00  | 16.7          |
| 2017-12-01 00:01:00  | 16.8          |
| 2017-12-01 00:02:00  | 16.8          |

We have 6 CSV files with temperatures at different locations stored in Azure Blob Storage:
 * `temperature1.csv`
 * `temperature2.csv`
 * `temperature3.csv`
 * `temperature4.csv`
 * `temperature5.csv`
 * `temperature6.csv`

And we will be downloading these CSV files aggregating them up to hourly granularity and taking the mean, standard deviation, minimum and maximum temperatures for each hour and storing it back to Azure Blob Storage.

We will split out this processing across nodes on a kubernetes cluster in Azure Kubernetes Service. The files to accompany this blog post can be found [here](https://github.com/benalexkeen/azure-k8s-blog-post). Some of the commands in this post assume you have cloned this repository and are in the root of this repository.

### Create Resource Group

We'll start by creating a resource group for all of our resources, I'm in West Europe so I'll use that location:

```bash
az group create --name myAKSResourceGroup --location westeurope
```

We should have an output that looks like:
```json
{
  "id": "/subscriptions/7dfe4fd7-24b7-466f-90d0-467628c1d8b2/resourceGroups/myAKSResourceGroup",
  "location": "westeurope",
  "managedBy": null,
  "name": "myAKSResourceGroup",
  "properties": {
    "provisioningState": "Succeeded"
  },
  "tags": null
}
```

### Create Cluster

We'll now create our Azure Kubernetes Service (AKS) cluster. We will create a 3 node cluster:

```bash
az aks create --resource-group myAKSResourceGroup --name redisQueueCluster --node-count 3 --generate-ssh-keys
```

This will take a few minutes and you'll get an output with information about your cluster.

### Set kubectl credentials

Kubectl is the kubernetes command line interface, if we have the azure command line tools or (gcloud tools for anyone that's used GCP as well) installed then we'll already have kubectl.

To set up the credentials for kubectl, we can run:
```bash
az aks get-credentials --resource-group myAKSResourceGroup --name redisQueueCluster
```

To check this has worked and to view the nodes in our cluster, we'll then run:
```bash

kubectl get nodes

```
And we should see an output that resembles the following:
```
NAME                       STATUS    ROLES     AGE       VERSION
aks-nodepool1-26984785-0   Ready     agent     2m       v1.9.11
aks-nodepool1-26984785-1   Ready     agent     2m       v1.9.11
aks-nodepool1-26984785-2   Ready     agent     2m       v1.9.11
```

### Create redis service

We'll create the redis service in the kubernetes cluster (note that your workers could just as easily read from an external redis instance or Azure Redis Cache).

We'll first create a redis pod (process running redis). We'll use our `redis-pod.yaml` file to set this up, the file contents look like:

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: redis-master
  labels:
    app: redis
spec:
  containers:
    - name: master
      image: redis
      env:
        - name: MASTER
          value: "true"
      ports:
        - containerPort: 6379
```

And we run the command:
```bash
kubectl create -f ./redis-pod.yaml
```

We'll then create a redis service (endpoint to make the pod accessible to other pods/externally). We'll make this pod exposed externally using our cloud provider's load balance, so that we can add tasks from our local machine, but if you were adding tasks from within the cluster, there's no need to expose this pod externally. The `redis-service.yaml` file looks like:
```yaml

apiVersion: v1
kind: Service
metadata:
  name: redis
spec:
  ports:
    - port: 6379
      targetPort: 6379
  selector:
    app: redis
  type: LoadBalancer

```

We run the command:
```bash
kubectl create -f ./redis-service.yaml
```

To check on the status of our redis service and to get the external IP of our redis service:
```bash
kubectl get service redis
```

Which should output something that resembles the following:
```
NAME      TYPE           CLUSTER-IP    EXTERNAL-IP    PORT(S)          AGE
redis     LoadBalancer   10.0.145.70   40.68.23.150   6379:30233/TCP   1m
```

### Add tasks to redis queue

We can add tasks to our redis queue using the `EXTERNAL-IP` from the output above.

We'll create a task queue `temperature_job` with the filename of each of the CSVs we want to analyse.

We can do this by running a short python script:

```python
import redis

host = '40.68.23.150'
port = 6379

r = redis.Redis(host=host, port=port)

files = []
for i in range(1, 7):
    files.append('temperature{}.csv'.format(i))

# Push filenames onto queue
r.rpush('temperature_job', *files)

# View items in queue
print(r.lrange('temperature_job', 0, -1))
```

### Create Azure Container Registry

Our redis-master container comes from an openly available redis image.

However, we'll often want to use our own private containers that are not available on a public container registry, particularly when doing our processing. To do this, we can create our own Azure Container Registry.

We can do this using the following command:
```bash
az acr create --resource-group myAKSResourceGroup --name redisQueueContainerRegistry --sku Basic
```
Note that the container registry name must be unique. Once complete, we will see output with some information about our newly created container registry instance.

To log in to our newly created container registry so that we can push our own local images to it, we can run:
```bash
az acr login --name redisQueueContainerRegistry
```
And we should see the following output:
```
Login Succeeded
```

### Allow Azure Kubernetes Service access to Azure Container Registry

In order for our AKS instance to pull images from our container registry, we need to allow it access to do so. We can run the following shell script to allow us to do this:

``` bash
AKS_RESOURCE_GROUP=myAKSResourceGroup
AKS_CLUSTER_NAME=redisQueueCluster
ACR_RESOURCE_GROUP=myAKSResourceGroup
ACR_NAME=redisQueueContainerRegistry

# Get the id of the service principal configured for AKS 
CLIENT_ID=$(az aks show --resource-group $AKS_RESOURCE_GROUP --name $AKS_CLUSTER_NAME --query "servicePrincipalProfile.clientId" --output tsv)

# Get the ACR registry resource id 
ACR_ID=$(az acr show --name $ACR_NAME --resource-group $ACR_RESOURCE_GROUP --query "id" --output tsv) 

# Create role assignment
az role assignment create --assignee $CLIENT_ID --role Reader --scope $ACR_ID
```

Once complete, we'll see an output confirming this operation of type `Microsoft.Authorization/roleAssignments`.

### Upload CSVs to Azure Blob Storage

#### Create Storage Account
We'll be creating an azure storage account to store our CSV files, we'll run the following command to do this:
```bash
az storage account create \
    --name weatherfiles \
    --resource-group myAKSResourceGroup \
    --location westeurope \
    --sku Standard_LRS \
    --encryption blob
```

Once complete, we'll see an output confirming so with details about our newly created storage account.

#### Get Account Keys

To get the credentials for our storage account:
```bash
az storage account keys list --account-name weatherfiles --resource-group myAKSResourceGroup
```

You should see an output that looks like:
```bash

[
  {
    "keyName": "key1",
    "permissions": "Full",
    "value": "<key1_value>"
  },
  {
    "keyName": "key2",
    "permissions": "Full",
    "value": "<key2_value>"
  }
]

```

We'll want to store these credentials in environment variables:
```
export AZURE_STORAGE_ACCOUNT=weatherfiles
export AZURE_STORAGE_ACCESS_KEY=<key>
```

We'll also need to create a json store these credentials `azure_credentials.json`, we'll be using this later to import this key into your docker container without having to add it to the docker image, create this file as follows:
```json
{
	"storage_account": "weatherfiles",
	"account_key": "<your account key>"
}
```

#### Create a container
We can now create a container for our CSV files:
```bash
az storage container create --name temperaturefiles
```

When created, we should see the output
```json
{
  "created": true
}
```

#### Upload files to blob
To upload our CSV files we can run the following shell script:
```bash
for filename in temperature_csvs/*.csv; do
    az storage blob upload \
        --container-name temperaturefiles \
        --name $(basename $filename) \
        --file $filename
done
```

### Creating worker to consume tasks and aggregate CSV files

We will be using the redis worker `rediswq.py` file that can be found in the kubernetes docs [here](https://kubernetes.io/examples/application/job/redis/rediswq.py) and is also available in the repository for this blog post in order to read tasks from our redis queue. This provides helper functions for using redis as a queue including, for example, leasing items on the queue.

We'll then define a python file to download our CSV files, this python file is available [here](https://github.com/benalexkeen/azure-k8s-blog-post/blob/master/aggregate_temperature.py) but in this post, we'll go through step-by-step.

We first define our imports, we'll need the `redis`, `azure-storage`, `numpy` and `pandas` external packages installed and we'll be importing from our `rediswq.py` file.

```python
import datetime
from io import BytesIO
import json
import os

from azure.storage.blob import BlockBlobService
import pandas as pd
import numpy as np
import redis

import rediswq
```

Next we'll define a function to get our azure storage credentials, we'll grab the credentials path to the json file we created above (`azure_credentials.json`) from the environment variable `AZURE_CREDENTIALS_PATH`.

```python
AZURE_CREDENTIALS_PATH = os.environ.get('AZURE_CREDENTIALS_PATH')
STORAGE_CONTAINER = 'temperaturefiles'


def get_account_credentials():
    with open(AZURE_CREDENTIALS_PATH, 'r') as f:
        azure_credentials = json.load(f)
    storage_account = azure_credentials.get('storage_account')
    account_key = azure_credentials.get('account_key')
    return storage_account, account_key
```

Then we download our temperature file from our azure storage account, we'll stream the blob to a `BytesIO` object and read directly from into a pandas DataFrame this using pandas `read_csv` function:
```python
def read_from_temperature_csv_file(filename, block_blob_service):
    my_stream_obj = BytesIO()
    block_blob_service.get_blob_to_stream(STORAGE_CONTAINER, filename, my_stream_obj)
    my_stream_obj.seek(0)
    return pd.read_csv(my_stream_obj)
```

We can then aggregate our DataFrame using the pandas DataFrame `resample` method. We first convert the `Datetime` column from strings to datetime objects, then set this column as our index for resampling. For more information on resampling time series data, see my blog post on [resampling here](http://benalexkeen.com/resampling-time-series-data-with-pandas/).
```python
def aggregate_df(temperature_df):
    dt_format = '%Y-%m-%d %H:%M:%S'
    temperature_df['Datetime'] = pd.to_datetime(temperature_df['Datetime'], format=dt_format)
    return temperature_df.set_index('Datetime').resample('H').agg([np.mean, np.std, np.max, np.min])
```

We can then save this aggregated temperature DataFrame to a CSV file in the BLOB store.
```python
def save_aggregated_temperature(aggregated_df, filename, block_blob_service):
    output_filename = '.'.join(filename.split('.')[:-1]) + '_aggregated.csv'
    block_blob_service.create_blob_from_bytes(
        STORAGE_CONTAINER,
        output_filename,
        aggregated_df.to_csv(index=False).encode('utf-8'))
```

Then to put all the above functions together, we create one function that can be called with a `filename` parameter:
```python
def aggregate_temperature_file(filename):
    storage_account, account_key = get_account_credentials()
    block_blob_service = BlockBlobService(account_name=storage_account, account_key=account_key)
    temperature_df = read_from_temperature_csv_file(filename, block_blob_service)
    aggregated_df = aggregate_df(temperature_df)
    save_aggregated_temperature(aggregated_df, filename, block_blob_service)
```

We will also create a main function that will read from the redis queue, determine whether there are any filenames on the redis queue and, if so, process the next filename on the queue.
```python
def main():
    redis_host = os.environ.get("REDIS_HOST")
    if not redis_host:
        redis_host = "redis"
    q = rediswq.RedisWQ(name="temperature_job", host=redis_host)
    print("Worker with sessionID: " +  q.sessionID())
    print("Initial queue state: empty=" + str(q.empty()))
    while not q.empty():
        item = q.lease(lease_secs=180, block=True, timeout=2) 
        if item is not None:
            filename = item.decode("utf=8")
            print("Aggregating " + filename)
            aggregate_temperature_file(filename)
            q.complete(item)
        else:
            print("Waiting for work")
            import time
            time.sleep(5)


if __name__ == '__main__':
    main()
```

### Containerising worker script

Our worker script needs to be containerised and we will use docker to containerise our script and push this docker image to the Azure Container Service, ready to be pulled by our Azure Kubernetes Service.

The `Dockerfile` here is a very simple one, it uses the publically-available `python` base image and pip installs some external package requirements, before copying over the python files we require and running our worker script:

```
FROM python
RUN pip install redis numpy pandas azure-storage
COPY ./aggregate_temperature.py /aggregate_temperature.py
COPY ./rediswq.py /rediswq.py

CMD  python aggregate_temperature.py
```

To build our docker image run from the root of the repository:
```bash
docker build -t aggregate-temperature .
```
You should see an output that resembles the following as the script runs through the steps:
```
Sending build context to Docker daemon  209.8MB
Step 1/5 : FROM python
 ---> a187104266fb
Step 2/5 : RUN pip install redis numpy pandas azure-storage
 ---> Using cache
 ---> 01a514bbd792
Step 3/5 : COPY ./aggregate_temperature.py /aggregate_temperature.py
 ---> 887ee9c41929
Step 4/5 : COPY ./rediswq.py /rediswq.py
 ---> 3fed313cadc4
Step 5/5 : CMD  python aggregate_temperature.py
 ---> Running in 3e7124bacf36
Removing intermediate container 3e7124bacf36
 ---> ce7a17b998af
Successfully built ce7a17b998af
Successfully tagged aggregate-temperature:latest
```

### Pushing image to Azure Container Registry

Now that we've built our docker image, we can push it to our Azure Container Registry. First we need to get our Azure Container Registry server URL:
```bash
az acr list --resource-group myAKSResourceGroup --query "[].{acrLoginServer:loginServer}" --output table
```
This should output something like:
```
AcrLoginServer
--------------------------------------
redisqueuecontainerregistry.azurecr.io
```

This URL is then used to push our built container to the registry. If we assume the URL above, we can tag our image:
```bash
docker tag aggregate-temperature redisqueuecontainerregistry.azurecr.io/aggregate-temperature
```

Then push our image:
```bash
docker push redisqueuecontainerregistry.azurecr.io/aggregate-temperature
```

We should see an output that resembles the following:
```
The push refers to repository [redisqueuecontainerregistry.azurecr.io/aggregate-temperature]
fd7441059c0a: Pushed
366d45665d4a: Pushed
e43801e1fb1f: Pushed
4c9ede4ddbda: Pushed
c134b6c064f6: Pushed
8eb8b96ceebb: Pushed
d62f0ea9a15e: Pushed
9978d084fd77: Pushed
1191b3f5862a: Pushed
08a01612ffca: Pushed
8bb25f9cdc41: Pushed
f715ed19c28b: Pushed
latest: digest: sha256:1f41ac17d3322628b72e23011d869e3f57062256680926b88ad47f4ae6706a38 size: 2846
```

### Creating Kubernetes Secret

Before we create the job to run scale out our processing of these CSV files, we'll start with creating a Kubernetes secret. This is an encrypted repository that kubernetes uses to securely store keys that we don't want to include in git/docker repositories.

We can create our secret for our Azure Storage Account credentials:

```bash
kubectl create secret generic azure-credentials --from-file=./azure_credentials.json
```

And we should see as an output:
```
secret "azure-credentials" created
```

### Creating Kubernetes Job

We'll now create our kubernetes job to scale out our processing of the CSV files from Azure Blob store.

We'll use the `job.yaml` file with the configuration for our kubernetes job. This job file defines things like
* Which image to use
* How many pods we'll be running in parallel
* Where to mount the secret key directory
* Our environment variable of the path to our azure credentials

And looks as follows:
```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: temperature-job
spec:
  parallelism: 3
  template:
    metadata:
      name: temperature-job
    spec:
      containers:
      - name: c
        image: redisqueuecontainerregistry.azurecr.io/aggregate-temperature
        env:
        - name: AZURE_CREDENTIALS_PATH
          value: /var/secrets/azure/azure_credentials.json
        volumeMounts:
        - name: azure-credentials
          mountPath: "/var/secrets/azure"
          readOnly: true
      volumes:
      - name: azure-credentials
        secret:
          secretName: azure-credentials
      restartPolicy: OnFailure
```

To run this job:

```bash
kubectl create -f ./job.yaml
```

We should see an output of:
```
job.batch "temperature-job" created
```

To see a description of the job, we can run:
```bash
kubectl describe jobs/temperature-job
```

And to see the status of the pods we can run:
```bash
kubectl get pods
```

This output will look like:
```
NAME                    READY     STATUS              RESTARTS   AGE
redis-master            1/1       Running             0          20m
temperature-job-fqg92   0/1       ContainerCreating   0          38s
temperature-job-pprsp   0/1       ContainerCreating   0          38s
temperature-job-s4s48   0/1       ContainerCreating   0          38s
```

Until the containers are up and running, then:
```
NAME                    READY     STATUS    RESTARTS   AGE
redis-master            1/1       Running   0          24m
temperature-job-fqg92   1/1       Running   0          4m
temperature-job-pprsp   1/1       Running   0          4m
temperature-job-s4s48   1/1       Running   0          4m
```

### Remove Resources

Because everything here was created in the same resource group, to remove everything, we can just run:
```
az group delete --name myAKSResourceGroup --yes --no-wait
```
