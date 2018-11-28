import datetime
from io import BytesIO
import json
import os

from azure.storage.blob import BlockBlobService
import pandas as pd
import numpy as np
import redis

import rediswq

AZURE_CREDENTIALS_PATH = os.environ.get('AZURE_CREDENTIALS_PATH')
STORAGE_CONTAINER = 'temperaturefiles'


def get_account_credentials():
    with open(AZURE_CREDENTIALS_PATH, 'r') as f:
        azure_credentials = json.load(f)
    storage_account = azure_credentials.get('storage_account')
    account_key = azure_credentials.get('account_key')
    return storage_account, account_key


def read_from_temperature_csv_file(filename, block_blob_service):
    my_stream_obj = BytesIO()
    block_blob_service.get_blob_to_stream(STORAGE_CONTAINER, filename, my_stream_obj)
    my_stream_obj.seek(0)
    return pd.read_csv(my_stream_obj)


def aggregate_df(temperature_df):
    dt_format = '%Y-%m-%d %H:%M:%S'
    temperature_df['Datetime'] = pd.to_datetime(temperature_df['Datetime'], format=dt_format)
    return temperature_df.set_index('Datetime').resample('H').agg([np.mean, np.std, np.max, np.min])


def save_aggregated_temperature(aggregated_df, filename, block_blob_service):
    output_filename = '.'.join(filename.split('.')[:-1]) + '_aggregated.csv'
    block_blob_service.create_blob_from_bytes(
        STORAGE_CONTAINER,
        output_filename,
        aggregated_df.to_csv(index=False).encode('utf-8'))


def aggregate_temperature_file(filename):
    storage_account, account_key = get_account_credentials()
    block_blob_service = BlockBlobService(account_name=storage_account, account_key=account_key)
    temperature_df = read_from_temperature_csv_file(filename, block_blob_service)
    aggregated_df = aggregate_df(temperature_df)
    save_aggregated_temperature(aggregated_df, filename, block_blob_service)


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
