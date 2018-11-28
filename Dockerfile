FROM python
RUN pip install redis numpy pandas azure-storage
COPY ./aggregate_temperature.py /aggregate_temperature.py
COPY ./rediswq.py /rediswq.py

CMD  python aggregate_temperature.py