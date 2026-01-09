import os
from datetime import datetime
from typing import List, NewType

LongLat = NewType("LongLat", List[float])
StartEndPair = NewType("StartEndPair", List[LongLat])

SOURCE_BUCKET = "50-ze-datalake-refined"
DESTINATION_BUCKET = "20-ze-datalake-landing"

SETUP = {
    "input_s3_base_prefix": 'data_mesh/vw_antifraud_fact_distances',
    "output_s3_base_prefix": 'osrm_distance/osrm_landing',
    "bookmark_s3_key": 'osrm_distance/control/bookmark.json',
    "LOCAL_TEMP_DIR": '/home/ubuntu/osrm_temp_parts',
    "start_coordinates": ["poc_longitude", "poc_latitude"],
    "end_coordinates": ["order_longitude", "order_latitude"],
    "metadata_columns": ["order_number"],
    "BATCH_SIZE": 1024*40,
    "NUM_PROCESSES": 15,
    "MAX_CONCURRENT": 30,
    "BLOCK_SIZE": 1_500_000,
    "skip_download": False,
}

processing_date = datetime.now().strftime('%Y-%m-%d')