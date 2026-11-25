import os,json,tempfile
from pathlib import Path
_TEST_PROMPTS=tempfile.TemporaryDirectory(prefix="customer-public-test-prompts-")
_path=Path(_TEST_PROMPTS.name)/"prompts.json"
_path.write_text(json.dumps({k:"Synthetic offline test instruction."for k in ["admin","customer","order_intent","knowledge_extraction"]}))
os.environ["PUBLIC_PROMPTS_FILE"]=str(_path)
