import threading
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

from apscheduler.schedulers.background import BackgroundScheduler
from openai import OpenAI

from khoj.database.models import ProcessLock
from khoj.utils import config as utils_config
from khoj.utils.helpers import LRU, get_device

# Application Global State
openai_client: OpenAI = None
log_file: Path = None
verbose: int = 0
host: str = None
port: int = None
ssl_config: Dict[str, str] = None
cli_args: List[str] = None
query_cache: Dict[str, LRU] = defaultdict(LRU)
chat_lock = threading.Lock()
SearchType = utils_config.SearchType
scheduler: BackgroundScheduler = None
schedule_leader_process_lock: ProcessLock = None
khoj_version: str = None
device = get_device()
anonymous_mode: bool = False
billing_enabled: bool = False
