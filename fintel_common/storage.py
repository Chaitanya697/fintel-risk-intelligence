"""Storage layer: the same code reads/writes AWS S3 in the cloud or a local folder on a laptop.

DATA_BACKEND=s3    -> uses bucket S3_BUCKET (credentials come from the EC2 instance role)
DATA_BACKEND=local -> uses folder LOCAL_DATA_DIR (default ./data)

Layout (same in both backends), matching the project design:
    raw/<SYMBOL>.csv, raw/stock_metadata.csv      <- Kaggle NIFTY50 dataset
    processed/returns.csv.gz, processed/...       <- written by data-processor
    models/risk_model.joblib, models/model_card.json
"""
import io
import os
from pathlib import Path


class Storage:
    def __init__(self):
        self.backend = os.getenv("DATA_BACKEND", "local").lower()
        if self.backend == "s3":
            import boto3

            self.bucket = os.environ["S3_BUCKET"]
            self.s3 = boto3.client("s3", region_name=os.getenv("AWS_REGION", "us-east-1"))
        else:
            self.root = Path(os.getenv("LOCAL_DATA_DIR", "data")).resolve()

    @property
    def location(self) -> str:
        return f"s3://{self.bucket}" if self.backend == "s3" else str(self.root)

    def list(self, prefix: str) -> list:
        if self.backend == "s3":
            keys, token = [], None
            while True:
                kw = {"Bucket": self.bucket, "Prefix": prefix}
                if token:
                    kw["ContinuationToken"] = token
                resp = self.s3.list_objects_v2(**kw)
                keys += [o["Key"] for o in resp.get("Contents", [])]
                if not resp.get("IsTruncated"):
                    return keys
                token = resp["NextContinuationToken"]
        base = self.root / prefix
        if not base.exists():
            return []
        return sorted(str(p.relative_to(self.root)).replace(os.sep, "/") for p in base.rglob("*") if p.is_file())

    def exists(self, key: str) -> bool:
        if self.backend == "s3":
            try:
                self.s3.head_object(Bucket=self.bucket, Key=key)
                return True
            except Exception:
                return False
        return (self.root / key).exists()

    def read_bytes(self, key: str) -> bytes:
        if self.backend == "s3":
            return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        return (self.root / key).read_bytes()

    def write_bytes(self, key: str, data: bytes) -> None:
        if self.backend == "s3":
            self.s3.put_object(Bucket=self.bucket, Key=key, Body=data)
            return
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def open_read(self, key: str):
        return io.BytesIO(self.read_bytes(key))
