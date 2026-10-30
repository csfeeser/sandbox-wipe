import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_DEFAULT_REGION": "us-east-1",
             "SANDBOX_WIPE_RETRY_DELAY": "0", "SANDBOX_WIPE_POLL_DELAY": "0", "SANDBOX_WIPE_ROUND_WAIT": "0",
             "MOTO_EC2_LOAD_DEFAULT_AMIS": "false", "AWS_CONFIG_FILE": "/nonexistent", "AWS_SHARED_CREDENTIALS_FILE": "/nonexistent"}.items():
    os.environ[k] = v
