#!/usr/bin/env python3

from main import *
import time

session = get_session()
influx = get_influx()
nodeset = NodeSet()

TIMEOUT = 1
PING_INTERVAL = 15

influx_keep_time = datetime.timedelta(minutes=15)

nodeset.update_from_db(session)
nodeset.load_from_influx(influx, delta=influx_keep_time)

try:
    while True:
        nodeset.update_from_db(session)

        for i in range(PING_INTERVAL // TIMEOUT):
            start = time.time()
            nodeset.ping_sliced(i, PING_INTERVAL, TIMEOUT)
            end = time.time()
            delta = start + TIMEOUT - end
            if delta > 0:
                time.sleep(delta)

        nodeset.save_to_influx(influx)

        for n in nodeset.nodes:
            if n.check_alarm(session):
                print("node " + n.name + ": alarm")
            elif n.check_resolved(session):
                print("node " + n.name + ": resolved")

        nodeset.flush_cache_all(delta=influx_keep_time)

except KeyboardInterrupt:
    print("CTRL + C pressed. Exiting.")
