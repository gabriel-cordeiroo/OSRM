import boto3
import json

s3 = boto3.client('s3')

bookmark = {
    "completed_partitions": [],
    "delta_timestamps": {},
    "last_updated": "2025-12-01T15:00:00+00:00"
}

s3.put_object(
    Bucket='20-ze-datalake-landing',
    Key='osrm_distance/control/bookmark.json',
    Body=json.dumps(bookmark, indent=2)
)

print("✅ Bookmark resetado!")