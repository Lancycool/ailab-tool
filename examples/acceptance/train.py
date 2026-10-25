"""Bounded synthetic GPU smoke test for Campus Runner acceptance."""
import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import torch

parser = argparse.ArgumentParser()
parser.add_argument("--steps", type=int, choices=range(1, 21), default=12)
parser.add_argument("--seed", type=int, default=7)
args = parser.parse_args()
if not torch.cuda.is_available():
    raise SystemExit("CUDA is not available")
torch.manual_seed(args.seed)
torch.set_num_threads(1)
output = Path(os.environ["CAMPUS_OUTPUT_DIR"])
metrics = Path(os.environ["CAMPUS_METRICS_PATH"])
metrics.parent.mkdir(parents=True, exist_ok=True)
model = torch.nn.Linear(16, 1).cuda()
optimizer = torch.optim.SGD(model.parameters(), lr=0.03)
x = torch.randn(128, 16, device="cuda")
y = x.sum(dim=1, keepdim=True)
print(json.dumps({"run_id": os.environ["CAMPUS_RUN_ID"], "gpu": torch.cuda.get_device_name(0), "torch": torch.__version__}), flush=True)
for step in range(args.steps):
    optimizer.zero_grad()
    loss = (model(x) - y).square().mean()
    loss.backward()
    optimizer.step()
    value = loss.item()
    row = {"schema_version": 1, "step": step, "split": "train", "metrics": {"loss": value}, "timestamp": datetime.now(timezone.utc).isoformat()}
    with metrics.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row) + "\n")
    print("step={} loss={:.6f}".format(step, value), flush=True)
    time.sleep(1)
torch.save(model.state_dict(), output / "model.pt")
(output / "summary.json").write_text(json.dumps({"steps": args.steps, "final_loss": value, "seed": args.seed}), encoding="utf-8")
print("acceptance training completed", flush=True)
